"""Alert deduplication via DynamoDB conditional put.

Suppresses repeat alerts for the same (namespace, pod, error_type) tuple
within a rolling window (default 30 min). A single atomic PutItem with a
condition expression avoids any read-before-write race and has no cost beyond
a single write per unique incident per window.

If DEDUP_TABLE is unset, every call returns False (no suppression).
"""
from __future__ import annotations

import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()

DEDUP_TABLE = os.environ.get("DEDUP_TABLE", "")
DEDUP_TTL_SECONDS = int(os.environ.get("DEDUP_TTL_SECONDS", "1800"))  # 30 min

_table_resource = None

def _table():
    global _table_resource
    if _table_resource is None:
        _table_resource = boto3.resource("dynamodb").Table(DEDUP_TABLE)
    return _table_resource

def is_duplicate(namespace: str, pod: str, error_type: str) -> bool:
    """Return True if this (namespace, pod, error_type) was already alerted
    within the dedup window; otherwise record it and return False."""
    if not DEDUP_TABLE:
        return False

    pk = f"{namespace}/{pod}/{error_type}"
    now = int(time.time())
    expires = now + DEDUP_TTL_SECONDS

    try:
        _table().put_item(
            Item={"pk": pk, "ttl": expires},
            ConditionExpression="attribute_not_exists(pk) OR #t < :now",
            ExpressionAttributeNames={"#t": "ttl"},
            ExpressionAttributeValues={":now": now},
        )
        return False  # new incident — process it
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info("Suppressed duplicate alert: %s/%s (%s)", namespace, pod, error_type)
            return True  # live item with future TTL — suppress
        logger.warning("DynamoDB dedup check failed: %s", exc)
        return False  # fail open — better to over-alert than miss incidents
