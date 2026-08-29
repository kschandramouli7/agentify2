"""dependency_miner.py — mines service-to-service dependency edges from
centralized Athena/Glue log data across every registered fleet cluster (ADR
0029, ROADMAP P15/P18 use case #2's Glue extension).

Complements, not replaces, agentify-discovery's existing live per-cluster
mining (src/adapters/discovery/main.py's _scan_namespace) — this runs
centrally, in the Agent process, alongside log_router.py's existing Athena
connector, querying the same shared Glue table Firehose already aggregates
across every onboarded cluster in one place. Reuses extract_service_mentions
verbatim (the actual "does this log mention service X" logic) — this miner
is simply a third caller of that function.

Runs as a periodic background task (app.py's startup event), not a Claude
tool or an on-demand HTTP handler — same "deterministic, not agentic"
posture as agentify-discovery's own scan cycle. boto3 is synchronous; the
Athena call runs via asyncio.to_thread so it never blocks the event loop
other tool calls share (same discipline as log_platform.py).
"""

import asyncio
import datetime
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import boto3
import httpx

from k8fy.service_topology import extract_service_mentions

logger = logging.getLogger(__name__)

_MAX_ROWS = 500
_POLL_INTERVAL_SECONDS = 1.0
_MAX_POLL_SECONDS = 10.0

# Matches a Fargate Fluent Bit CRI log line: "<RFC3339 timestamp> <stream>
# <tag> <message>" (e.g. "2026-07-24T22:22:26Z stdout F <app log line>").
# Group 1 is the application's own message text — the only part
# extract_service_mentions should ever see, not Fluent Bit's framing.
_CRI_LINE_RE = re.compile(r"^\S+\s+\S+\s+\S\s+(.*)$", re.DOTALL)


def _parse_cri_message(raw_log: str) -> str:
    """Strip a CRI log line's <timestamp> <stream> <tag> prefix. Returns the
    line unchanged if it doesn't match the expected shape — defensive; a
    malformed line just gets scanned as-is rather than dropped."""
    m = _CRI_LINE_RE.match(raw_log)
    return m.group(1) if m else raw_log


def _parse_athena_map(raw: str) -> Dict[str, str]:
    """Athena/Presto renders a map<string,string> column as "{k=v, k2=v2}"
    in query results, not JSON. Good enough for K8s labels (simple flat
    key=value strings, no embedded "=" or ", ") — a known simplification,
    not a general Presto-map parser."""
    raw = raw.strip()
    if not raw or raw in ("{}", "NULL"):
        return {}
    inner = raw.strip("{}")
    result: Dict[str, str] = {}
    for pair in inner.split(", "):
        if "=" in pair:
            k, _, v = pair.partition("=")
            result[k.strip()] = v.strip()
    return result


def _service_for_labels(labels: Dict[str, str], selectors: Dict[str, Dict[str, str]]) -> Optional[str]:
    """Which service (by name) a pod's labels match, via the same
    selector-to-label semantics K8s itself uses to build Service endpoints —
    ported from agentify-discovery's main.py::_service_for_pod, fed a stored
    selector map (this miner has no live cluster access) instead of a live
    Service list. A selector with no keys never matches (same as live)."""
    for name, selector in selectors.items():
        if selector and all(labels.get(k) == v for k, v in selector.items()):
            return name
    return None


def _partition_predicate(hours_back: int) -> str:
    """OR'd (year,month,day,hour) predicates covering the last `hours_back`
    hours (inclusive of the current one) — same partition-pruning shape as
    log_platform.py's own helper, deliberately duplicated rather than
    imported (this module and log_platform.py don't share private
    internals, same spirit as service_topology.py's two independent
    agent/discovery copies)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    clauses = []
    for i in range(hours_back):
        t = now - datetime.timedelta(hours=i)
        clauses.append(
            f"(year='{t.year:04d}' AND month='{t.month:02d}' AND day='{t.day:02d}' AND hour='{t.hour:02d}')"
        )
    return "(" + " OR ".join(clauses) + ")"


def _build_query(database: str, table: str, cluster_id: str, namespace: str, hours_back: int, limit: int) -> str:
    where = [
        _partition_predicate(hours_back),
        f"cluster_id = '{cluster_id}'",
        f"kubernetes.namespace_name = '{namespace}'",
    ]
    return (
        f"SELECT kubernetes.pod_name AS pod_name, kubernetes.labels AS labels, log "
        f"FROM {database}.{table} WHERE {' AND '.join(where)} "
        f"ORDER BY year DESC, month DESC, day DESC, hour DESC LIMIT {limit}"
    )


def _run_query_sync(query: str, workgroup: str, database: str, region: str) -> Dict[str, Any]:
    """Synchronous start->poll->fetch — always called via asyncio.to_thread.
    Same shape as log_platform.py's _run_query_sync (a separate copy, not
    imported, for the same reason _partition_predicate is above). region is
    passed explicitly because this deployment's botocore version only reads
    AWS_DEFAULT_REGION, not the AWS_REGION env var the pod actually sets —
    confirmed live (2026-08-30) via NoRegionError despite AWS_REGION being set."""
    client = boto3.client("athena", region_name=region)
    start = client.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": database},
        WorkGroup=workgroup,
    )
    query_id = start["QueryExecutionId"]

    elapsed = 0.0
    while elapsed < _MAX_POLL_SECONDS:
        status = client.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status.get("StateChangeReason", "unknown error")
            return {"error": f"Athena query {state.lower()}: {reason}"}
        time.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS
    else:
        return {"error": f"Athena query timed out after {_MAX_POLL_SECONDS}s (query_id={query_id})"}

    results = client.get_query_results(QueryExecutionId=query_id, MaxResults=_MAX_ROWS)
    rows = results["ResultSet"]["Rows"]
    if not rows:
        return {"rows": []}
    data_rows = rows[1:]  # rows[0] is the header row
    parsed = []
    for row in data_rows:
        values = [c.get("VarCharValue", "") for c in row.get("Data", [])]
        if len(values) >= 3:
            parsed.append({"pod_name": values[0], "labels": values[1], "log": values[2]})
    return {"rows": parsed}


async def _fetch_registered_clusters(backend_url: str) -> List[Dict[str, Any]]:
    """Every registered fleet cluster (id + onboarded namespaces), read from
    the Hub's admin API. Degrades to an empty list on any failure (same
    convention as every other Hub-read helper in this codebase) — a failed
    cycle just mines nothing this time, not a crash."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{backend_url.rstrip('/')}/admin/integrations")
            resp.raise_for_status()
            return resp.json() or []
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("dependency_miner: failed to list registered clusters: %s", e)
        return []


async def _fetch_selectors(backend_url: str, cluster_id: str, namespace: str) -> Dict[str, Dict[str, str]]:
    """One (cluster, namespace)'s known services and their selectors (ADR
    0029). Doubles as the known-services set extract_service_mentions needs
    — its keys ARE the known service names, no separate fetch required."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{backend_url.rstrip('/')}/api/cluster-service-selectors",
                params={"cluster_id": cluster_id, "namespace": namespace},
            )
            resp.raise_for_status()
            return resp.json().get("selectors", {}) or {}
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("dependency_miner: failed to fetch selectors for %s/%s: %s", cluster_id, namespace, e)
        return {}


async def _push_edge(backend_url: str, cluster_id: str, namespace: str, from_service: str, to_service: str) -> None:
    """Push one discovered edge — no bearer token (ADR 0029's trusted-
    internal-caller path; this miner is the Agent, on the same trusted,
    unauthenticated boundary every other Agent-to-Hub call already uses).
    Best-effort: log-and-swallow on failure, same as every other push_* in
    this codebase — one dropped edge never blocks the rest of the cycle."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{backend_url.rstrip('/')}/api/service-dependencies",
                json={
                    "namespace": namespace,
                    "from_service": from_service,
                    "to_service": to_service,
                    "cluster_id": cluster_id,
                },
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning(
            "dependency_miner: push_edge failed for %s/%s->%s in cluster=%s: %s",
            namespace, from_service, to_service, cluster_id, e,
        )


async def _mine_namespace(
    backend_url: str, athena_config: Dict[str, str], cluster_id: str, namespace: str, hours_back: int,
) -> None:
    """Mine one (cluster, namespace)'s recently-landed log partition for
    service-dependency edges, pushing each discovered edge at most once per
    cycle (evidence_count already accumulates cluster-side across cycles —
    no need to push a duplicate within one)."""
    selectors = await _fetch_selectors(backend_url, cluster_id, namespace)
    if not selectors:
        return  # no known services (or the fetch failed) — nothing to attribute logs to

    known_services: Set[str] = set(selectors.keys())
    workgroup = athena_config.get("workgroup", "")
    database = athena_config.get("database", "")
    table = athena_config.get("table", "")
    region = athena_config.get("region", "")
    query = _build_query(database, table, cluster_id, namespace, hours_back, _MAX_ROWS)

    try:
        result = await asyncio.to_thread(_run_query_sync, query, workgroup, database, region)
    except Exception as e:  # noqa: BLE001 — boto3 raises many distinct exception types
        logger.warning("dependency_miner: Athena query failed for cluster=%s namespace=%s: %s", cluster_id, namespace, e)
        return
    if "error" in result:
        logger.warning("dependency_miner: %s (cluster=%s namespace=%s)", result["error"], cluster_id, namespace)
        return

    # Group each pod's log lines together so extract_service_mentions sees
    # that pod's full recently-landed text at once, not one line at a time —
    # matches Discovery's own live-mining unit of work (one pod's fetched
    # tail as a whole), not a per-line scan.
    by_pod: Dict[str, List[str]] = {}
    pod_labels: Dict[str, Dict[str, str]] = {}
    for row in result.get("rows", []):
        pod_name = row["pod_name"]
        pod_labels.setdefault(pod_name, _parse_athena_map(row["labels"]))
        by_pod.setdefault(pod_name, []).append(_parse_cri_message(row["log"]))

    pushed: Set[Tuple[str, str]] = set()
    for pod_name, lines in by_pod.items():
        from_service = _service_for_labels(pod_labels.get(pod_name, {}), selectors)
        if not from_service:
            continue
        log_text = "\n".join(lines)
        for to_service in extract_service_mentions(log_text, namespace, known_services):
            if to_service == from_service:
                continue
            edge = (from_service, to_service)
            if edge in pushed:
                continue
            pushed.add(edge)
            await _push_edge(backend_url, cluster_id, namespace, from_service, to_service)


async def run_once(backend_url: str, athena_config: Dict[str, str], hours_back: int = 2) -> None:
    """One full mining cycle across every registered fleet cluster's
    onboarded namespaces. Never raises — a failure mining one
    (cluster, namespace) never blocks the others (same log-and-continue
    discipline as agentify-discovery's own _scan_once)."""
    if not (athena_config.get("workgroup") and athena_config.get("database") and athena_config.get("table")):
        logger.info("dependency_miner: Athena not configured, skipping this cycle")
        return

    clusters = await _fetch_registered_clusters(backend_url)
    for cluster in clusters:
        cluster_id = cluster.get("id", "")
        if not cluster_id:
            continue
        for namespace in cluster.get("namespaces") or []:
            try:
                await _mine_namespace(backend_url, athena_config, cluster_id, namespace, hours_back)
            except Exception:
                logger.exception("dependency_miner: mining failed for cluster=%s namespace=%s", cluster_id, namespace)


async def run_forever(
    backend_url: str, athena_config: Dict[str, str], interval_seconds: int, shutdown: asyncio.Event,
) -> None:
    """Periodic background task — same shape as agentify-discovery's own
    scan-cycle ticker (main.py::_run): run once, then wait up to
    interval_seconds for shutdown before the next cycle. Never raises out of
    the loop; a failed cycle just tries again next time."""
    while not shutdown.is_set():
        try:
            await run_once(backend_url, athena_config)
        except Exception:
            logger.exception("dependency_miner: mining cycle failed")
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass  # normal: next cycle starts
