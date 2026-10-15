"""log_platform_splunk.py — read a customer's real Splunk instance on demand
(ROADMAP P15, ADR 0041).

Tried before the Athena/Glue test harness by log_router.py's get_logs()
when SPLUNK_URL is configured — Splunk represents a customer's real,
already-populated log platform, more authoritative than Athena's own "test
harness" (ADR 0021) when both happen to be configured. Falls back to
Athena, then the live cluster, on empty results or any error — same
fetch-redact-discard, never-persisted discipline ADR 0014 already
established for every tier.

Design-first (ADR 0041): no real Splunk instance exists yet to verify the
field-mapping schema against. See that ADR before changing the SPL query
or the result-row parsing below — the field names read off each result row
are this design's single highest-risk assumption.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from k8fy.log_platform import _validate_k8s_name  # same validator Athena uses — namespace/pod-agnostic, not Athena-specific
from k8fy.log_redaction import redact_log_text

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 1.0
_MAX_POLL_SECONDS = 10.0
_MAX_ROWS = 200


def _build_spl(index: str, namespace: str, pod: Optional[str], limit: int) -> str:
    # _validate_k8s_name has already rejected anything namespace/pod could
    # contain that would make this interpolation unsafe (same "reject, don't
    # escape" discipline Athena's _build_query already uses) — index is an
    # operator-configured value (SPLUNK_INDEX), not request input.
    where = [f"index={index}", f'kubernetes.namespace_name="{namespace}"']
    if pod:
        where.append(f'kubernetes.pod_name="{pod}"')
    # "| table *" is load-bearing, not cosmetic — see
    # splunk_dependency_miner.py's _build_namespace_spl for the full
    # explanation (confirmed live): Splunk's JSON results API silently
    # drops any field not named in the SPL itself, even when it's genuinely
    # indexed on the event. kubernetes.container_name and (when `pod` is
    # unset) kubernetes.pod_name are never named in this query otherwise.
    return f"search {' '.join(where)} | head {limit} | table *"


async def _run_query(
    client: httpx.AsyncClient, splunk_url: str, token: str, spl: str, hours_back: int, result_limit: int = _MAX_ROWS,
) -> Dict[str, Any]:
    """Start -> poll -> fetch — same three-call shape Athena's own
    _run_query_sync uses, just async-native (httpx) instead of boto3-via-
    asyncio.to_thread, since Splunk's REST API has no equivalent SDK here.
    result_limit is a parameter, not a bare module constant, because
    splunk_dependency_miner.py (ADR 0042) reuses this same function for a
    namespace-wide mining fetch with its own cap (500, matching
    dependency_miner.py's own _MAX_ROWS) — different from this module's
    single-pod on-demand-read cap (200, matching the tail_lines convention)."""
    headers = {"Authorization": f"Bearer {token}"}
    base = splunk_url.rstrip("/")

    start_resp = await client.post(
        f"{base}/services/search/jobs",
        headers=headers,
        data={
            "search": spl,
            "earliest_time": f"-{hours_back}h",
            "latest_time": "now",
            "output_mode": "json",
        },
    )
    start_resp.raise_for_status()
    sid = start_resp.json().get("sid")
    if not sid:
        return {"error": "Splunk did not return a search job id"}

    elapsed = 0.0
    while elapsed < _MAX_POLL_SECONDS:
        status_resp = await client.get(
            f"{base}/services/search/jobs/{sid}", headers=headers, params={"output_mode": "json"},
        )
        status_resp.raise_for_status()
        entries = status_resp.json().get("entry") or []
        state = entries[0].get("content", {}).get("dispatchState") if entries else None
        if state == "DONE":
            break
        if state == "FAILED":
            return {"error": f"Splunk search job {sid} failed"}
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS
    else:
        return {"error": f"Splunk search timed out after {_MAX_POLL_SECONDS}s (sid={sid})"}

    results_resp = await client.get(
        f"{base}/services/search/jobs/{sid}/results",
        headers=headers, params={"output_mode": "json", "count": result_limit},
    )
    results_resp.raise_for_status()
    rows = results_resp.json().get("results") or []
    return {"rows": rows}


async def query_splunk_logs(
    namespace: str,
    pod: Optional[str],
    splunk_config: Dict[str, Any],
    tail_lines: int = 200,
    hours_back: int = 2,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Fetch recent log lines for a namespace (optionally one pod) from a
    customer's Splunk instance. Same output shape as query_athena_logs/
    live_get_pod_logs (`{"namespace", "pod", "logs"}`) so callers don't care
    which source answered. See ADR 0041 for the field-mapping assumption
    this makes about the Splunk event schema — unverified against a real
    instance.
    """
    if err := _validate_k8s_name(namespace, "namespace"):
        return {"error": err}
    if pod and (err := _validate_k8s_name(pod, "pod")):
        return {"error": err}

    splunk_url = splunk_config.get("url", "")
    index = splunk_config.get("index", "")
    token = splunk_config.get("token", "")
    verify_tls = splunk_config.get("verify_tls", True)
    if not (splunk_url and index and token):
        return {"error": "Splunk log platform is not configured (missing url/index/token)"}

    limit = max(1, min(tail_lines, _MAX_ROWS))
    spl = _build_spl(index, namespace, pod, limit)

    try:
        async with httpx.AsyncClient(verify=verify_tls, timeout=15.0) as client:
            result = await _run_query(client, splunk_url, token, spl, hours_back, result_limit=limit)
    except httpx.HTTPError as e:
        logger.warning("splunk query failed: %s", e)
        return {"error": f"Splunk query failed: {e}"}

    if "error" in result:
        return result

    rows: List[Dict[str, Any]] = result.get("rows", [])
    if not rows:
        return {"namespace": namespace, "pod": pod, "logs": ""}

    # Field names assumed per ADR 0041 (Fluent-Bit-style kubernetes.* fields,
    # raw text in _raw) — not verified against a real index. Falls back to
    # Athena's own column names too, in case a future real deployment's
    # Splunk app normalizes to that shape instead.
    lines = []
    for r in rows:
        pod_name = r.get("kubernetes.pod_name") or r.get("pod_name") or pod or "?"
        container_name = r.get("kubernetes.container_name") or r.get("container_name") or "?"
        raw = r.get("_raw") or r.get("log") or ""
        lines.append(f"[{pod_name}/{container_name}] {raw}")

    return {
        "namespace": namespace,
        "pod": pod,
        "logs": redact_log_text("\n".join(lines)),
    }
