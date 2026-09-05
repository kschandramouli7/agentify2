"""service_topology.py — mine a service dependency graph out of log text
(service-topology brainstorm, Option 2 — see ROADMAP/ADR 0011).

Diagnosis today is scoped to one service at a time: `DiagnoseSkill` has no
way to know that a symptom in `payment-backend` might actually be caused by
`payment-prod`, one hop downstream. Building a full dependency graph properly
means distributed tracing or a service mesh (ADR 0011 already defers both
until "we need cross-service traces") — expensive, and requires instrumenting
every service. This is the cheap middle ground: scan log text already
flowing through `get_logs()` for K8s-DNS-shaped hostnames that match a
namespace's *real* known services, and record those as directed edges with
an evidence count. No new infrastructure, no app instrumentation — just
reading logs that already exist.

Deliberately precision-over-recall: a bare, unqualified service-name mention
(e.g. a log line saying "payment-backend restarted") is NOT enough evidence —
too many words could coincidentally match a real service name. Only mentions
shaped like `<service>.<namespace>` (optionally `.svc.cluster.local`), where
BOTH the service is a real known service in that namespace AND the namespace
segment matches, count as evidence. This is a deliberate, revisitable
precision/recall tradeoff, not an oversight — expect the graph to be sparse
against apps that don't log outbound hostnames, and expect it to say nothing
rather than something wrong.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

logger = logging.getLogger(__name__)

# <label>.<label> optionally followed by the K8s in-cluster DNS suffix.
# Permissive on purpose — cross-validation against known_services/namespace
# (not this regex) is what keeps false positives out; see module docstring.
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


# ── Beyond the namespace boundary (ROADMAP P27 phase 3) ──────────────────────
#
# extract_service_mentions deliberately returns only in-namespace targets
# validated against the live Service list. That makes every edge it produces
# trustworthy and every diagram built on it a claim about ONE namespace — which
# is false as architecture. The agent calls vault.vault, api.anthropic.com and
# an RDS endpoint; none of those can ever appear.
#
# This function captures the rest, in a SEPARATE and WEAKER trust tier. It is a
# separate function rather than a wider return from the one above because that
# one is duplicated in the agent, imported by the Glue miner, and pinned by 24
# tests; widening it would put every existing edge at risk for the benefit of a
# less certain one.
#
# THE WHOLE DIFFICULTY IS FALSE POSITIVES. A log line is full of dotted strings
# that are not hosts: version numbers ("1.2.3"), Java packages
# ("com.example.Foo"), file names ("config.yaml"), durations, JSON keys. The
# in-cluster miner is safe because it validates against a real Service list;
# there is no equivalent list for the public internet. So the guards are:
#
#   1. hostname CONTEXT only — immediately after "//" or before ":<port>";
#   2. the last label must look like a TLD: alphabetic, at least two chars,
#      which rejects "1.2.3", "config.yaml" (yaml passes shape but see 4),
#      and anything ending in a digit;
#   3. no IP addresses, no localhost, no *.local, no single-label names;
#   4. an explicit deny-list of file-ish and package-ish suffixes that pass the
#      TLD shape test but never name a service we call.
#
# A cross-namespace target is the one case that CAN be validated: its namespace
# segment must be one the Hub actually tracks.
_TLD_RE = re.compile(r"^[a-z]{2,}$")

# Suffixes that satisfy the "looks like a TLD" test and are never a host we
# called. Kept short and specific on purpose — a long speculative list would
# start hiding real egress.
_NOT_A_HOST_SUFFIX = frozenset({
    "yaml", "yml", "json", "log", "txt", "md", "conf", "ini", "toml", "pem",
    "crt", "key", "sql", "py", "go", "ts", "js", "tsx", "html", "css", "sh",
    "jar", "war", "class", "java", "so", "gz", "zip", "tar", "lock", "sum",
})

_PRIVATE_PREFIXES = ("10.", "127.", "192.168.", "169.254.", "0.")


def _looks_like_a_real_host(host: str) -> bool:
    """A conservative "is this a hostname we called" test — see the notes above."""
    if not host or len(host) > 253 or ".." in host:
        return False
    labels = host.split(".")
    if len(labels) < 2 or any(not lab for lab in labels):
        return False
    if not _TLD_RE.match(labels[-1]):
        return False                      # rejects 1.2.3, foo.bar2, trailing digits
    if labels[-1] in _NOT_A_HOST_SUFFIX:
        return False                      # config.yaml, Foo.class, go.sum
    if host.startswith(_PRIVATE_PREFIXES) or host == "localhost":
        return False
    # An all-numeric-label name is an IP, not a host we can name.
    if all(lab.isdigit() for lab in labels[:-1]):
        return False
    return True


def extract_external_mentions(
    log_text: str, namespace: str, known_namespaces: Set[str]
) -> Set[Tuple[str, str]]:
    """Targets OUTSIDE this namespace, as (kind, target) pairs.

    kind is "cross_namespace" for `<service>.<other-tracked-namespace>` — the
    only case here that can be validated, because the namespace must be one the
    Hub tracks — or "external" for a public hostname.

    Weaker evidence than extract_service_mentions by construction: nothing
    validates that a public hostname is a service rather than a string that
    happened to look like one. Callers must keep the two tiers apart.
    """
    if not log_text:
        return set()

    found: Set[Tuple[str, str]] = set()

    # Qualified in-cluster names pointing at a DIFFERENT namespace. The trailing
    # .svc.cluster.local is optional, exactly as in the same-namespace path.
    for service_candidate, namespace_candidate in _HOSTNAME_RE.findall(log_text):
        if namespace_candidate == namespace:
            continue                      # the same-namespace miner owns this
        if namespace_candidate in known_namespaces:
            found.add(("cross_namespace", f"{service_candidate}.{namespace_candidate}"))

    # Public hostnames, in a hostname context only.
    for host in _URL_HOST_RE.findall(log_text):
        h = host.rstrip(".").lower()
        if h.endswith(".svc.cluster.local") or h.endswith(".cluster.local") or h.endswith(".local"):
            continue                      # in-cluster; handled above
        if _looks_like_a_real_host(h):
            found.add(("external", h))

    return found

async def fetch_tracked_pairs(backend_url: str) -> List[str]:
    """Every tracked `"namespace/service"` pair the Hub knows about.

    Same `GET /admin/tracked` endpoint `get_known_services` wraps, without the
    namespace filter — callers that do not yet know the namespace need the whole
    list to resolve one. Degrades to an empty list on any failure, like every
    other best-effort fetch here.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{backend_url.rstrip('/')}/admin/tracked")
            resp.raise_for_status()
            return resp.json() or []
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("fetch_tracked_pairs failed: %s", e)
        return []


async def get_known_services(namespace: str, backend_url: str) -> Set[str]:
    """Real services tracked for this namespace — the ground truth
    `extract_service_mentions` cross-validates candidates against. Wraps the
    existing `GET /admin/tracked` endpoint (entries are `"namespace/service"`
    strings); degrades to an empty set on any failure, same as every other
    best-effort fetch in this codebase.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{backend_url.rstrip('/')}/admin/tracked")
            resp.raise_for_status()
            entries: List[str] = resp.json() or []
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("get_known_services failed for namespace=%s: %s", namespace, e)
        return set()

    services = set()
    for entry in entries:
        ns, _, service = entry.partition("/")
        if ns == namespace and service:
            services.add(service)
    return services


async def upsert_service_dependency(namespace: str, from_service: str, to_service: str, backend_url: str) -> None:
    """Record one piece of evidence for a from->to edge. Best-effort: any
    failure is logged and swallowed — losing one piece of evidence never
    surfaces as a diagnosis error."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{backend_url.rstrip('/')}/api/service-dependencies",
                json={"namespace": namespace, "from_service": from_service, "to_service": to_service},
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning(
            "upsert_service_dependency failed for %s/%s->%s: %s", namespace, from_service, to_service, e
        )


async def fetch_service_dependencies(namespace: str, backend_url: str) -> List[Dict[str, Any]]:
    """Read the namespace's already-mined graph. Degrades to an empty list on
    any failure — a missing graph should never block diagnosis."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{backend_url.rstrip('/')}/api/service-dependencies", params={"namespace": namespace}
            )
            resp.raise_for_status()
            return resp.json() or []
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("fetch_service_dependencies failed for namespace=%s: %s", namespace, e)
        return []


async def resolve_service_clusters(namespace: str, service: str, backend_url: str) -> List[str]:
    """Resolve which fleet cluster(s) run (namespace, service) — ROADMAP P16
    / ADR 0023 — via the Hub's cluster_services registry (populated by
    agentify-discovery's inventory push). Degrades to an empty list on any
    failure or when nothing matches, same convention as
    fetch_service_dependencies: callers fall back to today's single-cluster
    behavior, never block or raise on a missing/incomplete registry.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{backend_url.rstrip('/')}/api/resolve-cluster",
                params={"namespace": namespace, "service": service},
            )
            resp.raise_for_status()
            return resp.json().get("cluster_ids", []) or []
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("resolve_service_clusters failed for %s/%s: %s", namespace, service, e)
        return []


async def mine_service_dependencies(namespace: str, from_service: str, log_text: str, backend_url: str) -> None:
    """Extract validated service mentions from `log_text` and record each as
    an edge `from_service -> mentioned_service`. Best-effort end-to-end —
    never raises; a failed mining pass just means the graph doesn't improve
    this time.
    """
    known_services = await get_known_services(namespace, backend_url)
    if not known_services:
        return
    for to_service in extract_service_mentions(log_text, namespace, known_services):
        if to_service == from_service:
            continue  # not a dependency, just the service mentioning itself
        await upsert_service_dependency(namespace, from_service, to_service, backend_url)
