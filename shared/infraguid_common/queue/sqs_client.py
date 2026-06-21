from __future__ import annotations

import asyncio
import json
from typing import Any

import boto3

from infraguid_common.config.settings import get_settings
from infraguid_common.observability.logger import get_logger

logger = get_logger(__name__)

_client = None


def _get_client():
    """Lazily build a boto3 SQS client using the default credential chain (IRSA)."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = boto3.client("sqs", region_name=settings.aws_region)
    return _client


async def send_message(queue_url: str, body: dict[str, Any]) -> str:
    """Enqueue a JSON message. Returns the SQS MessageId."""
    payload = json.dumps(body)
    # boto3 is synchronous; offload to a thread so we never block the event loop.
    response = await asyncio.to_thread(
        _get_client().send_message,
        QueueUrl=queue_url,
        MessageBody=payload,
    )
    message_id = response["MessageId"]
    logger.info("sqs_message_sent", queue=queue_url, message_id=message_id)
    return message_id


async def receive_messages(
    queue_url: str,
    max_messages: int = 1,
    wait_time_seconds: int = 20,
    visibility_timeout: int = 900,
) -> list[dict[str, Any]]:
    """Long-poll the queue. Returns raw SQS message dicts (empty list on timeout)."""
    response = await asyncio.to_thread(
        _get_client().receive_message,
        QueueUrl=queue_url,
        MaxNumberOfMessages=max_messages,
        WaitTimeSeconds=wait_time_seconds,
        VisibilityTimeout=visibility_timeout,
    )
    return response.get("Messages", [])


async def delete_message(queue_url: str, receipt_handle: str) -> None:
    """Acknowledge a message so it is not redelivered."""
    await asyncio.to_thread(
        _get_client().delete_message,
        QueueUrl=queue_url,
        ReceiptHandle=receipt_handle,
    )


async def get_queue_depth(queue_url: str) -> dict[str, int]:
    """Return approximate message counts for the queue.

    ``pending``  — visible messages waiting to be received.
    ``in_flight`` — messages currently held by a consumer (not yet deleted).
    """
    response = await asyncio.to_thread(
        _get_client().get_queue_attributes,
        QueueUrl=queue_url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
        ],
    )
    attrs = response.get("Attributes", {})
    return {
        "pending": int(attrs.get("ApproximateNumberOfMessages", 0)),
        "in_flight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
    }
