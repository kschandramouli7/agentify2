"""service_topology.py — mine a service-dependency graph out of log text and
push it to the multi-tenant Hub (ADR 0022 / ROADMAP P18 use case #2).

`extract_service_mentions` is copied unchanged from
src/agent/k8fy/service_topology.py (see that module's docstring for the
precision-over-recall rationale) — the mining LOGIC doesn't get rebuilt, per
Decision #6, just re-hosted here against a portable log source.

`push_dependency` differs from the original `upsert_service_dependency` in
one deliberate way: it sends `Authorization: Bearer {collector_token}`. The
original has no auth header at all (it's called in-process by the agent,
which has no credential to present) — this is the whole reason
agentify-discovery exists: to be a real, tenant-scoped caller of the
already-built ingest path.
"""

import logging
import re
from typing import Set

import httpx

logger = logging.getLogger(__name__)

# <label>.<label> optionally followed by the K8s in-cluster DNS suffix.
# Permissive on purpose — cross-validation against known_services/namespace
# (not this regex) is what keeps false positives out.
_HOSTNAME_RE = re.compile(
    r"\b([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)(?:\.svc\.cluster\.local)?\b"
)


def extract_service_mentions(log_text: str, namespace: str, known_services: Set[str]) -> Set[str]:
    """Find candidate `<service>.<namespace>[.svc.cluster.local]` mentions in
    log text. A match only counts if the namespace segment equals `namespace`
    AND the service segment is in `known_services` — real ground truth, not
    just regex-shaped text. Returns the set of validated service names
    (never includes `namespace` itself, never raises on malformed input).
    """
    if not log_text or not known_services:
        return set()

    found: Set[str] = set()
    for service_candidate, namespace_candidate in _HOSTNAME_RE.findall(log_text):
        if namespace_candidate == namespace and service_candidate in known_services:
            found.add(service_candidate)
    return found


async def push_dependency(
    namespace: str, from_service: str, to_service: str, backend_url: str, collector_token: str,
) -> None:
    """Record one piece of evidence for a from->to edge via the tenant-scoped
    ingest endpoint. Best-effort: any failure is logged and swallowed — one
    dropped scan cycle never blocks the next.
    """
    # Omit the header entirely when unset — see push_inventory's identical
    # comment (inventory.py) for why.
    headers = {"Authorization": f"Bearer {collector_token}"} if collector_token else {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{backend_url.rstrip('/')}/api/service-dependencies",
                json={"namespace": namespace, "from_service": from_service, "to_service": to_service},
                headers=headers,
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("push_dependency failed for %s/%s->%s: %s", namespace, from_service, to_service, e)
