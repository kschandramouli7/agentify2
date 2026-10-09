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
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

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

    Implemented in terms of extract_service_calls (ROADMAP P27 phase 2) —
    same matches, this just drops the port/outcome detail nothing here needs.
    """
    return {obs.service for obs in extract_service_calls(log_text, namespace, known_services)}


# ── Outcome and port capture (ROADMAP P27 phase 2) ───────────────────────────
#
# A mined edge today is four facts — from_service, to_service, evidence_count,
# first/last_seen — so a healthy call and a failed one produce identical rows.
# CallObservation and extract_service_calls add two fields already sitting in
# the log text and previously discarded: which port was called (bare host:port
# form only — a qualified FQDN mention carries no port in the matched text
# itself, and no attempt is made to guess one nearby), and whether that
# specific call succeeded, failed, or timed out.
#
# extract_service_mentions is NOT changed in place — it is pinned by tests in
# this file and its agent twin (src/agent/k8fy/service_topology.py, which this
# module's docstring already says is where the mining LOGIC is defined and
# copied from unchanged). Instead it is reimplemented above in terms of this
# richer function, which is behavior-preserving: none of the regexes above can
# match across a newline (no character class includes "\n"), so scanning
# line-by-line here finds exactly the same mentions extract_service_mentions
# always has — line-splitting only adds per-line context for outcome
# inference, it does not change which hostnames are found.
@dataclass(frozen=True)
class CallObservation:
    """One validated service mention, plus what could be learned about that
    specific call from the same log line it appeared on."""
    service: str
    port: Optional[int] = None      # known only when a :<port> immediately follows the host
    outcome: Optional[str] = None   # "success" | "failure" | "timeout" | None (unknown)
    path: str = ""                  # normalized operation class, "" when not captured (ROADMAP P27 phase 4)
    # ROADMAP P27 phase 4 (provenance). "qualified" | "bare" — which of the
    # three match loops in extract_service_calls produced this observation.
    # Not a detection change, just a label on a distinction the matcher
    # already makes: _HOSTNAME_RE is the qualified FQDN form, _URL_HOST_RE/
    # _HOST_PORT_RE are both bare-name forms (weaker evidence — a short name
    # resolved via the pod's own search domain, not a fully-qualified one).
    match_kind: str = ""


# Trigger words/symbols that make a following 3-digit number a plausible HTTP
# status code rather than a coincidental number (a duration, a retry count, a
# port). Checked first, before any keyword, because it's the strongest signal
# available in ordinary log text.
_HTTP_STATUS_RE = re.compile(
    r"(?:->|status(?:=|\s*:\s*|\s+is\s+)?|responded|response|returned)\s*\**\b([1-5]\d{2})\b",
    re.IGNORECASE,
)
_TIMEOUT_RE = re.compile(r"\b(?:timeout|timed out)\b", re.IGNORECASE)
_FAILURE_WORDS_RE = re.compile(r"\b(?:unreachable|refused|failed|failure|unavailable)\b", re.IGNORECASE)
_SUCCESS_WORDS_RE = re.compile(r"\bok\b", re.IGNORECASE)


def _infer_outcome(line: str) -> Optional[str]:
    """Best-effort outcome classification for one log line. Deliberately
    conservative and ordered by confidence, so a weaker keyword elsewhere on
    the line never overrides a stronger signal. Returns None (unknown) far
    more often than a real APM would — that is by design: outcome vocabulary
    is per-logger, so a wrong guess here corrupts the confidence model this
    data feeds (evidence_count / coverage), while an honest "unknown" just
    stays out of the outcome counters and is visible as such."""
    m = _HTTP_STATUS_RE.search(line)
    if m:
        code = int(m.group(1))
        return "success" if code < 400 else "failure"
    if _TIMEOUT_RE.search(line):
        return "timeout"
    if _FAILURE_WORDS_RE.search(line):
        return "failure"
    if _SUCCESS_WORDS_RE.search(line):
        return "success"
    return None


# ── Path / operation class (ROADMAP P27 phase 4) ──────────────────────────────
#
# A raw path has unbounded cardinality (/orders/12345 vs /orders/67890), unlike
# port — storing it as-is would fragment evidence across effectively-infinite
# rows. Normalizing first turns it into a bounded "operation class":
# /orders/12345 -> /orders/:id. Deliberately simple and conservative, same
# spirit as the rest of this pipeline (no OpenAPI inference, no ML) — a
# segment becomes :id only when it is unambiguously an identifier shape.
_NUMERIC_SEGMENT_RE = re.compile(r"^\d+$")
_UUID_SEGMENT_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_HEX_SEGMENT_RE = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)

# Matches immediately after a validated hostname (via re.match(line, pos), so
# no leading ^): an optional :<port>, then an optional /<path>. The path stops
# at the first whitespace/quote/bracket/comma/?, which keeps a query string or
# trailing log prose (" -> 200", attempt 3/3)") out of the capture entirely —
# there is no separate strip-the-query-string step because there is nothing to
# strip; it was never captured.
_PATH_SUFFIX_RE = re.compile(r"(?::(\d{2,5}))?(/[^\s\"'()<>,?]*)?")
_MAX_RAW_PATH_LEN = 200  # a longer capture is more likely log prose than a real path


def _normalize_path(raw: str) -> str:
    """/orders/12345 -> /orders/:id. Empty or unparseable input -> "" (the
    "not captured" sentinel everywhere else in this pipeline uses). A bare
    "/" with nothing after it normalizes to "" too — unlike "/health" or
    "/v1/pki/issue", it names no operation, so it carries no more
    diagnostic value than "no path captured" at all. Without this, a log
    line ending "host/" (no path) and one ending just "host" (also no
    path) would split the same edge's evidence across two rows instead of
    accumulating in one."""
    if not raw or raw == "/" or len(raw) > _MAX_RAW_PATH_LEN:
        return ""
    segments = raw.split("/")
    normalized = []
    for seg in segments:
        if seg and (
            _NUMERIC_SEGMENT_RE.match(seg)
            or _UUID_SEGMENT_RE.match(seg)
            or _HEX_SEGMENT_RE.match(seg)
        ):
            normalized.append(":id")
        else:
            normalized.append(seg)
    return "/".join(normalized)


def _peek_port_and_path(line: str, pos: int) -> "Tuple[Optional[int], str]":
    """Looks immediately after a validated hostname match for an optional
    `:<port>` and an optional `/<path>` — the shape a URL takes right after
    its host (`http://host:port/path`). Best-effort: a line with neither
    yields (None, "") rather than failing anything.
    """
    m = _PATH_SUFFIX_RE.match(line, pos)
    port = int(m.group(1)) if m.group(1) else None
    return port, _normalize_path(m.group(2) or "")


def extract_service_calls(log_text: str, namespace: str, known_services: Set[str]) -> List[CallObservation]:
    """Like extract_service_mentions, but keeps the port, the normalized path,
    and infers an outcome from the same line each mention appeared on.

    Iterates line-by-line — unlike extract_service_mentions, which scans the
    whole blob at once — because outcome context is local to one line; see
    the module note above for why this doesn't change which mentions are
    found. Returns one CallObservation per (line, mention): a repeated
    mention across multiple lines yields multiple entries, since each one may
    carry a different outcome. Callers that need "at most once per cycle"
    dedup decide that policy themselves.
    """
    if not log_text or not known_services:
        return []

    observations: List[CallObservation] = []
    for line in log_text.split("\n"):
        outcome = _infer_outcome(line)

        for m in _HOSTNAME_RE.finditer(line):
            service_candidate, namespace_candidate = m.group(1), m.group(2)
            if namespace_candidate == namespace and service_candidate in known_services:
                port, path = _peek_port_and_path(line, m.end())
                observations.append(
                    CallObservation(
                        service=service_candidate, port=port, outcome=outcome, path=path, match_kind="qualified",
                    )
                )

        for m in _URL_HOST_RE.finditer(line):
            host = m.group(1)
            if "." in host:
                continue
            if host in known_services:
                port, path = _peek_port_and_path(line, m.end())
                observations.append(
                    CallObservation(service=host, port=port, outcome=outcome, path=path, match_kind="bare")
                )

        for m in _HOST_PORT_RE.finditer(line):
            name, port_str = m.group(1), m.group(2)
            if name in known_services:
                # Port is already known from this match itself; only the path
                # half of the peek is used here.
                _, path = _peek_port_and_path(line, m.end())
                observations.append(
                    CallObservation(
                        service=name, port=int(port_str), outcome=outcome, path=path, match_kind="bare",
                    )
                )

    return observations


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
    log_text: str, namespace: str, services_by_namespace: Dict[str, Set[str]]
) -> Set[Tuple[str, str]]:
    """Targets OUTSIDE this namespace, as (kind, target) pairs.

    `services_by_namespace` maps every namespace this cluster has to its real
    Service names. BOTH segments of a cross-namespace target are checked
    against it — the namespace must exist AND the service must be a real
    Service in that namespace.

    Validating only the namespace was not enough, and the failure was live on
    2026-09-05: `_HOSTNAME_RE`'s character class accepts hex and hyphens, so a
    trace UUID followed by a dot and a real namespace ("c53b9dca-f4c0-….vault")
    passed as a service call and drew three phantom boxes. Requiring the
    service segment to be a real Service removes the entire class rather than
    pattern-matching UUIDs, which is what the same-namespace miner has always
    done and why it has never had this problem.

    "external" (a public hostname) remains unvalidatable and is gated off by
    default — see MINE_EXTERNAL_EGRESS.
    """
    if not log_text:
        return set()

    found: Set[Tuple[str, str]] = set()

    # Qualified in-cluster names pointing at a DIFFERENT namespace. The trailing
    # .svc.cluster.local is optional, exactly as in the same-namespace path.
    for service_candidate, namespace_candidate in _HOSTNAME_RE.findall(log_text):
        if namespace_candidate == namespace:
            continue                      # the same-namespace miner owns this
        # BOTH segments, not just the namespace. See the docstring.
        if service_candidate in services_by_namespace.get(namespace_candidate, frozenset()):
            found.add(("cross_namespace", f"{service_candidate}.{namespace_candidate}"))

    # Public hostnames, in a hostname context only.
    for host in _URL_HOST_RE.findall(log_text):
        h = host.rstrip(".").lower()
        if h.endswith(".svc.cluster.local") or h.endswith(".cluster.local") or h.endswith(".local"):
            continue                      # in-cluster; handled above
        if _looks_like_a_real_host(h):
            found.add(("external", h))

    return found


async def resolve_cross_cluster_target(
    host: str, backend_url: str, kinds: List[str], cache: Dict[str, List[str]],
) -> List[str]:
    """Which cluster_id(s), if any, run an ingress/route fronting `host` —
    the validation that promotes an "external"-shaped hostname mention to
    the stronger `cross_cluster` tier (ROADMAP P31 phase 1, ADR 0037).
    Wraps `GET /admin/ingress-lookup`, same best-effort/degrade-to-empty
    convention as every other Hub call here. `cache` memoizes per scan
    cycle (keyed by host) so N pods mentioning the same migrating host cost
    one HTTP call, not N.
    """
    if host in cache:
        return cache[host]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{backend_url.rstrip('/')}/admin/ingress-lookup",
                params={"host": host, "kinds": ",".join(kinds)},
            )
            resp.raise_for_status()
            cluster_ids = (resp.json() or {}).get("cluster_ids") or []
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("resolve_cross_cluster_target failed for host=%s: %s", host, e)
        cluster_ids = []
    cache[host] = cluster_ids
    return cluster_ids


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
    namespace: str,
    from_service: str,
    to_service: str,
    backend_url: str,
    collector_token: str,
    target_kind: str = "service",
    port: Optional[int] = None,
    outcome: Optional[str] = None,
    path: str = "",
    caller_pod: str = "",
    match_kind: str = "",
    source: str = "",
    target_cluster_id: str = "",
) -> None:
    """Record one piece of evidence for a from->to edge via the tenant-scoped
    ingest endpoint. Best-effort: any failure is logged and swallowed — one
    dropped scan cycle never blocks the next.

    port/outcome/path (ROADMAP P27 phases 2 and 4) are sent as 0/""/"" when
    unknown — see upsert_service_dependency's identical note (agent's
    service_topology.py) for why those rather than omitting the fields.
    caller_pod/match_kind/source (phase 4, caller cardinality and
    provenance) follow the same convention; see that same note for why an
    empty caller_pod/source is skipped Hub-side rather than stored as a
    sentinel. target_cluster_id (ROADMAP P31 phase 1) is the resolved
    cluster_id when target_kind="cross_cluster" — "" means either not
    cross-cluster, or resolved ambiguously across more than one cluster and
    deliberately left unattributed (see resolve_cross_cluster_target).
    """
    # Omit the header entirely when unset — see push_inventory's identical
    # comment (inventory.py) for why.
    headers = {"Authorization": f"Bearer {collector_token}"} if collector_token else {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{backend_url.rstrip('/')}/api/service-dependencies",
                json={
                    "namespace": namespace,
                    "from_service": from_service,
                    "to_service": to_service,
                    # Which trust tier produced this edge (ROADMAP P27 phase 3).
                    # Sent explicitly rather than inferred Hub-side: only the
                    # miner knows, and inferring from the string shape would
                    # silently reclassify edges if a log format changed.
                    "target_kind": target_kind,
                    "port": port or 0,
                    "outcome": outcome or "",
                    "path": path or "",
                    "caller_pod": caller_pod or "",
                    "match_kind": match_kind or "",
                    "source": source or "",
                    "target_cluster_id": target_cluster_id or "",
                },
                headers=headers,
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("push_dependency failed for %s/%s->%s: %s", namespace, from_service, to_service, e)
