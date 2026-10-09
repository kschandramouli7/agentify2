"""ColdServicesSkill — handles the `cold_services` intent (ROADMAP P31
phase 2, ADR 0038).

Deterministic only — zero Claude calls, same reasoning DependencyGraphSkill
already applies: "which services have gone quiet" is a threshold query over
data already sitting in Postgres (service_dependencies.last_seen,
scan_coverage.last_scan), not something a model needs to reason about.

Reuses fetch_cold_services (k8fy/service_topology.py) rather than building
its own HTTP call, same "one function, two entry points" posture
DependencyGraphSkill's own docstring describes for _build_service_graph.
"""

import logging
from typing import Any, Dict, Optional

from k8fy.agent import K8fyAgent
from models.response import AgentResponse

logger = logging.getLogger(__name__)


def _cold_services_answer(namespace: str, cold: list) -> str:
    """Prose for a cold-services report. Deliberately states the two
    thresholds it used (RELEVANT to decommissioning — a reader needs to
    know how stale/how-recently-scanned "cold" meant here) and the lower-
    bound caveat every evidence-based answer in this codebase carries: this
    is mined log evidence, not a guarantee nothing else depends on it.
    """
    if not cold:
        return (
            f"No services in {namespace} currently look cold — nothing scanned recently "
            "has gone quiet for longer than the stale threshold.\n\n"
            "This is a lower bound, same as every mined-evidence answer here: it reflects "
            "what the scanner has sampled, not a certainty that nothing was missed."
        )
    lines = []
    for svc in cold:
        service = svc.get("service", "?")
        last_seen = svc.get("last_seen")
        if last_seen:
            lines.append(f"- {service}: last evidence at {last_seen}")
        else:
            lines.append(f"- {service}: no evidence has ever been mined for it")
    body = "\n".join(lines)
    return (
        f"{len(cold)} service{'s' if len(cold) != 1 else ''} in {namespace} look cold "
        f"— scanned recently, with no recent (or, for some, any) call-graph evidence:\n\n"
        f"{body}\n\n"
        "This is a candidate list, not a decommission order: it reflects mined log "
        "evidence only, and cannot see traffic the scanner never sampled."
    )


class ColdServicesSkill(K8fyAgent):
    """Decommission-candidate reporter — deterministic, no Claude call."""

    def __init__(self) -> None:
        # system_prompt="" for the same reason DependencyGraphSkill uses it:
        # this skill never reaches the model, so resolving a prompt here
        # would be a network call for text nothing reads.
        super().__init__(system_prompt="", tools=[])

    async def reason(
        self, intent: str, data: Dict[str, Any], context: Optional[Dict[str, Any]] = None
    ) -> AgentResponse:
        if context is None:
            context = {}

        namespace = context.get("namespace") or data.get("namespace")
        if not namespace:
            return AgentResponse(
                answer="Which namespace? I need one to check for cold services.",
                status="ok",
                confidence=1.0,
                sources=[],
                tool_calls=[],
                details={},
                tier="tier1",
            )

        # Local import (not module-level): mirrors DependencyGraphSkill's own
        # _build_service_graph import pattern (k8fy/agent.py) — a
        # module-level `from k8fy.service_topology import fetch_cold_services`
        # binds the name at import time, so a test's
        # monkeypatch.setattr("k8fy.service_topology.fetch_cold_services", ...)
        # would silently miss it; resolving it fresh per call picks up the
        # patched module attribute.
        from k8fy.service_topology import fetch_cold_services

        try:
            cold = await fetch_cold_services(namespace, self.backend_url)
        except Exception as e:  # noqa: BLE001
            logger.warning("cold_services skill: fetch failed: %s", e)
            cold = []

        answer = _cold_services_answer(namespace, cold)
        logger.info(
            "cold_services skill answered deterministically: namespace=%s cold_count=%d",
            namespace, len(cold),
        )
        return AgentResponse(
            answer=answer,
            status="ok",
            confidence=1.0,
            sources=["service_dependencies", "scan_coverage"],
            tool_calls=[],
            details={"namespace": namespace, "cold_services": cold},
            tier="tier1",
        )
