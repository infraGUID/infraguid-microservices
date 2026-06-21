from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from infraguid_common.config.settings import get_settings
from infraguid_common.database.postgres_client import get_db_session, ping_db
from infraguid_common.database.repositories.document_repo import DocumentRepository
from infraguid_common.llm.bedrock import BedrockThrottlingError, map_bedrock_error
from infraguid_common.observability.logger import get_logger
from infraguid_common.queue import sqs_client
from infraguid_common.vectorstore.pgvector_store import PgVectorStore

from app.ingestion.ingest import IngestionService

logger = get_logger(__name__)
router = APIRouter(tags=["ingestion"])


@router.get("/health")
async def health() -> dict:
    try:
        await ping_db()
        return {"status": "healthy", "service": "ingestion-service"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"ingestion-service unhealthy: {exc}") from exc


@router.post("/ingest")
async def ingest(session: AsyncSession = Depends(get_db_session)) -> dict:
    """Trigger a knowledge-base (re)ingestion.

    When an SQS queue is configured, the job is enqueued and processed out of
    band by the worker (returns 202-style `queued` immediately). Without a
    queue (local dev) it falls back to running synchronously.
    """
    settings = get_settings()

    if settings.sqs_ingestion_enabled:
        job_id = str(uuid4())
        try:
            message_id = await sqs_client.send_message(
                settings.sqs_ingestion_queue_url,
                {"job_id": job_id, "action": "ingest", "reset": True},
            )
        except Exception as exc:
            logger.error("ingest_enqueue_failed", error=str(exc))
            raise HTTPException(status_code=503, detail=f"Could not enqueue ingestion job: {exc}") from exc
        logger.info("ingest_enqueued", job_id=job_id, message_id=message_id)
        return {"status": "queued", "job_id": job_id, "message_id": message_id}

    # Synchronous fallback (no SQS configured).
    try:
        return await IngestionService().run(session, reset=True)
    except Exception as exc:
        mapped = map_bedrock_error(exc)
        if isinstance(mapped, BedrockThrottlingError):
            raise HTTPException(status_code=429, detail=f"Bedrock throttling limit reached: {mapped}") from exc
        logger.error("ingest_failed", error=str(mapped))
        raise HTTPException(status_code=503, detail=f"Ingestion failed: {mapped}") from exc


@router.get("/ingest/status")
async def ingest_status(session: AsyncSession = Depends(get_db_session)) -> dict:
    """Return the last ingestion result plus live SQS queue depth (if configured).

    Callers can poll this until ``queue.pending + queue.in_flight == 0`` to know
    when an async job has finished, then read ``last_run`` for the final result.
    """
    settings = get_settings()
    repo = DocumentRepository(session)

    last_run = await repo.latest_ingestion_log()

    queue: dict = {"pending": 0, "in_flight": 0}
    if settings.sqs_ingestion_enabled:
        try:
            queue = await sqs_client.get_queue_depth(settings.sqs_ingestion_queue_url)
        except Exception as exc:
            logger.warning("ingest_status_queue_depth_failed", error=str(exc))

    busy = queue["pending"] + queue["in_flight"] > 0
    return {
        "state": "running" if busy else (last_run["status"] if last_run else "idle"),
        "last_run": last_run,
        "queue": queue,
    }


@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_db_session)) -> dict:
    try:
        counts = await DocumentRepository(session).counts()
        vector_chunks = await PgVectorStore(session).count()
        return {
            "documents": counts["documents"],
            "chunks": counts["chunks"],
            "vector_chunks": vector_chunks,
            "last_ingestion": counts["last_ingestion"].isoformat() if counts["last_ingestion"] else None,
        }
    except Exception as exc:
        logger.error("ingestion_stats_failed", error=str(exc))
        raise HTTPException(status_code=503, detail=f"Stats unavailable: {exc}") from exc
