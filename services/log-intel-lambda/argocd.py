"""Best-effort ArgoCD deployment correlation for the Log Intelligence Agent.

Answers "did a recent deploy cause this incident?" by reading the ArgoCD app's
recent sync/health history. The API token lives in Secrets Manager
(ARGOCD_SECRET_ARN); the server is ARGOCD_SERVER_URL. Active only when
ENABLE_ARGOCD == "true". Uses botocore (bundled) + stdlib urllib only.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.parse
import urllib.request

import boto3

logger = logging.getLogger()

SERVER_URL = os.environ.get("ARGOCD_SERVER_URL", "").rstrip("/")
SECRET_ARN = os.environ.get("ARGOCD_SECRET_ARN", "")
ENABLED = os.environ.get("ENABLE_ARGOCD", "false").lower() == "true"

_secrets_client = None
_token_cache: str | None = None


def is_enabled() -> bool:
    return ENABLED and bool(SERVER_URL) and bool(SECRET_ARN)


def _secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def _token() -> str:
    """Fetch the ArgoCD API token from Secrets Manager (cached per container).

    Accepts either a raw token string or a JSON blob with a `token`/`authToken` key.
    """
    global _token_cache
    if _token_cache is None:
        raw = _secrets().get_secret_value(SecretId=SECRET_ARN)["SecretString"]
        try:
            doc = json.loads(raw)
            _token_cache = doc.get("token") or doc.get("authToken") or raw
        except (json.JSONDecodeError, AttributeError):
            _token_cache = raw
    return _token_cache


def _get(path: str, timeout: int = 8) -> dict:
    req = urllib.request.Request(SERVER_URL + path, method="GET")
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Accept", "application/json")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read())


def get_recent_history(app: str, limit: int = 5) -> dict:
    """Return the app's sync status, health and recent deployment history."""
    if not is_enabled():
        return {"enabled": False, "note": "ArgoCD correlation disabled"}

    raw = _get(f"/api/v1/applications/{urllib.parse.quote(app)}")
    status = raw.get("status", {})
    history = status.get("history", []) or []
    return {
        "enabled": True,
        "sync_status": status.get("sync", {}).get("status"),
        "health_status": status.get("health", {}).get("status"),
        "recent_deployments": [
            {
                "revision": (h.get("revision") or "")[:12],
                "deployedAt": h.get("deployedAt"),
                "id": h.get("id"),
            }
            for h in history[-limit:]
        ],
    }
