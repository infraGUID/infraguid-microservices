"""Live Kubernetes API access for the Log Intelligence Agent.

The Lambda role is mapped (via an EKS access entry) to the read-only
`log-intel-readers` RBAC group, so it can GET/LIST pods, events, nodes and
workloads. Authentication uses the standard aws-iam-authenticator scheme: a
presigned STS GetCallerIdentity URL carrying the cluster name in the
`x-k8s-aws-id` header, base64url-encoded and prefixed with `k8s-aws-v1.`.

Only botocore (bundled) + stdlib are used — no `kubernetes`/`awscli` package.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import time
import urllib.parse
import urllib.request

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger()

CLUSTER_NAME = os.environ.get("EKS_CLUSTER_NAME", "")
_TOKEN_PREFIX = "k8s-aws-v1."
_TOKEN_TTL_SECONDS = 60  # presigned URL validity; we cache slightly under this

_eks_client = None
_cluster_cache: dict | None = None
_ca_path: str | None = None
_token_cache: tuple[float, str] | None = None  # (expires_at_epoch, token)

def _eks():
    global _eks_client
    if _eks_client is None:
        _eks_client = boto3.client("eks")
    return _eks_client

def _describe_cluster() -> dict:
    """Return {endpoint, ca_data} for the cluster, cached per container."""
    global _cluster_cache
    if _cluster_cache is None:
        desc = _eks().describe_cluster(name=CLUSTER_NAME)["cluster"]
        _cluster_cache = {
            "endpoint": desc["endpoint"],
            "ca_data": desc["certificateAuthority"]["data"],
        }
    return _cluster_cache

def _ca_file() -> str:
    """Materialise the cluster CA to /tmp once, for TLS verification."""
    global _ca_path
    if _ca_path is None:
        ca = base64.b64decode(_describe_cluster()["ca_data"])
        path = "/tmp/eks-ca.pem"
        with open(path, "wb") as fh:
            fh.write(ca)
        _ca_path = path
    return _ca_path

def _bearer_token() -> str:
    """Generate (and briefly cache) an aws-iam-authenticator bearer token.

    The token is a presigned STS GetCallerIdentity URL, base64url-encoded and
    prefixed with `k8s-aws-v1.`. The `x-k8s-aws-id` header is included in the
    SigV4 signature (via SigV4QueryAuth + AWSRequest) so the EKS authenticator
    can verify which cluster the token targets — this is what `aws eks get-token`
    does internally. `botocore` is already bundled in the Lambda runtime, so no
    extra dependencies are needed.
    """
    global _token_cache
    now = time.time()
    if _token_cache and _token_cache[0] > now:
        return _token_cache[1]

    session = boto3.session.Session()
    region = session.region_name
    credentials = session.get_credentials().get_frozen_credentials()

    url = f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15"
    request = AWSRequest(method="GET", url=url, headers={"x-k8s-aws-id": CLUSTER_NAME})
    SigV4QueryAuth(credentials, "sts", region, expires=_TOKEN_TTL_SECONDS).add_auth(request)

    token = _TOKEN_PREFIX + base64.urlsafe_b64encode(
        request.url.encode("utf-8")
    ).decode("utf-8").rstrip("=")
    _token_cache = (now + _TOKEN_TTL_SECONDS - 10, token)
    return token

def _get(path: str, timeout: int = 8) -> dict:
    """Perform an authenticated GET against the cluster API server."""
    endpoint = _describe_cluster()["endpoint"]
    url = endpoint.rstrip("/") + path
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {_bearer_token()}")
    req.add_header("Accept", "application/json")
    ctx = ssl.create_default_context(cafile=_ca_file())
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read())

def get_pod(namespace: str, pod: str) -> dict:
    """Return the live pod object's status/conditions/container statuses."""
    raw = _get(f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/pods/{urllib.parse.quote(pod)}")
    status = raw.get("status", {})
    return {
        "phase": status.get("phase"),
        "reason": status.get("reason"),
        "message": status.get("message"),
        "conditions": status.get("conditions", []),
        "containerStatuses": [
            {
                "name": cs.get("name"),
                "ready": cs.get("ready"),
                "restartCount": cs.get("restartCount"),
                "state": cs.get("state"),
                "lastState": cs.get("lastState"),
            }
            for cs in status.get("containerStatuses", [])
        ],
    }

def list_events(namespace: str, limit: int = 30) -> list[dict]:
    """Return recent Kubernetes Events in a namespace (Warning first)."""
    raw = _get(
        f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/events?limit={limit}"
    )
    events = [
        {
            "type": e.get("type"),
            "reason": e.get("reason"),
            "object": f"{e.get('involvedObject', {}).get('kind')}/"
            f"{e.get('involvedObject', {}).get('name')}",
            "message": (e.get("message") or "")[:300],
            "count": e.get("count"),
            "lastTimestamp": e.get("lastTimestamp"),
        }
        for e in raw.get("items", [])
    ]
    events.sort(key=lambda e: 0 if e["type"] == "Warning" else 1)
    return events

def list_pods(namespace: str) -> list[dict]:
    """Return a compact phase/restart summary of pods in a namespace."""
    raw = _get(f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/pods")
    out = []
    for p in raw.get("items", []):
        st = p.get("status", {})
        out.append({
            "name": p.get("metadata", {}).get("name"),
            "phase": st.get("phase"),
            "restarts": sum(cs.get("restartCount", 0) for cs in st.get("containerStatuses", [])),
        })
    return out

def describe_node(node: str) -> dict:
    """Return a node's conditions and allocatable/capacity (pressure signals)."""
    raw = _get(f"/api/v1/nodes/{urllib.parse.quote(node)}")
    status = raw.get("status", {})
    return {
        "conditions": [
            {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
            for c in status.get("conditions", [])
        ],
        "capacity": status.get("capacity", {}),
        "allocatable": status.get("allocatable", {}),
    }
