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
from typing import Dict, List, Set

import httpx

logger = logging.getLogger(__name__)

# <label>.<label> optionally followed by the K8s in-cluster DNS suffix.
# Permissive on purpose — cross-validation against known_services/namespace
# (not this regex) is what keeps false positives out.
_HOSTNAME_RE = re.compile(
    r"\b([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)(?:\.svc\.cluster\.local)?\b"
)

# Bare in-cluster hostnames, e.g. "http://agentify-backend:8080".
#
# Kubernetes resolves a short service name through the pod's search domain, so
# in-cluster callers almost never write the FQDN. That meant this miner could
# only ever see dependencies logged in a form nobody actually uses: confirmed
# 2026-09-01, the agentify namespace's own services address each other as
# "http://agentify-backend:8080" and produced ZERO edges, while the payments
# namespace produced five only because its test workloads were written to log
# FQDNs on purpose.
#
# A bare name counts ONLY in a hostname context — immediately after "//", or
# immediately before ":<port>" — because a service named "payment" would
# otherwise match the word "payment" in ordinary log prose. Validation against
# the live Service list is the second guard. The boundary is pinned by
# test_extract_rejects_bare_unqualified_mention: "payment-backend restarted due
# to OOMKilled" must still yield nothing.
#
# A bare name is namespace-local by construction (that is what the search domain
# does), so checking it against the scanned namespace's own Service list is
# exactly the right test.
_URL_HOST_RE = re.compile(r"//([a-z0-9][a-z0-9.-]*)")
_HOST_PORT_RE = re.compile(r"(?<![\w.-])([a-z0-9][a-z0-9-]*):(\d{2,5})(?![\w.])")


def extract_service_mentions(log_text: str, namespace: str, known_services: Set[str]) -> Set[str]:
    """Find candidate service hostnames in log text, in two forms:

      - qualified — `<service>.<namespace>[.svc.cluster.local]`, counted only
        when the namespace segment equals `namespace`;
      - bare — a short name in a hostname context (`//<name>` or
        `<name>:<port>`), which Kubernetes resolves within the pod's own
        namespace. This is the form in-cluster callers actually use.

    Either way the name must be in `known_services` — real ground truth, not
    just regex-shaped text. Returns the set of validated service names (never
    includes `namespace` itself, never raises on malformed input).
    """
    if not log_text or not known_services:
        return set()

    found: Set[str] = set()
    for service_candidate, namespace_candidate in _HOSTNAME_RE.findall(log_text):
        if namespace_candidate == namespace and service_candidate in known_services:
            found.add(service_candidate)

    # Bare short names, hostname contexts only (see the regexes above).
    for host in _URL_HOST_RE.findall(log_text):
        if "." in host:
            continue  # dotted form — the qualified pass above already ruled on it
        if host in known_services:
            found.add(host)
    for name, _port in _HOST_PORT_RE.findall(log_text):
        if name in known_services:
            found.add(name)

    return found


async def push_scan_coverage(
    namespace: str,
    stats: Dict[str, Dict[str, int]],
    backend_url: str,
    collector_token: str,
) -> None:
    """Report one scan cycle's accounting for a namespace (ROADMAP P27 phase 1).

    This is the DENOMINATOR for the edges push_dependency records.
    `evidence_count` alone cannot distinguish a service that is called rarely
    from one whose logs are unreadable from one whose pods are never among the
    MAX_PODS_PER_NAMESPACE sampled — the ambiguity that made payment-worker's
    decline uninterpretable on 2026-09-03.

    One request per namespace, not per service: a scan produces a single report
    covering everything it looked at, and the Hub reads them together.

    Best-effort, like every other push here — a dropped report costs one cycle
    of denominator, never a scan.
    """
    if not stats:
        return
    # Omit the header entirely when unset — same reason as push_dependency.
    headers = {"Authorization": f"Bearer {collector_token}"} if collector_token else {}
    payload = {
        "namespace": namespace,
        "services": [{"service": name, **counts} for name, counts in sorted(stats.items())],
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{backend_url.rstrip('/')}/api/scan-coverage", json=payload, headers=headers,
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("push_scan_coverage failed for namespace=%s: %s", namespace, e)


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
