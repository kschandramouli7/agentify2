"""live_tools.py — read-only LIVE Kubernetes API calls served over the
persistent collector connection (ADR 0022 Decision #7 / ROADMAP P18 use
case #9).

Adapted from src/agent/k8fy/live_diagnostics.py — copied rather than
imported (this package takes no cross-package dependency on src/agent, same
convention as service_topology.py), rehosted on this package's own
k8s_client.py (_k8s_get/k8s_headers) and log_redaction.py. Every function
here is strictly read-only: get/list on pods, pod logs, and events — no
pods/exec, no mutation, matching the original module's security rationale.

Reachable one way here: dispatched by live_relay.py in response to a Hub
request relayed from the agent's live_* tools (see tools.py's
_dispatch_live_diagnostic in src/agent/k8fy) when a query targets this
cluster rather than the agent's own.
"""

import base64
import binascii
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from cryptography import x509
from cryptography.x509.oid import NameOID

from . import k8s_client
from .log_redaction import redact_log_text

logger = logging.getLogger(__name__)

# Tool names this module serves — the explicit allow-list live_relay.py
# validates inbound requests against, mirroring LIVE_DIAGNOSTIC_TOOLS in
# src/agent/k8fy/live_diagnostics.py and the Hub's own liveFetchAllowedTools
# (src/backend/internal/api/handlers.go) so all three layers agree on
# exactly what this connection can be asked to do.
LIVE_TOOLS = frozenset({
    "live_list_pods",
    "live_get_pod_logs",
    "live_get_events",
    "live_describe_pod",
    "live_get_certificates",
})


async def live_list_pods(namespace: str) -> Dict[str, Any]:
    """List pods in a namespace with a compact live status summary."""
    if not namespace:
        # An empty namespace segment (e.g. /api/v1/namespaces//pods) gets
        # treated by the K8s API server as a CLUSTER-scoped list request —
        # this collector's ServiceAccount only holds a namespace-scoped
        # Role, so that always 403s with a confusing "at the cluster scope"
        # message instead of the actual problem (no namespace was
        # supplied). Fail clearly here instead.
        return {"error": "namespace is required"}
    resp = await k8s_client._k8s_get(f"/api/v1/namespaces/{quote(namespace)}/pods")
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

    resp = await k8s_client._k8s_get(f"/api/v1/namespaces/{quote(namespace)}/pods/{quote(pod)}/log", params)
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

    resp = await k8s_client._k8s_get(f"/api/v1/namespaces/{quote(namespace)}/events", params)
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
    pod_resp = await k8s_client._k8s_get(f"/api/v1/namespaces/{quote(namespace)}/pods/{quote(pod)}")
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


def _parse_cert_summary(name: str, tls_crt_b64: str) -> Optional[Dict[str, Any]]:
    """Decode one Secret's tls.crt and return only the safe summary fields —
    never the certificate bytes themselves, and the private key (tls.key)
    is never even read (see list_tls_secrets). Returns None on any decode/
    parse failure (malformed or non-PEM data) rather than raising, so one
    bad secret doesn't fail the whole namespace's cert check.
    """
    try:
        pem_bytes = base64.b64decode(tls_crt_b64)
        cert = x509.load_pem_x509_certificate(pem_bytes)
    except (binascii.Error, ValueError) as e:
        logger.warning("failed to parse certificate for secret=%s: %s", name, e)
        return None

    common_names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    common_name = common_names[0].value if common_names else None
    expiry = cert.not_valid_after_utc
    days_until_expiry = (expiry - datetime.now(timezone.utc)).days

    return {
        "name": name,
        "common_name": common_name,
        "expiry_date": expiry.isoformat(),
        "days_until_expiry": days_until_expiry,
    }


async def live_get_certificates(namespace: str) -> Dict[str, Any]:
    """Live TLS certificate expiry check for a namespace (ROADMAP P16/P18,
    ADR 0024) — lists this namespace's `kubernetes.io/tls` Secrets and
    returns only {name, common_name, expiry_date, days_until_expiry} per
    cert. days_until_expiry is computed here, deterministically — never
    handed to Claude as arithmetic (same principle already established for
    the ingested-store cert-check flow). Raw certificate/key bytes are
    never included in the return value, by construction: _parse_cert_summary
    only ever builds this summary dict, nothing else touches the decoded
    secret data.
    """
    if not namespace:
        return {"error": "namespace is required"}
    secrets = await k8s_client.list_tls_secrets(namespace)
    certificates = []
    for secret in secrets:
        summary = _parse_cert_summary(secret["name"], secret["tls_crt_b64"])
        if summary is not None:
            certificates.append(summary)
    return {"namespace": namespace, "certificates": certificates}


async def dispatch(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch one inbound {tool, args} request from live_relay.py to the
    matching function above. Callers must check `tool in LIVE_TOOLS` first —
    this raises KeyError for anything else, deliberately, rather than
    silently no-op-ing on an unrecognized tool name.
    """
    if tool == "live_list_pods":
        return await live_list_pods(namespace=args.get("namespace", ""))
    if tool == "live_get_pod_logs":
        return await live_get_pod_logs(
            namespace=args.get("namespace", ""),
            pod=args.get("pod", ""),
            container=args.get("container"),
            tail_lines=int(args.get("tail_lines", 200)),
            previous=bool(args.get("previous", False)),
        )
    if tool == "live_get_events":
        return await live_get_events(namespace=args.get("namespace", ""), pod=args.get("pod"))
    if tool == "live_describe_pod":
        return await live_describe_pod(namespace=args.get("namespace", ""), pod=args.get("pod", ""))
    if tool == "live_get_certificates":
        return await live_get_certificates(namespace=args.get("namespace", ""))
    raise KeyError(f"unrecognized live tool: {tool}")
