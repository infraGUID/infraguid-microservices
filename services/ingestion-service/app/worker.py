"""SQS consumer for async knowledge-base ingestion.

The `/ingest` endpoint enqueues a job and returns immediately; this worker
drains the queue and runs the (slow, Bedrock-throttled) ingestion out of band.

Lifecycle: started as a background asyncio task from the FastAPI lifespan when
`INGESTION_WORKER_ENABLED` is true and an SQS queue URL is configured. On
failure the message is left un-deleted so SQS redelivers it after the
visibility timeout; after `maxReceiveCount` it lands in the dead-letter queue.
"""
from __future__ import annotations

import asyncio
import json

from infraguid_common.config.settings import get_settings
from infraguid_common.database.postgres_client import session_scope
from infraguid_common.observability.logger import get_logger
from infraguid_common.queue import sqs_client

from app.ingestion.ingest import IngestionService

logger = get_logger(__name__)


async def run_ingestion_job(reset: bool = True) -> dict:
    """Run a single ingestion pass inside its own DB session."""
    async with session_scope() as session:
        return await IngestionService().run(session, reset=reset)


async def _handle_message(message: dict) -> None:
    settings = get_settings()
    receipt_handle = message["ReceiptHandle"]
    try:
        body = json.loads(message.get("Body") or "{}")
    except json.JSONDecodeError:
        body = {}

    job_id = body.get("job_id", "unknown")
    reset = bool(body.get("reset", True))
    logger.info("ingestion_job_started", job_id=job_id, reset=reset)

    result = await run_ingestion_job(reset=reset)

    # Only acknowledge (delete) once ingestion has fully succeeded so a crash
    # mid-run lets SQS redeliver the job rather than silently dropping it.
    await sqs_client.delete_message(settings.sqs_ingestion_queue_url, receipt_handle)
    logger.info("ingestion_job_completed", job_id=job_id, **result)


async def consume_forever(stop_event: asyncio.Event) -> None:
    """Long-poll the queue until `stop_event` is set."""
    settings = get_settings()
    queue_url = settings.sqs_ingestion_queue_url
    logger.info("ingestion_worker_starting", queue=queue_url)

    while not stop_event.is_set():
        try:
            messages = await sqs_client.receive_messages(
                queue_url,
                max_messages=1,
                wait_time_seconds=settings.sqs_wait_time_seconds,
                visibility_timeout=settings.sqs_visibility_timeout,
            )
            for message in messages:
                await _handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never let a transient error kill the loop; back off briefly and
            # retry. The failed message stays invisible then redelivers.
            logger.error("ingestion_worker_error", error=str(exc))
            await asyncio.sleep(5)

    logger.info("ingestion_worker_stopped")
