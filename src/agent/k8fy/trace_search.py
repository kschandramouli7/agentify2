"""trace_search.py — on-demand, cross-cluster search of raw Athena/Glue log
data by trace ID or by a `METHOD /path` string (ROADMAP P29, second half:
"an on-demand search of the raw log store... clarified to mean this, not
network-level capture").

Distinct from dependency_miner.py in the one way that matters: that module
mines CONTINUOUSLY, one (cluster, namespace) at a time, to build the
aggregate `service_dependencies` graph. This module runs ONCE, ON DEMAND,
with NO namespace or cluster restriction at all — a distributed request
routinely crosses namespace boundaries, which is the entire point of tracing
it. Reuses dependency_miner.py's generic helpers (_fetch_selectors,
_service_for_labels, _parse_athena_map, _partition_predicate) rather than
duplicating them — they carry no per-namespace assumption baked in. Unlike
that module, this one never fetches the registered-cluster list at all: the
cross-cluster Athena query itself selects cluster_id/namespace_name per row,
so which (cluster, namespace) pairs matched is already known from the
result set — selectors are then fetched lazily, only for pairs that actually
appear in a matching row. What IS duplicated, deliberately, matching
dependency_miner.py's own stated convention: the query builder and
_run_query_sync, since this module's query selects 5 columns (cluster_id,
namespace_name, pod_name, labels, log) instead of dependency_miner.py's
fixed-namespace 3.

The other real difference: dependency_miner.py's _parse_cri_message DISCARDS
the CRI line's own timestamp, because a continuous miner accumulating
evidence_count never needed order. Reconstructing "who was called, in what
order" for one identified request is this module's entire reason to exist,
so _parse_cri_line here keeps and parses it instead.
"""

import asyncio
import datetime
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import boto3

from k8fy.dependency_miner import (
    _fetch_selectors,
    _parse_athena_map,
    _partition_predicate,
    _service_for_labels,
)
from k8fy.log_redaction import redact_log_text
from k8fy.service_topology import _infer_outcome

logger = logging.getLogger(__name__)

_MAX_ROWS = 500
_POLL_INTERVAL_SECONDS = 1.0
_MAX_POLL_SECONDS = 10.0
_DEFAULT_HOURS_BACK = 24  # an operator debugging something that may have
                          # happened hours ago, not a periodic mining cadence


# ── Input classification ─────────────────────────────────────────────────────
#
# Hex-only on purpose (covers UUID/Jaeger/Zipkin/X-Ray-shaped trace IDs) so an
# ordinary word ("payments" is 8 chars) never gets misclassified as a trace
# ID. Non-hex custom ID schemes are a stated v1 limitation, not an oversight.
_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{8,40}(?:-[0-9a-fA-F]{4,20}){0,4}$")

# The path charset is deliberately restrictive — no quotes, no '%', no
# backslash — doubling as the validate-before-interpolate guard
# log_platform.py's _validate_k8s_name establishes for the same reason: this
# Athena SDK path has no query parameterization, so an unvalidated value
# would be interpolated directly into a LIKE clause.
_METHODS = r"GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS"
_METHOD_PATH_RE = re.compile(
    rf"^(?:(?P<method>{_METHODS})\s+)?(?P<path>/[A-Za-z0-9_\-./]*)$", re.IGNORECASE
)


def _classify_input(text: str) -> Tuple[Optional[str], Optional[Dict[str, Optional[str]]], Optional[str]]:
    """Returns (kind, value, error). kind is "trace_id" or "url_path"; value
    is {"trace_id": ...} or {"method": ...|None, "path": ...}. Exactly one of
    (kind, error) is set."""
    text = text.strip()
    if not text:
        return None, None, "empty query"
    m = _METHOD_PATH_RE.match(text)
    if m:
        return "url_path", {"method": (m.group("method") or "").upper() or None, "path": m.group("path")}, None
    if _TRACE_ID_RE.match(text):
        return "trace_id", {"trace_id": text}, None
    return None, None, (
        f"{text!r} doesn't look like a trace ID or a \"METHOD /path\" (e.g. \"POST /charge\" or \"/charge\")"
    )


# ── Request-line-context post-filter ─────────────────────────────────────────
#
# service_topology.py's own module docstring names the exact bug class this
# guards against: the disabled "external" mining tier fabricated dependency
# edges (www.nokia.com, dashboard.voyageai.com) from bare substring matches
# in log prose — a Referer header, a quoted error body. A bare `LIKE
# '%/charge%'` has the identical failure mode. The fix here is the same
# whitelist-only discipline: a path only counts when it appears immediately
# after an HTTP verb or a path=/route=/"uri":-shaped field, never a denylist
# of bad contexts.
def _path_context_re(path: str) -> "re.Pattern[str]":
    e = re.escape(path)
    return re.compile(
        rf'(?:\b(?:{_METHODS})\s+{e}(?=[\s"\'?]|$))'
        rf'|(?:\b(?:path|route|uri|endpoint)"?\s*[:=]\s*"?{e}(?=[\s"\'/,}}]|$))',
        re.IGNORECASE,
    )


def _line_matches_request_context(line: str, method: Optional[str], path: str) -> bool:
    m = _path_context_re(path).search(line)
    if not m:
        return False
    method_prefixed = re.match(rf"\b({_METHODS})\s+", m.group(0), re.IGNORECASE)
    if method and method_prefixed and method_prefixed.group(1).upper() != method.upper():
        return False  # an explicit method was given and disagrees with the request-line's own
    return True


# ── CRI parsing that KEEPS the timestamp (contrast dependency_miner.py) ──────
_CRI_LINE_FULL_RE = re.compile(r"^(\S+)\s+(\S+)\s+(\S)\s+(.*)$", re.DOTALL)


def _parse_cri_line(raw_log: str) -> Tuple[Optional[datetime.datetime], str]:
    """Splits a CRI log line into (parsed timestamp or None, message text).
    A line that doesn't match the expected shape, or whose timestamp fails to
    parse, returns (None, <best-effort message text>) — the caller drops
    None-timestamp rows from the ordered result rather than guessing their
    position, since an unordered row is worse than a missing one for a
    feature whose entire value is order."""
    m = _CRI_LINE_FULL_RE.match(raw_log)
    if not m:
        return None, raw_log
    ts_str, _stream, _tag, message = m.groups()
    try:
        return datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00")), message
    except ValueError:
        return None, message


# ── Cross-cluster, cross-namespace Athena query (no namespace/cluster WHERE) ─

def _build_trace_id_query(database: str, table: str, trace_id: str, hours_back: int, limit: int) -> str:
    where = [_partition_predicate(hours_back), f"log LIKE '%{trace_id}%'"]
    return (
        f"SELECT cluster_id, kubernetes.namespace_name AS namespace_name, "
        f"kubernetes.pod_name AS pod_name, kubernetes.labels AS labels, log "
        f"FROM {database}.{table} WHERE {' AND '.join(where)} LIMIT {limit}"
    )


def _build_url_path_query(database: str, table: str, path: str, hours_back: int, limit: int) -> str:
    # LIKE on the path alone — method isn't reliably adjacent to path in
    # every log format, so this is a cheap broad SQL pre-filter; the precise
    # request-context guard runs in Python afterward, against the parsed
    # message text.
    where = [_partition_predicate(hours_back), f"log LIKE '%{path}%'"]
    return (
        f"SELECT cluster_id, kubernetes.namespace_name AS namespace_name, "
        f"kubernetes.pod_name AS pod_name, kubernetes.labels AS labels, log "
        f"FROM {database}.{table} WHERE {' AND '.join(where)} LIMIT {limit}"
    )


def _run_query_sync(query: str, workgroup: str, database: str, region: str) -> Dict[str, Any]:
    """Same start->poll->fetch shape as dependency_miner.py's own
    _run_query_sync (copied, not imported, per that module's own precedent of
    not sharing internals across query shapes) — but this result set has 5
    columns instead of 3, since there is no fixed cluster_id/namespace to
    already know."""
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
        if len(values) >= 5:
            parsed.append({
                "cluster_id": values[0], "namespace": values[1],
                "pod_name": values[2], "labels": values[3], "log": values[4],
            })
    return {"rows": parsed}


async def search_trace(
    query_text: str,
    backend_url: str,
    athena_config: Dict[str, str],
    hours_back: int = _DEFAULT_HOURS_BACK,
) -> Dict[str, Any]:
    """The public entry point: classify query_text, search every onboarded
    cluster/namespace's recent logs for it, attribute and order what
    matched, and return an ordered list of "hops" — see the module's own
    ADR/plan for the exact shape. Never raises; every failure mode returns
    {"error": ..., "error_kind": ...} instead."""
    kind, value, err = _classify_input(query_text)
    if err:
        return {"error": err, "error_kind": "invalid_input"}

    workgroup = athena_config.get("workgroup", "")
    database = athena_config.get("database", "")
    table = athena_config.get("table", "")
    region = athena_config.get("region", "")
    if not (workgroup and database and table):
        return {"error": "Athena is not configured (missing workgroup/database/table)", "error_kind": "query_failed"}

    limit = _MAX_ROWS
    method: Optional[str] = None
    path: Optional[str] = None
    if kind == "trace_id":
        query = _build_trace_id_query(database, table, value["trace_id"], hours_back, limit)
    else:
        method, path = value["method"], value["path"]
        query = _build_url_path_query(database, table, path, hours_back, limit)

    try:
        result = await asyncio.to_thread(_run_query_sync, query, workgroup, database, region)
    except Exception as e:  # noqa: BLE001 — boto3 raises many distinct exception types
        logger.warning("trace_search: Athena query failed: %s", e)
        return {"error": f"Athena query failed: {e}", "error_kind": "query_failed"}
    if "error" in result:
        return {"error": result["error"], "error_kind": "query_failed"}

    rows = result.get("rows", [])

    # Parse timestamps + strip the CRI envelope; drop rows whose timestamp
    # can't be parsed rather than guess their position.
    parsed_rows: List[Dict[str, Any]] = []
    dropped_unparseable_timestamp_count = 0
    for row in rows:
        ts, message = _parse_cri_line(row["log"])
        if ts is None:
            dropped_unparseable_timestamp_count += 1
            continue
        parsed_rows.append({**row, "timestamp": ts, "message": message})

    # URL/path: require genuine request-line context. Trace ID: an opaque ID
    # is not prose that can be mistaken for something else, so no filter.
    if kind == "url_path":
        parsed_rows = [r for r in parsed_rows if _line_matches_request_context(r["message"], method, path)]

    # Selectors fetched lazily — only for (cluster_id, namespace) pairs that
    # actually appear in the surviving rows, not every registered
    # cluster/namespace. This is one on-demand click, not a standing
    # pipeline; a real trace touches a handful of services.
    distinct_pairs: Set[Tuple[str, str]] = {(r["cluster_id"], r["namespace"]) for r in parsed_rows}
    selectors_by_pair: Dict[Tuple[str, str], Dict[str, Dict[str, str]]] = {}
    for cluster_id, namespace in distinct_pairs:
        selectors_by_pair[(cluster_id, namespace)] = await _fetch_selectors(backend_url, cluster_id, namespace)

    hops: List[Dict[str, Any]] = []
    unattributed_count = 0
    for r in parsed_rows:
        selectors = selectors_by_pair.get((r["cluster_id"], r["namespace"]), {})
        labels = _parse_athena_map(r["labels"])
        service = _service_for_labels(labels, selectors)
        if service is None:
            unattributed_count += 1
        hops.append({
            "timestamp": r["timestamp"],
            "cluster_id": r["cluster_id"],
            "namespace": r["namespace"],
            "service": service,
            "pod_name": r["pod_name"],
            "outcome": _infer_outcome(r["message"]),
            "log_excerpt": redact_log_text(r["message"]),
        })

    hops.sort(key=lambda h: h["timestamp"])
    for i, h in enumerate(hops, start=1):
        h["seq"] = i
        h["timestamp"] = h["timestamp"].isoformat()

    return {
        "kind": kind,
        "query": query_text,
        "hours_back": hours_back,
        "hops": hops,
        "unattributed_count": unattributed_count,
        "dropped_unparseable_timestamp_count": dropped_unparseable_timestamp_count,
    }
