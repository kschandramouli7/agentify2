"""IncidentResponderSkill — handles incident_respond intent (spec 011 Use Case 1).

Pattern A: proposes ONE remediation action for an already-diagnosed incident.
This call makes zero infrastructure writes — it only produces a proposal that
a human reviews and explicitly approves (ADR 0020). Execution happens later,
via a separate call routed through RemediationExecutorSkill after approval.
"""

import asyncio
import logging
from typing import Any, Dict

from k8fy.agent import K8fyAgent, REMEDIATION_REASONING_SCHEMA
from k8fy.prompts import INCIDENT_RESPONDER_PROMPT
from k8fy.skills.diagnose import _crashing_pod_ids
from k8fy.tools import TOOLS
from models.response import AgentResponse

_INCIDENT_RESPONDER_TOOLS = [
    t for t in TOOLS
    if t["name"] in {"get_similar_incidents", "get_change_history", "get_logs"}
]

logger = logging.getLogger(__name__)


class IncidentResponderSkill(K8fyAgent):
    """Proposes a Phase-3 remediation action — never executes one (ADR 0020)."""

    def __init__(self) -> None:
        super().__init__(
            prompt_name="k8fy/incident-responder",
            prompt_fallback=INCIDENT_RESPONDER_PROMPT,
            tools=_INCIDENT_RESPONDER_TOOLS,
            output_schema=REMEDIATION_REASONING_SCHEMA,
        )

    async def reason(
        self, intent: str, data: Dict[str, Any], context: Dict[str, Any] | None = None
    ) -> AgentResponse:
        if context is None:
            context = {}
        prefetched = await self._prefetch(data, context)
        return await self._reason_pattern_a(intent, data, context, prefetched)

    async def _prefetch(
        self, data: Dict[str, Any], context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Fire all predictable evidence-gathering tool calls in parallel."""
        namespace = context.get("namespace", "default")
        service_name = context.get("service") or context.get("service_name")
        tasks: Dict[str, Any] = {}

        if service_name:
            tasks["similar_incidents"] = self._fetch(
                "get_similar_incidents",
                {
                    "namespace": namespace,
                    "service": service_name,
                    "description": f"service health issues in {namespace}/{service_name}",
                    "limit": 3,
                },
            )
            tasks["change_history"] = self._fetch(
                "get_change_history",
                {"namespace": namespace, "deployment": service_name},
            )

        # get_logs — reads the Glue/Athena test harness first when configured,
        # falling back to the live cluster — still a deterministic function
        # call, not an LLM decision (log_router.py).
        for pod_id in _crashing_pod_ids(data):
            tasks[f"logs.{pod_id}"] = self._fetch(
                "get_logs",
                {"namespace": namespace, "pod": pod_id, "previous": True},
            )

        if not tasks:
            return {}

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        prefetched: Dict[str, Any] = {}
        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("incident_responder prefetch failed for %s: %s", key, result)
            else:
                prefetched[key] = result
        return prefetched
