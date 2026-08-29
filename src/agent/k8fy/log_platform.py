"""log_platform.py — read the Fargate->Firehose->S3->Glue/Athena test harness
(ADR 0021) on demand.

This is the log-platform side of the get_logs router (log_router.py): tried
first for every namespace whenever ATHENA_WORKGROUP/DATABASE/TABLE are
configured (see config/settings.py), falling back to the live Kubernetes API
on empty results or errors. It's a genuinely separate data source from
everything else in this codebase — it queries data agentify-discovery never
ingests either, via AWS Athena/Glue, using the agent's IRSA role (already granted
athena:*/glue:*/s3:GetObject in infra/terraform/aws/logging.tf's
log_query_access policy — no new IAM needed).

boto3 is synchronous; every call here runs via asyncio.to_thread so it never
blocks the event loop other tool calls share.
"""

import asyncio
import datetime
import logging
import re
import time
from typing import Any, Dict, List, Optional

import boto3

from k8fy.log_redaction import redact_log_text

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 1.0
_MAX_POLL_SECONDS = 10.0
_MAX_ROWS = 200

# Kubernetes namespace/pod names are RFC 1123 labels — lowercase alphanumeric
# and hyphens only. Athena has no native query parameterization for this
# SDK path, so values are validated against this instead of interpolated
# unchecked into SQL; anything that doesn't match is rejected outright rather
# than escaped, which is a stronger guarantee than string-escaping alone.
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")


def _validate_k8s_name(value: str, field: str) -> Optional[str]:
    if not _K8S_NAME_RE.match(value):
        return f"invalid {field} {value!r} — must be a valid Kubernetes name"
    return None


def _partition_predicate(hours_back: int) -> str:
    """OR'd (year,month,day,hour) predicates covering the last `hours_back`
    hours (inclusive of the current one), so the query stays partition-pruned
    instead of scanning the whole table."""
    now = datetime.datetime.now(datetime.timezone.utc)
    clauses = []
    for i in range(hours_back):
        t = now - datetime.timedelta(hours=i)
        clauses.append(
            f"(year='{t.year:04d}' AND month='{t.month:02d}' AND day='{t.day:02d}' AND hour='{t.hour:02d}')"
        )
    return "(" + " OR ".join(clauses) + ")"


def _build_query(database: str, table: str, namespace: str, pod: Optional[str], hours_back: int, limit: int) -> str:
    where = [_partition_predicate(hours_back), f"kubernetes.namespace_name = '{namespace}'"]
    if pod:
        where.append(f"kubernetes.pod_name = '{pod}'")
    return (
        f"SELECT kubernetes.pod_name AS pod_name, kubernetes.container_name AS container_name, log "
        f"FROM {database}.{table} WHERE {' AND '.join(where)} "
        f"ORDER BY year DESC, month DESC, day DESC, hour DESC LIMIT {limit}"
    )


def _run_query_sync(query: str, workgroup: str, database: str, region: str) -> Dict[str, Any]:
    """Synchronous start->poll->fetch — always called via asyncio.to_thread.
    region is passed explicitly (not left to boto3's default resolution)
    because this deployment sets AWS_REGION, not AWS_DEFAULT_REGION — the
    only env var this botocore version's region provider chain reads —
    confirmed live (2026-08-30) via a bare boto3.client("athena") raising
    NoRegionError despite AWS_REGION being set in the pod."""
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
    # rows[0] is the header row.
    data_rows = rows[1:]
    parsed = []
    for row in data_rows:
        values = [c.get("VarCharValue", "") for c in row.get("Data", [])]
        if len(values) >= 3:
            parsed.append({"pod_name": values[0], "container_name": values[1], "log": values[2]})
    return {"rows": parsed}


async def query_athena_logs(
    namespace: str,
    pod: Optional[str],
    athena_config: Dict[str, str],
    tail_lines: int = 200,
    hours_back: int = 2,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Fetch recent log lines for a namespace (optionally one pod) from the
    Glue/Athena test harness. Same output shape as live_get_pod_logs
    (`{"namespace", "pod", "logs"}`) so callers don't care which source
    answered.
    """
    if err := _validate_k8s_name(namespace, "namespace"):
        return {"error": err}
    if pod and (err := _validate_k8s_name(pod, "pod")):
        return {"error": err}

    workgroup = athena_config.get("workgroup", "")
    database = athena_config.get("database", "")
    table = athena_config.get("table", "")
    region = athena_config.get("region", "")
    if not (workgroup and database and table):
        return {"error": "Athena log platform is not configured (missing workgroup/database/table)"}

    limit = max(1, min(tail_lines, _MAX_ROWS))
    query = _build_query(database, table, namespace, pod, hours_back, limit)

    try:
        result = await asyncio.to_thread(_run_query_sync, query, workgroup, database, region)
    except Exception as e:  # noqa: BLE001 — boto3 raises many distinct exception types
        logger.warning("athena query failed: %s", e)
        return {"error": f"Athena query failed: {e}"}

    if "error" in result:
        return result

    rows: List[Dict[str, str]] = result.get("rows", [])
    if not rows:
        return {"namespace": namespace, "pod": pod, "logs": ""}

    lines = [f"[{r['pod_name']}/{r['container_name']}] {r['log']}" for r in rows]
    return {
        "namespace": namespace,
        "pod": pod,
        "logs": redact_log_text("\n".join(lines)),
    }
