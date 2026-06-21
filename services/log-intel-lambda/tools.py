"""LangChain tools the ReAct agent uses to investigate an incident.

Every tool is READ-ONLY and defensive: each catches its own exceptions and
returns a short string instead of raising, so a failing tool never aborts the
agent graph. The active tool list is assembled by `get_tools()`, which drops
capabilities that aren't configured (e.g. ArgoCD).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from langchain_core.tools import tool

import argocd
import k8s

logger = logging.getLogger()

LOG_GROUP_NAME = os.environ.get("LOG_GROUP_NAME", "")
EKS_CLUSTER_NAME = os.environ.get("EKS_CLUSTER_NAME", "")

_logs_client = None
_cw_client = None


def _logs():
    global _logs_client
    if _logs_client is None:
        _logs_client = boto3.client("logs")
    return _logs_client


def _cw():
    global _cw_client
    if _cw_client is None:
        _cw_client = boto3.client("cloudwatch")
    return _cw_client


def _safe(fn, *args, **kwargs) -> str:
    """Run a tool body, returning JSON text or an error string (never raises)."""
    try:
        result = fn(*args, **kwargs)
        return json.dumps(result, default=str)[:6000]
    except Exception as exc:  # noqa: BLE001 - tools must not crash the graph
        logger.warning("tool %s failed: %s", getattr(fn, "__name__", "?"), exc)
        return f"ERROR: {type(exc).__name__}: {exc}"


# ── CloudWatch logs ─────────────────────────────────────────────────────────

@tool
def search_pod_logs(query: str, minutes: int = 30) -> str:
    """Search the cluster's pod-log group for a substring over the last N minutes.

    Use to find related errors, stack traces, or other pods hitting the same
    issue. `query` is a plain substring (e.g. a pod name or error phrase).
    Returns up to 30 matching log lines as JSON.
    """
    def _run():
        start = int((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp() * 1000)
        resp = _logs().filter_log_events(
            logGroupName=LOG_GROUP_NAME,
            filterPattern=f'"{query}"',
            startTime=start,
            limit=30,
        )
        return [e["message"][:500] for e in resp.get("events", [])]

    return _safe(_run)


@tool
def get_log_context(log_stream: str, limit: int = 20) -> str:
    """Fetch the most recent log lines from a specific pod log stream.

    Use after you know the exact stream name to read surrounding context.
    """
    def _run():
        resp = _logs().get_log_events(
            logGroupName=LOG_GROUP_NAME,
            logStreamName=log_stream,
            limit=limit,
            startFromHead=False,
        )
        return [e["message"][:500] for e in resp.get("events", [])]

    return _safe(_run)


# ── Live Kubernetes API ─────────────────────────────────────────────────────

@tool
def k8s_get_pod(namespace: str, pod: str) -> str:
    """Get a live pod's status: phase, conditions, and per-container restart
    counts / last-terminated state (exit codes, OOM reasons). Best signal for
    crash loops and OOM kills."""
    return _safe(k8s.get_pod, namespace, pod)


@tool
def k8s_list_events(namespace: str) -> str:
    """List recent Kubernetes Events in a namespace (Warnings first). This is the
    authoritative source for scheduling failures, evictions, image-pull errors,
    volume-mount failures and probe failures, which are Events rather than pod
    stdout."""
    return _safe(k8s.list_events, namespace)


@tool
def k8s_list_pods(namespace: str) -> str:
    """List pods in a namespace with their phase and total restart count, to see
    whether an issue is isolated to one pod or cluster-wide."""
    return _safe(k8s.list_pods, namespace)


@tool
def k8s_describe_node(node: str) -> str:
    """Get a node's conditions (DiskPressure/MemoryPressure/PIDPressure/Ready)
    plus capacity and allocatable, to confirm node-level resource exhaustion."""
    return _safe(k8s.describe_node, node)


# ── CloudWatch metrics (Container Insights) ─────────────────────────────────

@tool
def get_metric(target: str, metric: str, minutes: int = 30, namespace: str = "") -> str:
    """Query a Container Insights metric over the last N minutes.

    `metric` examples: pod_memory_utilization, pod_cpu_utilization,
    pod_number_of_container_restarts, node_filesystem_utilization,
    node_memory_utilization. `target` is the pod or node name; `namespace` is
    required for pod_* metrics. Use to confirm resource-exhaustion theories with
    actual numbers. Returns the max/avg datapoints as JSON.
    """
    def _run():
        is_pod = metric.startswith("pod_")
        dims = [{"Name": "ClusterName", "Value": EKS_CLUSTER_NAME}]
        if is_pod:
            dims.append({"Name": "PodName", "Value": target})
            if namespace:
                dims.append({"Name": "Namespace", "Value": namespace})
        else:
            dims.append({"Name": "NodeName", "Value": target})

        resp = _cw().get_metric_data(
            MetricDataQueries=[{
                "Id": "m1",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "ContainerInsights",
                        "MetricName": metric,
                        "Dimensions": dims,
                    },
                    "Period": 300,
                    "Stat": "Maximum",
                },
            }],
            StartTime=datetime.now(timezone.utc) - timedelta(minutes=minutes),
            EndTime=datetime.now(timezone.utc),
        )
        r = resp["MetricDataResults"][0]
        values = r.get("Values", [])
        return {
            "metric": metric,
            "target": target,
            "datapoints": len(values),
            "max": max(values) if values else None,
            "latest": values[0] if values else None,
        }

    return _safe(_run)


# ── ArgoCD correlation ──────────────────────────────────────────────────────

@tool
def get_argocd_history(app: str) -> str:
    """Get an ArgoCD application's current sync/health status and recent
    deployment history, to determine whether a recent deploy correlates with the
    incident's onset."""
    return _safe(argocd.get_recent_history, app)


def get_tools() -> list:
    """Assemble the active tool list, dropping unconfigured capabilities."""
    tools = [
        search_pod_logs,
        get_log_context,
        k8s_get_pod,
        k8s_list_events,
        k8s_list_pods,
        k8s_describe_node,
        get_metric,
    ]
    if argocd.is_enabled():
        tools.append(get_argocd_history)
    return tools
