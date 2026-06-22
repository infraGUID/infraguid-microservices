"""CS-02 Kubernetes Log Intelligence Agent — Lambda entrypoint.

Event-triggered: a CloudWatch Logs subscription filter invokes this function
ONLY on pod-log lines matching the anomaly pattern. For each detected incident a
LangGraph ReAct agent (Bedrock Amazon Nova Pro) investigates read-only across CloudWatch
logs, the live Kubernetes API, metrics, and ArgoCD, classifies severity (P1-P4),
and publishes a notification to SNS that PROPOSES a remediation for human
approval. Notify-only; the agent never mutates the cluster.
"""
from __future__ import annotations

import base64
import gzip
import json
import logging

from agent import run_agent
from collect import enrich_context, extract_incidents
from notify import publish_alert

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):  # noqa: ANN001, ARG001
    data = event.get("awslogs", {}).get("data")
    if not data:
        logger.warning("Event has no awslogs.data; ignoring")
        return {"status": "ignored"}

    decoded = json.loads(gzip.decompress(base64.b64decode(data)))
    if decoded.get("messageType") != "DATA_MESSAGE":
        return {"status": "skipped", "reason": decoded.get("messageType")}

    log_group = decoded.get("logGroup", "")
    incidents = extract_incidents(decoded.get("logEvents", []), decoded.get("logStream", ""))
    if not incidents:
        logger.info("No recognised anomalies in %d events", len(decoded.get("logEvents", [])))
        return {"status": "no_incident"}

    alerted = []
    for incident in incidents:
        try:
            enrich_context(incident, log_group)
            summary = run_agent(incident)
            publish_alert(summary, incident)
            alerted.append({"pod": incident["pod"], "severity": summary["severity"]})
        except Exception:  # noqa: BLE001 - isolate failures per incident
            logger.exception("Failed to process incident for pod %s", incident.get("pod"))

    return {"status": "alerted", "count": len(alerted), "incidents": alerted}
