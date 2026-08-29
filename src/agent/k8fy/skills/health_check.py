"""HealthSkill — Pattern A skill for health_check intent (spec 010).

Pattern A: derives which tool calls are needed from the initial data and
context, fires them in parallel, then makes a single Claude call with the
assembled result. No agentic tool-calling loop.

Pre-fetch sequence:
  1. get_service_health(service_name, namespace)  — if service_name is known
  2. get_pod_events(pod_id, namespace)            — for every pod in the initial
     data that already shows restarts > 0 or ready=False
  3. Fleet-cluster scoping (ROADMAP P16 / ADR 0023/0024): resolve which
     cluster(s) run this service, and if any, prefetch a cluster-scoped
     get_service_health (ADR 0024) plus a live snapshot via live_list_pods
     (ROADMAP P18 use case #9) per resolved cluster — same pattern as
     DiagnoseSkill. A no-op for deployments with no registered fleet
     clusters (resolution returns []).

These are the only two cases where the agentic loop would have called extra
tools anyway. Pre-fetching them in parallel before the Claude call saves the
round-trip cost of asking Claude to request them.
"""

import asyncio
import logging
from typing import Any, Dict, List

from k8fy.agent import K8fyAgent, HEALTH_REASONING_SCHEMA
from k8fy.prompts import HEALTH_SKILL_PROMPT
from k8fy.service_topology import resolve_service_clusters
from k8fy.tools import TOOLS
from models.response import AgentResponse

_HEALTH_TOOLS = [
    t for t in TOOLS if t["name"] in {"get_service_health", "query_pod", "get_pod_events"}
]

logger = logging.getLogger(__name__)


class HealthSkill(K8fyAgent):
    """K8s health model expert — Pattern A: parallel pre-fetch + single Claude call."""

    def __init__(self) -> None:
        super().__init__(
            prompt_name="k8fy/health-check",
            prompt_fallback=HEALTH_SKILL_PROMPT,
            tools=_HEALTH_TOOLS,
            output_schema=HEALTH_REASONING_SCHEMA,
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
        """Fire all predictable tool calls in parallel before the Claude call."""
        namespace = context.get("namespace", "default")
        tasks: Dict[str, Any] = {}

        # Service health — fetch when the caller identified a specific service.
        service_name = context.get("service_name")
        if service_name:
            tasks["service_health"] = self._fetch(
                "get_service_health",
                {"service_name": service_name, "namespace": namespace},
            )

        # Pod events — only for pods that already look degraded in the initial
        # data; healthy pods rarely have useful events and fetching them wastes
        # tokens and latency.
        for pod_id in _degraded_pod_ids(data):
            tasks[f"events.{pod_id}"] = self._fetch(
                "get_pod_events",
                {"pod_id": pod_id, "namespace": namespace},
            )

        # Fleet-cluster scoping (ROADMAP P16 / ADR 0023/0024) — same pattern
        # as DiagnoseSkill: every resolved cluster gets a cluster-scoped
        # get_service_health and a live_list_pods snapshot, routed through
        # the existing self._fetch()/process_tool_call path. No-op when no
        # fleet clusters are registered for this service.
        if service_name:
            for cluster_id in await resolve_service_clusters(namespace, service_name, self.backend_url):
                tasks[f"service_health.{cluster_id}"] = self._fetch(
                    "get_service_health",
                    {"service_name": service_name, "namespace": namespace, "cluster_id": cluster_id},
                )
                tasks[f"live_pods.{cluster_id}"] = self._fetch(
                    "live_list_pods", {"namespace": namespace, "cluster_id": cluster_id},
                )

        if not tasks:
            return {}

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        prefetched = {}
        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("prefetch failed for %s: %s", key, result)
            else:
                prefetched[key] = result
        return prefetched


def _degraded_pod_ids(data: Dict[str, Any]) -> List[str]:
    """Return keys from data whose values indicate a pod with visible issues.

    Treats the data value as a pod-state dict and picks up restarts > 0 or
    ready=False — the two signals that make get_pod_events worth fetching.
    """
    pod_ids = []
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        if val.get("restarts", 0) > 0 or val.get("ready") is False:
            pod_ids.append(key)
    return pod_ids
