"""VaultCertSkill — handles vault_cert and renew_cert intents.

For vault_cert (status check):
  Pre-fetches Vault PKI cert status + K8s cert list, then makes one Claude
  call to assess health and decide if rotation is needed.

For renew_cert (on-demand renewal from UI button or chat):
  Directly calls rotate_vault_cert with the correct parameters (pki_mount,
  pki_role, common_name, k8s_secret_name, k8s_namespace, ttl=24h) without
  an LLM call — the renewal parameters are deterministic from the context.
  Returns a structured response the frontend Renew button can display.
"""

import asyncio
import logging
from typing import Any, Dict

from k8fy.agent import K8fyAgent
from k8fy.prompts import VAULT_CERT_PROMPT
from k8fy.service_topology import resolve_service_clusters
from k8fy.tools import TOOLS, process_tool_call
from models.response import AgentResponse

logger = logging.getLogger(__name__)

_VAULT_TOOLS = [
    t for t in TOOLS
    if t["name"] in {
        "get_vault_cert_status",
        "rotate_vault_cert",
        "get_certificates",
        "get_service_health",
    }
]

# Maps namespace/service → renewal parameters. Extend as more services are added.
#
# Confirmed live Vault configuration (kubectl exec vault-0 -- vault list pki-payments/roles):
#   Mount:  pki-payments
#   Roles:  payment-api   (for payment and payment-api services)
#           payment-worker (for payment-worker)
#
_RENEW_CONFIG: Dict[str, Dict[str, str]] = {
    "payments/payment":        {"pki_mount": "pki-payments", "pki_role": "payment-api",    "common_name": "payment.payments.svc.cluster.local",        "k8s_secret": "payment-tls", "k8s_ns": "payments"},
    "payments/payment-api":    {"pki_mount": "pki-payments", "pki_role": "payment-api",    "common_name": "payment-api.payments.svc.cluster.local",    "k8s_secret": "payment-tls", "k8s_ns": "payments"},
    "payments/payment-worker": {"pki_mount": "pki-payments", "pki_role": "payment-worker", "common_name": "payment-worker.payments.svc.cluster.local", "k8s_secret": "payment-tls", "k8s_ns": "payments"},
}

_DEFAULT_TTL = "24h"


class VaultCertSkill(K8fyAgent):
    """Vault PKI certificate monitoring and on-demand renewal."""

    def __init__(self) -> None:
        super().__init__(
            prompt_name="k8fy/vault-cert",
            prompt_fallback=VAULT_CERT_PROMPT,
            tools=_VAULT_TOOLS,
        )

    async def reason(
        self, intent: str, data: Dict[str, Any], context: Dict[str, Any] | None = None
    ) -> Any:
        if context is None:
            context = {}

        # On-demand renewal — deterministic, no LLM call needed.
        if intent == "renew_cert" or context.get("action") == "renew":
            return await self._renew(context)

        # Status check — Pattern A with prefetch.
        prefetched = await self._prefetch(data, context)
        return await self._reason_pattern_a(intent, data, context, prefetched)

    async def _renew(self, context: Dict[str, Any]) -> AgentResponse:
        """Issue a fresh cert from Vault and update the K8s Secret immediately."""
        namespace = context.get("namespace", "payments")
        service   = context.get("service", "payment")
        key       = f"{namespace}/{service}"

        cfg = _RENEW_CONFIG.get(key) or _RENEW_CONFIG.get(f"{namespace}/payment")
        if not cfg:
            return AgentResponse(
                answer=f"No renewal config found for {key}. Supported: {', '.join(_RENEW_CONFIG)}",
                status="error",
                confidence=0.0,
            )

        result = await process_tool_call(
            "rotate_vault_cert",
            {
                "pki_mount":       cfg["pki_mount"],
                "pki_role":        cfg["pki_role"],
                "common_name":     cfg["common_name"],
                "ttl":             context.get("ttl", _DEFAULT_TTL),
                "k8s_secret_name": cfg["k8s_secret"],
                "k8s_namespace":   cfg["k8s_ns"],
            },
            self.backend_url,
        )

        if "error" in result:
            return AgentResponse(answer=result["error"], status="error", confidence=0.0)

        return AgentResponse(
            answer=result.get("message", "Certificate renewed successfully."),
            status="ok",
            confidence=1.0,
            details={
                "serial":              result.get("serial"),
                "ttl":                 result.get("ttl"),
                "pki_role":            result.get("pki_role"),
                "k8s_secret_updated":  result.get("k8s_secret_updated"),
                "k8s_secret":          result.get("k8s_secret"),
                # New cert metadata — lets the backend update current data
                # immediately without waiting for the next adapter scrape cycle.
                "expires_at":          result.get("expires_at", ""),
                "days_until_expiry":   result.get("days_until_expiry", 0),
                "dns_names":           result.get("dns_names", []),
                "common_name":         result.get("common_name", ""),
                "namespace":           cfg.get("k8s_ns", ""),
                "secret_name":         cfg.get("k8s_secret", ""),
            },
        )

    async def _prefetch(
        self, data: Dict[str, Any], context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Fetch Vault cert status and K8s cert status in parallel.

        Fleet-cluster scoping (ROADMAP P16 / ADR 0023/0024) only applies to
        the K8s-cert half (get_certificates, ADR 0024's cluster_id support) —
        NOT to get_vault_cert_status. Vault is addressed by a single
        VAULT_ADDR this agent process is configured with; there is no
        per-cluster Vault routing concept anywhere (no live_get_vault_status
        tool, no cluster_id support on the Hub's Vault-facing code), so a
        resolved fleet cluster's Vault instance (if it even has one) can't be
        reached this way. Flagged rather than silently assumed away — a real
        gap if Vault-backed cert monitoring needs to span a fleet.
        """
        namespace = context.get("namespace", "payments")
        service_name = context.get("service_name") or context.get("service")
        pki_role  = context.get("pki_role", data.get("pki_role", "payment-api"))
        kv_path   = context.get("kv_path",  data.get("kv_path",  "secret/data/payments/tls"))

        tasks = {
            "vault_cert": self._fetch("get_vault_cert_status", {"pki_role": pki_role, "kv_path": kv_path}),
            "k8s_certs":  self._fetch("get_certificates", {"namespace": namespace}),
        }

        if service_name:
            for cluster_id in await resolve_service_clusters(namespace, service_name, self.backend_url):
                tasks[f"k8s_certs.{cluster_id}"] = self._fetch(
                    "get_certificates", {"namespace": namespace, "cluster_id": cluster_id},
                )

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        prefetched: Dict[str, Any] = {}
        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("vault_cert prefetch failed for %s: %s", key, result)
            else:
                prefetched[key] = result
        return prefetched
