"""live_diagnostics.py — read-only LIVE Kubernetes API calls (not the platform's
ingested/cached store).

These are the only functions that call the live Kubernetes API on demand —
distinct from the rest of the tool set (tools.py), which reads data
agentify-discovery (ADR 0022/0027 — the per-cluster collector; absorbed the
original k8fy-adapter's ingestion role) has already pushed into the ingested
store. Every function here is strictly read-only:
`get`/`list` on pods, pod logs, and events. None of them can mutate anything,
and none of them implement `pods/exec` (shell into a container) — that is a
fundamentally different, much higher-risk capability and is intentionally not
part of this module.

Reachable two ways:
  1. As Claude-callable tools (registered in tools.py's TOOLS list) — Claude
     may call these itself mid-conversation when cached data isn't fresh
     enough.
  2. Directly via app.py's POST /live-tool-call endpoint, which the UI's
     "Run" buttons hit — no LLM call involved, so a human can re-run a
     recommended diagnostic command and see fresh output immediately.

Uses the same in-cluster service-account-token pattern as action_executor.py
(see k8s_client.py). RBAC: infra/kubernetes/payments-test/serviceaccounts.yaml,
Role "agent-live-diagnostics" — get/list pods, get pods/log, get/list events.
"""

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from k8fy.k8s_client import K8S_API, k8s_headers
from k8fy.log_redaction import redact_log_text

logger = logging.getLogger(__name__)

# Tool names this module exposes — the explicit allow-list app.py's
# /live-tool-call endpoint validates against. Keep in sync with the
# functions below and with their TOOLS entries in tools.py.
LIVE_DIAGNOSTIC_TOOLS = frozenset({
    "live_list_pods",
    "live_get_pod_logs",
    "live_get_events",
    "live_describe_pod",
    # live_get_certificates (ROADMAP P16/P18, ADR 0024) has NO local
    # implementation in this module — it's remote-only, always requires an
    # explicit cluster_id and is served by agentify-discovery's
    # live_tools.py over the persistent-connection relay (use case #9).
    # Listed here anyway so process_tool_call/_dispatch_live_diagnostic and
    # app.py's /live-tool-call allow-list route it correctly.
    "live_get_certificates",
})


async def _k8s_get(path: str, params: Optional[Dict[str, str]] = None) -> httpx.Response:
    headers = k8s_headers()
    if not headers:
        raise RuntimeError("service account token unavailable — live_diagnostics requires in-cluster credentials")
    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
        return await client.get(f"{K8S_API}{path}", headers=headers, params=params or {})


async def live_list_pods(namespace: str) -> Dict[str, Any]:
    """List pods in a namespace with a compact live status summary."""
    if not namespace:
        # An empty namespace segment (e.g. /api/v1/namespaces//pods) gets
        # treated by the K8s API server as a CLUSTER-scoped list request —
        # this agent's ServiceAccount only holds a namespace-scoped Role, so
        # that always 403s with a confusing "at the cluster scope" message
        # instead of the actual problem (no namespace was supplied). Fail
        # clearly here instead.
        return {"error": "namespace is required"}
    try:
        resp = await _k8s_get(f"/api/v1/namespaces/{quote(namespace)}/pods")
    except RuntimeError as e:
        return {"error": str(e)}
    if resp.status_code != 200:
        return {"error": f"list pods failed ({resp.status_code}): {resp.text[:300]}"}

    items = resp.json().get("items", [])
    pods: List[Dict[str, Any]] = []
    for item in items:
        status = item.get("status", {})
        containers = status.get("containerStatuses", [])
        restarts = sum(c.get("restartCount", 0) for c in containers)
        ready = all(c.get("ready") for c in containers) if containers else False
        pods.append({
            "name": item.get("metadata", {}).get("name", ""),
            "phase": status.get("phase", "Unknown"),
            "ready": ready,
            "restart_count": restarts,
            "node": item.get("spec", {}).get("nodeName", ""),
        })
    return {"namespace": namespace, "pods": pods}


async def live_get_pod_logs(
    namespace: str,
    pod: str,
    container: Optional[str] = None,
    tail_lines: int = 200,
    previous: bool = False,
) -> Dict[str, Any]:
    """Fetch a live, bounded, redacted tail of a pod's current logs."""
    if not namespace or not pod:
        return {"error": "namespace and pod are required"}
    params: Dict[str, str] = {"tailLines": str(max(1, min(tail_lines, 1000)))}
    if container:
        params["container"] = container
    if previous:
        params["previous"] = "true"

    try:
        resp = await _k8s_get(f"/api/v1/namespaces/{quote(namespace)}/pods/{quote(pod)}/log", params)
    except RuntimeError as e:
        return {"error": str(e)}
    if resp.status_code != 200:
        return {"error": f"log fetch failed ({resp.status_code}): {resp.text[:300]}"}

    return {
        "namespace": namespace,
        "pod": pod,
        "container": container,
        "previous": previous,
        "logs": redact_log_text(resp.text),
    }


async def live_get_events(namespace: str, pod: Optional[str] = None) -> Dict[str, Any]:
    """List recent live events in a namespace, optionally filtered to one pod."""
    if not namespace:
        return {"error": "namespace is required"}
    params: Dict[str, str] = {}
    if pod:
        params["fieldSelector"] = f"involvedObject.name={pod}"

    try:
        resp = await _k8s_get(f"/api/v1/namespaces/{quote(namespace)}/events", params)
    except RuntimeError as e:
        return {"error": str(e)}
    if resp.status_code != 200:
        return {"error": f"list events failed ({resp.status_code}): {resp.text[:300]}"}

    items = resp.json().get("items", [])
    items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)
    events = [{
        "type": e.get("type"),
        "reason": e.get("reason"),
        "message": e.get("message"),
        "count": e.get("count"),
        "last_timestamp": e.get("lastTimestamp") or e.get("eventTime"),
        "involved_object": e.get("involvedObject", {}).get("name"),
    } for e in items[:50]]
    return {"namespace": namespace, "pod": pod, "events": events}


async def live_describe_pod(namespace: str, pod: str) -> Dict[str, Any]:
    """Approximate `kubectl describe pod` — pod spec/status summary + its recent events."""
    if not namespace or not pod:
        return {"error": "namespace and pod are required"}
    try:
        pod_resp = await _k8s_get(f"/api/v1/namespaces/{quote(namespace)}/pods/{quote(pod)}")
    except RuntimeError as e:
        return {"error": str(e)}
    if pod_resp.status_code != 200:
        return {"error": f"get pod failed ({pod_resp.status_code}): {pod_resp.text[:300]}"}

    body = pod_resp.json()
    status = body.get("status", {})
    containers = [{
        "name": c.get("name"),
        "image": c.get("image"),
        "ready": c.get("ready"),
        "restart_count": c.get("restartCount"),
        "state": next(iter(c.get("state", {}).keys()), "unknown"),
        "last_state": next(iter(c.get("lastState", {}).keys()), None),
    } for c in status.get("containerStatuses", [])]

    events_result = await live_get_events(namespace, pod)

    return {
        "namespace": namespace,
        "pod": pod,
        "phase": status.get("phase"),
        "node": body.get("spec", {}).get("nodeName"),
        "conditions": [
            {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
            for c in status.get("conditions", [])
        ],
        "containers": containers,
        "events": events_result.get("events", []),
    }
