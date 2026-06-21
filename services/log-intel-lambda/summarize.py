"""Evidence formatting and heuristic fallback for the Log Intelligence Agent.

The LangGraph ReAct agent (`agent.py`) is the primary analysis path. This module
provides two pieces it depends on:
  * `build_evidence`   — flatten an incident's captured log lines into a prompt block.
  * `heuristic_summary`— a deterministic report used when the agent is unavailable,
                         so an alert is always produced (never fail on LLM errors).
"""
from __future__ import annotations


def build_evidence(incident: dict) -> str:
    """Flatten captured + context log lines into a de-duped, capped evidence block."""
    lines = incident.get("samples", []) + incident.get("context_lines", [])
    seen, out = set(), []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(f"- {line}")
        if len(out) >= 25:
            break
    return "\n".join(out) or "(no log lines captured)"


def heuristic_summary(incident: dict) -> dict:
    """Deterministic report used when the agent cannot run."""
    return {
        "severity": incident["default_severity"],
        "root_cause": f"{incident['error_type']} detected",
        "summary": (
            f"{incident['error_type']} in pod {incident['pod']} "
            f"(namespace {incident['namespace']}). Review recent deploy."
        ),
        "suggested_fix": "Inspect pod events and recent rollout; consider rollback.",
    }
