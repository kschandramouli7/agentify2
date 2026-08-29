"""CertAuditSkill — Pattern A skill for cert_check intent (spec 010).

Pattern A: always pre-fetches the full certificate list for the namespace
before calling Claude. The cert list is the only data source this skill needs,
its parameters are fully known from context alone, and it never varies by what
the initial data contains — making it the cleanest Pattern A candidate.

Pre-fetch sequence:
  1. get_certificates(namespace)  — always, unconditionally
  2. Fleet-cluster scoping (ROADMAP P16 / ADR 0023/0024): if a service_name
     is present, resolve which cluster(s) run it via the Hub's
     cluster_services registry, and prefetch a LIVE cert check
     (live_get_certificates, ROADMAP P18 use case #9) per resolved cluster —
     agentify-discovery reads kubernetes.io/tls Secrets directly, giving a
     fresher signal than the ingested get_certificates snapshot for a fleet
     cluster. A no-op for deployments with no registered fleet clusters.
"""

import asyncio
import logging
from typing import Any, Dict

from k8fy.agent import K8fyAgent
from k8fy.prompts import CERT_AUDIT_PROMPT
from k8fy.service_topology import resolve_service_clusters
from k8fy.tools import TOOLS
from models.response import AgentResponse

_CERT_TOOLS = [t for t in TOOLS if t["name"] in {"get_certificates", "live_get_certificates"}]

logger = logging.getLogger(__name__)


class CertAuditSkill(K8fyAgent):
    """PKI/TLS lifecycle expert — Pattern A: pre-fetch certs + single Claude call."""

    def __init__(self) -> None:
        super().__init__(
            prompt_name="k8fy/cert-audit",
            prompt_fallback=CERT_AUDIT_PROMPT,
            tools=_CERT_TOOLS,
        )

    async def reason(
        self, intent: str, data: Dict[str, Any], context: Dict[str, Any] | None = None
    ) -> AgentResponse:
        if context is None:
            context = {}
        prefetched = await self._prefetch(context)
        return await self._reason_pattern_a(intent, data, context, prefetched)

    async def _prefetch(self, context: Dict[str, Any]) -> Dict[str, Any]:
        namespace = context.get("namespace", "default")
        service_name = context.get("service_name") or context.get("service")
        tasks: Dict[str, Any] = {
            "certificates": self._fetch("get_certificates", {"namespace": namespace}),
        }

        if service_name:
            for cluster_id in await resolve_service_clusters(namespace, service_name, self.backend_url):
                tasks[f"live_certificates.{cluster_id}"] = self._fetch(
                    "live_get_certificates", {"namespace": namespace, "cluster_id": cluster_id},
                )

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        prefetched: Dict[str, Any] = {}
        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("cert audit prefetch failed for %s: %s", key, result)
            else:
                prefetched[key] = result
        return prefetched
