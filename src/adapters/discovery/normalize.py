"""normalize.py — normalize raw K8s watch-stream objects into the Hub's
canonical Event schema, and push them to POST /api/ingest (ADR 0027,
merged from the retired src/adapters/k8fy adapter's normalizer.py +
emitter.py).

The event shape mirrors src/backend/internal/models/event.go (Event +
EventTraits); the backend infers the storage backend from the traits (see
context-mesh/policies/storage-strategy.md).

Ported logic, not ported representation: the retired adapter used the
`kubernetes` client library's typed objects (`pod.metadata.name`); this
package works with raw dict JSON everywhere else (ADR 0022 Decision #6 —
no client-library dependency), so every function here reads plain dicts
(`pod["metadata"]["name"]`) instead.

The `k8fy.*` event-namespace strings below are NOT part of this rename —
they're the K8fy pod-mesh taxonomy (ADR 0005), a stable storage/routing
contract baked into the Hub's deriveStoreType/RouteToPods and every
existing Postgres row. Only the adapter component moved; the data
vocabulary it writes into did not.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_event(
    *,
    event_namespace: str,
    event_type: str,
    payload: Dict[str, Any],
    traits: Dict[str, str],
    entity_key: str,
) -> Dict[str, Any]:
    """Assemble a canonical event dict accepted by POST /api/ingest.
    entity_key is the stable identity of the thing the event describes; for
    current-state pods the backend keys storage on it so the latest event
    for an entity overwrites the previous one."""
    return {
        "id": str(uuid.uuid4()),
        "timestamp": _now_rfc3339(),
        "event_namespace": event_namespace,
        "type": event_type,
        "source": "kubernetes-api",
        "payload": payload,
        "traits": traits,
        "entity_key": entity_key,
    }


# Traits for current pod/service state: point lookups against the latest value.
_LIVE_STATE_TRAITS = {
    "shape": "structured",
    "access_pattern": "point-lookup",
    "temporality": "current-state",
    "mutability": "mutable",
    "authority": "derived",
    "retention": "ephemeral",
}

# Traits for append-only metric samples: each scrape is retained as its own
# row so a trend (e.g. restarts climbing over time) is readable (spec 006,
# ADR 0013).
_METRIC_SAMPLE_TRAITS = {
    "shape": "numeric/metric",
    "access_pattern": "time-range-scan",
    "temporality": "append-only",
    "mutability": "immutable",
    "authority": "derived",
    "retention": "30d",
}

# Traits for append-only change/deploy events: each rollout is its own row,
# read by time range to correlate a change against a symptom onset (spec 007).
_CHANGE_EVENT_TRAITS = {
    "shape": "structured",
    "access_pattern": "time-range-scan",
    "temporality": "append-only",
    "mutability": "immutable",
    "authority": "derived",
    "retention": "30d",
}


def _total_restarts(pod: Dict[str, Any]) -> int:
    statuses = pod.get("status", {}).get("containerStatuses", []) or []
    return sum(cs.get("restartCount", 0) for cs in statuses)


def _is_pod_ready(pod: Dict[str, Any]) -> bool:
    for condition in pod.get("status", {}).get("conditions", []) or []:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def normalize_pod_event(
    pod: Dict[str, Any], event_type: str, service: Optional[str] = None
) -> Dict[str, Any]:
    """Convert a watch event for a pod into a canonical event.

    `service` is the Service this pod belongs to, resolved by the caller via
    service_index. It is attributed HERE, at push time, rather than by shipping
    pod labels for the Hub to match: the collector already does this matching
    for dependency mining, and the Hub has no matcher at all. Without it,
    nothing downstream can aggregate pod health per service (ROADMAP P22).

    Omitted from the payload when unresolved rather than sent as null, so a
    consumer cannot mistake "no Service selects this pod" for "attributed to
    nothing".
    """
    metadata = pod.get("metadata", {})
    status = pod.get("status", {})
    payload = {
        "pod_id": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "phase": status.get("phase"),
        "ready": _is_pod_ready(pod),
        "restarts": _total_restarts(pod),
    }
    if service:
        payload["service"] = service
    return _canonical_event(
        event_namespace="k8fy.live-state",
        event_type=f"pod_{event_type.lower()}",
        payload=payload,
        traits=_LIVE_STATE_TRAITS,
        entity_key=metadata.get("name", ""),
    )


def normalize_service_event(svc: Dict[str, Any], event_type: str) -> Dict[str, Any]:
    """Convert a watch event for a service into a canonical event."""
    metadata = svc.get("metadata", {})
    spec = svc.get("spec", {})
    payload = {
        "service": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "cluster_ip": spec.get("clusterIP"),
        "ports": len(spec.get("ports") or []),
    }
    return _canonical_event(
        event_namespace="k8fy.live-state",
        event_type=f"service_{event_type.lower()}",
        payload=payload,
        traits=_LIVE_STATE_TRAITS,
        entity_key=metadata.get("name", ""),
    )


def normalize_metric_event(pod_name: str, namespace: str, container: str, restarts: int) -> Dict[str, Any]:
    """Convert a scraped restart count into an append-only metric sample.
    Emitted to k8fy.metrics (append-only) rather than k8fy.live-state, so
    each scrape is retained as a distinct row and the restart-count trend
    over time is readable (spec 006). The backend keys the row on the event
    id, not entity_key, so samples accumulate instead of overwriting."""
    payload = {"pod_id": pod_name, "namespace": namespace, "container": container, "restarts": restarts}
    return _canonical_event(
        event_namespace="k8fy.metrics",
        event_type="pod_metrics",
        payload=payload,
        traits=_METRIC_SAMPLE_TRAITS,
        entity_key=f"{pod_name}/{container}",
    )


def normalize_deploy_event(
    deployment: Dict[str, Any], revision: str, service: Optional[str] = None
) -> Dict[str, Any]:
    """Convert a Deployment rollout (revision change) into an append-only
    event. Emitted to k8fy.events so diagnosis can align a rollout time with
    a symptom onset (spec 007) — a change record, not current state; every
    revision is retained as its own row."""
    metadata = deployment.get("metadata", {})
    spec = deployment.get("spec", {})
    containers = spec.get("template", {}).get("spec", {}).get("containers", []) or []
    payload = {
        "deployment": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "revision": revision,
        "images": [c["image"] for c in containers if c.get("image")],
        "replicas_desired": spec.get("replicas"),
        "change": "rollout",
    }
    # Resolved from the Deployment's POD TEMPLATE labels, not its own metadata
    # labels — a Service selects the pods, and the two label sets are commonly
    # different. Lets a consumer answer "what image is payment-api running"
    # without a second join.
    if service:
        payload["service"] = service
    return _canonical_event(
        event_namespace="k8fy.events",
        event_type="deploy",
        payload=payload,
        traits=_CHANGE_EVENT_TRAITS,
        entity_key=metadata.get("name", ""),
    )


def normalize_certificate_event(
    secret_name: str,
    namespace: str,
    expires_at: Optional[datetime],
    dns_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Convert a TLS secret's expiry into a canonical event."""
    days_until_expiry: Optional[int] = None
    expires_iso: Optional[str] = None
    if expires_at is not None:
        utc = expires_at.astimezone(timezone.utc)
        # Human-readable UTC string so the UI and chat show the same format:
        # "4 Jul 2026, 02:31 UTC" (day without leading zero). "%-d" is a
        # glibc-only strftime extension that raises on Windows' CRT — built
        # manually instead so this works on every platform, not just Linux
        # containers (a real bug surfaced by testing this port; the retired
        # k8fy adapter had the same "%-d" and was never run outside Linux).
        expires_iso = f"{utc.day} {utc.strftime('%b %Y, %H:%M UTC')}"
        days_until_expiry = (utc - datetime.now(timezone.utc)).days

    payload = {
        "secret": secret_name,
        "namespace": namespace,
        "expires_at": expires_iso,
        "days_until_expiry": days_until_expiry,
        "should_renew": days_until_expiry is not None and days_until_expiry < 30,
        "dns_names": dns_names or [],
    }
    return _canonical_event(
        event_namespace="k8fy.certificates",
        event_type="cert_check",
        payload=payload,
        traits={**_LIVE_STATE_TRAITS, "retention": "30d"},
        entity_key=secret_name,
    )


async def push_event(event: Dict[str, Any], backend_url: str, collector_token: str) -> None:
    """POST one canonical event to the Hub's ingestion endpoint. Best-effort:
    a failed push is logged and swallowed, never raises — same convention as
    every other push_* function in this package, and matches the retired
    adapter's Emitter (a failed POST must never crash a watch/scan loop)."""
    # Omit the header entirely when unset — httpx raises a local
    # (client-side, never-sent) error on a "Bearer " value with an empty
    # token, and an absent credential is exactly what the Hub's
    # resolveTenantContext already tolerates for /api/ingest (defaults to
    # DefaultTenantID) for a single-cluster deployment.
    headers = {"Authorization": f"Bearer {collector_token}"} if collector_token else {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{backend_url.rstrip('/')}/api/ingest",
                json=event,
                headers=headers,
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("push_event failed for event_namespace=%s: %s", event.get("event_namespace"), e)
