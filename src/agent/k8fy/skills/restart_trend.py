"""RestartTrendSkill — Pattern A: pre-fetch restart metrics, one Claude call (spec 010).

Pre-fetch sequence:
  1. get_metrics_history(namespace, service_name, order=asc) — always, unconditionally.
  2. Fleet-cluster scoping (ROADMAP P16 / ADR 0023/0024): if service_name is
     present, resolve which cluster(s) run it via the Hub's cluster_services
     registry, and add a cluster-scoped get_metrics_history per resolved
     cluster — no live equivalent exists for restart metrics, so this is the
     ingested-data path only (ADR 0024's cluster_id support on
     get_metrics_history). A no-op for deployments with no registered fleet
     clusters.

Restart time-series is the single data source for this intent; its parameters are
fully known from context, so pre-fetching before the Claude call is safe and
eliminates the agentic tool-call round-trip entirely.

Cost: 1 backend fetch (+ 1 per resolved fleet cluster) + exactly 1 Claude call.
"""

import asyncio
import logging
from typing import Any, Dict

from k8fy.agent import K8fyAgent
from k8fy.prompts import RESTART_TREND_PROMPT
from k8fy.service_topology import resolve_service_clusters
from k8fy.tools import TOOLS
from models.response import AgentResponse

_TOOLS = [t for t in TOOLS if t["name"] in {"get_metrics_history"}]

logger = logging.getLogger(__name__)


class RestartTrendSkill(K8fyAgent):
    """Restart-trend analyst — Pattern A: unconditional pre-fetch + single Claude call."""

    def __init__(self) -> None:
        super().__init__(
            prompt_name="k8fy/restart-trend",
            prompt_fallback=RESTART_TREND_PROMPT,
            tools=_TOOLS,
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
        """Fetch restart time-series unconditionally — it is always the sole data source."""
        namespace = context.get("namespace", "default")
        service_name = context.get("service_name") or context.get("service")
        args: Dict[str, Any] = {"namespace": namespace, "order": "asc"}
        if service_name:
            args["service_name"] = service_name

        tasks: Dict[str, Any] = {"metrics_history": self._fetch("get_metrics_history", args)}

        if service_name:
            for cluster_id in await resolve_service_clusters(namespace, service_name, self.backend_url):
                tasks[f"metrics_history.{cluster_id}"] = self._fetch(
                    "get_metrics_history", {**args, "cluster_id": cluster_id},
                )

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        prefetched: Dict[str, Any] = {}
        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("metrics_history prefetch failed for %s: %s", key, result)
            else:
                prefetched[key] = result
        return prefetched
