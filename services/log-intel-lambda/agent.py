"""LangGraph ReAct agent that investigates a Kubernetes incident.

Replaces the previous single-shot Bedrock call. The agent (Amazon Nova Pro via
`ChatBedrockConverse`) reasons and calls read-only tools — CloudWatch logs, the
live Kubernetes API, CloudWatch metrics, and ArgoCD — until it can explain the
incident, then returns a typed `IncidentReport`. It is read-only: it proposes a
remediation command for a human to approve, it never executes one.

Falls back to the heuristic in `summarize.py` if the agent invocation fails, so
an alert is always produced.
"""
from __future__ import annotations

import logging
import os

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

from summarize import build_evidence, heuristic_summary
from tools import get_tools

logger = logging.getLogger()

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")
MAX_AGENT_ITERATIONS = int(os.environ.get("MAX_AGENT_ITERATIONS", "6"))

class IncidentReport(BaseModel):
    """Structured conclusion the agent must return."""

    severity: str = Field(description='One of "P1","P2","P3","P4".')
    root_cause: str = Field(description="One-sentence most-likely root cause.")
    summary: str = Field(description="2-3 sentence human summary of what happened.")
    suggested_fix: str = Field(description="One concrete remediation step in plain English.")
    suggested_command: str = Field(
        description="A single concrete command or runbook link a human can run to "
        "remediate (e.g. 'kubectl rollout undo deploy/x -n y'). Read-only agent: "
        "this is a proposal for human approval, not something you executed."
    )
    investigation_trail: str = Field(
        description="Brief notes on which tools you called and what they showed."
    )

_SYSTEM = (
    "You are InfraGuid's Kubernetes SRE ReAct agent. A pod-log anomaly was "
    "detected after a deployment. Investigate using the available read-only tools "
    "before concluding: inspect pod status, namespace Events (the authoritative "
    "source for scheduling/eviction/image/volume/probe failures), node pressure, "
    "Container Insights metrics, related log lines, and recent ArgoCD deploys. "
    "Form a hypothesis, verify it with at least one tool, and only then conclude. "
    "You are STRICTLY READ-ONLY: never claim you executed, restarted, scaled, or "
    "rolled back anything. In `suggested_command`, output ONE concrete command or "
    "runbook step for a human to approve and run. Be specific and actionable."
)

_SEED = """A Kubernetes anomaly was detected from pod logs.

Error type: {error_type}
Namespace: {namespace}
Pod: {pod}
Container: {container}
Default severity (heuristic): {default_severity}

Initial log evidence:
{evidence}

Investigate and produce the incident report."""

_agent = None

def _get_agent():
    """Build the ReAct agent once per warm container."""
    global _agent
    if _agent is None:
        model = ChatBedrockConverse(model=MODEL_ID, temperature=0, max_tokens=1500)
        _agent = create_react_agent(
            model,
            tools=get_tools(),
            prompt=_SYSTEM,
            response_format=IncidentReport,
        )
    return _agent

def run_agent(incident: dict) -> dict:
    """Investigate an incident and return the report dict. Falls back to the
    heuristic summary if the agent invocation fails — never raises on LLM error."""
    seed = _SEED.format(
        error_type=incident["error_type"],
        namespace=incident["namespace"],
        pod=incident["pod"],
        container=incident["container"],
        default_severity=incident["default_severity"],
        evidence=build_evidence(incident),
    )
    try:
        result = _get_agent().invoke(
            {"messages": [HumanMessage(content=seed)]},
            config={"recursion_limit": MAX_AGENT_ITERATIONS * 2 + 1},
        )
        report: IncidentReport = result["structured_response"]
        return report.model_dump()
    except Exception as exc:  # noqa: BLE001 - never fail the alert on agent errors
        logger.warning("ReAct agent failed, using heuristic: %s", exc)
        return heuristic_summary(incident)
