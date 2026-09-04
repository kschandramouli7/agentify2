"""k8s_client.py — in-cluster Kubernetes API access for agentify-discovery.

Same auth pattern as src/agent/k8fy/k8s_client.py + live_diagnostics.py
(read the mounted service-account token, call
https://kubernetes.default.svc directly over HTTPS with a Bearer header) —
copied rather than imported, see log_redaction.py's docstring for why.

No `kubernetes` client library dependency, deliberately: raw HTTP calls give
this component direct control over API version-skew fallback (ADR 0022
Decision #6), which a versioned client library would fight rather than help
with.
"""

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx
from cryptography import x509

logger = logging.getLogger(__name__)

_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_API = "https://kubernetes.default.svc"


def k8s_headers(content_type: str = "application/json") -> Dict[str, str]:
    try:
        with open(_SA_TOKEN_PATH) as f:
            token = f.read()
    except OSError:
        return {}
    return {"Authorization": f"Bearer {token}", "Content-Type": content_type}


async def _k8s_get(path: str, params: Optional[Dict[str, str]] = None) -> httpx.Response:
    headers = k8s_headers()
    if not headers:
        raise RuntimeError("service account token unavailable — agentify-discovery requires in-cluster credentials")
    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
        return await client.get(f"{K8S_API}{path}", headers=headers, params=params or {})


async def _group_exists(group: str) -> bool:
    """Whether an API group is registered on this cluster (200 = yes, 404 or
    any other non-200 = no). Used to decide whether it's worth calling a
    CRD-based list function at all — Gateway API and OpenShift Route are
    genuinely optional/distribution-specific, unlike core/apps/v1 groups."""
    try:
        resp = await _k8s_get(f"/apis/{group}")
    except RuntimeError:
        return False
    return resp.status_code == 200


async def discover_api_capabilities() -> Optional[Dict[str, Any]]:
    """Best-effort startup capability check (ADR 0022 Decision #6:
    "API-capability discovery at startup — query /version and /apis, never
    assume a fixed surface"). Logged once at startup.

    Also probes for the two optional, distribution-specific API groups
    ingress mapping (ROADMAP P18 use case #3) needs — Gateway API
    (`gateway.networking.k8s.io`) and OpenShift Route (`route.openshift.io`)
    — under `"gateway_api"`/`"openshift_route"` booleans, so main.py's scan
    loop can skip those list calls entirely on a cluster that doesn't have
    them, rather than eating a 404 (and a log line) every namespace, every
    scan cycle. Ingress itself (`networking.k8s.io/v1`) needs no such gate —
    it's been core since K8s 1.19 and every list function already tolerates
    a missing API gracefully via the same 404-returns-[] fallback.
    """
    try:
        resp = await _k8s_get("/version")
    except RuntimeError as e:
        logger.warning("api capability discovery skipped: %s", e)
        return None
    if resp.status_code != 200:
        logger.warning("GET /version failed (%s): %s", resp.status_code, resp.text[:200])
        return None
    caps = resp.json()
    caps["gateway_api"] = await _group_exists("gateway.networking.k8s.io/v1")
    caps["openshift_route"] = await _group_exists("route.openshift.io/v1")
    return caps


async def list_namespaces(exclude: Optional[set] = None) -> List[str]:
    """List every namespace this ServiceAccount can see, minus `exclude`."""
    exclude = exclude or set()
    resp = await _k8s_get("/api/v1/namespaces")
    if resp.status_code != 200:
        logger.warning("list namespaces failed (%s): %s", resp.status_code, resp.text[:200])
        return []
    items = resp.json().get("items", [])
    return [
        name for item in items
        if (name := item.get("metadata", {}).get("name", "")) and name not in exclude
    ]


async def list_services(namespace: str) -> List[Dict[str, Any]]:
    """List Services in `namespace` as `{"name": ..., "selector": {...}}`.
    Service *names* are the ground truth extract_service_mentions cross-
    validates candidates against; each Service's `selector` is how a pod is
    matched back to the service it belongs to (see main.py's
    `_service_for_pod` — the same label-matching semantics K8s itself uses
    to build Service endpoints, not a pod-name-guessing heuristic).

    Queried directly from this cluster's own K8s API rather than the Hub's
    GET /admin/tracked (see the agentify-discovery plan: that endpoint has
    no tenant scoping, so reusing it would leak cross-tenant service names
    into extraction validation).
    """
    resp = await _k8s_get(f"/api/v1/namespaces/{quote(namespace)}/services")
    if resp.status_code != 200:
        logger.warning("list services failed for namespace=%s (%s): %s", namespace, resp.status_code, resp.text[:200])
        return []
    items = resp.json().get("items", [])
    services = []
    for item in items:
        name = item.get("metadata", {}).get("name", "")
        if not name:
            continue
        spec = item.get("spec", {}) or {}
        # Everything below arrives in the SAME response we already make; it was
        # being discarded. spec.type/ports/clusterIP are what turn an anonymous
        # box on the dependency diagram into "an internally-exposed HTTPS
        # service on 8443" (ROADMAP P22 service profile).
        services.append({
            "name": name,
            "selector": spec.get("selector") or {},
            "type": spec.get("type") or "ClusterIP",
            # clusterIP "None" means headless — no VIP, used for peer discovery
            # by StatefulSets. Worth distinguishing from a normal ClusterIP.
            "headless": (spec.get("clusterIP") == "None"),
            "ports": [
                {
                    "name": pt.get("name") or "",
                    "port": pt.get("port"),
                    "protocol": pt.get("protocol") or "TCP",
                }
                for pt in (spec.get("ports") or [])
            ],
        })
    return services


async def list_pods(namespace: str) -> List[Dict[str, Any]]:
    """List pods in `namespace` as `{"name": ..., "labels": {...}}`."""
    resp = await _k8s_get(f"/api/v1/namespaces/{quote(namespace)}/pods")
    if resp.status_code != 200:
        logger.warning("list pods failed for namespace=%s (%s): %s", namespace, resp.status_code, resp.text[:200])
        return []
    items = resp.json().get("items", [])
    pods = []
    for item in items:
        name = item.get("metadata", {}).get("name", "")
        if not name:
            continue
        pods.append({"name": name, "labels": item.get("metadata", {}).get("labels") or {}})
    return pods


async def list_pod_health(namespace: str) -> Dict[str, int]:
    """Aggregate pod readiness in `namespace` as `{"total": N, "ready": M}`
    (ROADMAP P18 use case #5). A pod counts as ready when it has a `Ready`
    condition with status "True" — the standard K8s pod-level readiness
    signal (kubectl's own convention), not a per-container check. Missing
    `status.conditions` (e.g. a pod still Pending) counts as not-ready,
    never raises. Separate call from list_pods (same endpoint, different
    extraction) rather than widening list_pods' shape, so log-topology
    mining's existing consumers are untouched."""
    resp = await _k8s_get(f"/api/v1/namespaces/{quote(namespace)}/pods")
    if resp.status_code != 200:
        logger.warning("list pod health failed for namespace=%s (%s): %s", namespace, resp.status_code, resp.text[:200])
        return {"total": 0, "ready": 0}
    items = resp.json().get("items", [])
    total = 0
    ready = 0
    for item in items:
        if not item.get("metadata", {}).get("name", ""):
            continue
        total += 1
        conditions = item.get("status", {}).get("conditions", []) or []
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            ready += 1
    return {"total": total, "ready": ready}


async def list_tls_secrets(namespace: str) -> List[Dict[str, str]]:
    """List TLS Secrets in `namespace` as `{"name": ..., "tls_crt_b64": ...}`
    (ROADMAP P16/P18 use case unlocked by ADR 0024's live_get_certificates).

    Filtered server-side to `type=kubernetes.io/tls` via a field selector —
    never lists arbitrary Secrets. Returns the still-base64-encoded `tls.crt`
    field only; decoding/parsing (and NEVER returning it downstream) is
    live_tools.py's job, not this thin transport layer's. `tls.key` (the
    private key) is never read here at all — only `tls.crt` is extracted
    from each Secret's data.
    """
    params = {"fieldSelector": "type=kubernetes.io/tls"}
    resp = await _k8s_get(f"/api/v1/namespaces/{quote(namespace)}/secrets", params)
    if resp.status_code != 200:
        logger.warning("list tls secrets failed for namespace=%s (%s): %s", namespace, resp.status_code, resp.text[:200])
        return []
    items = resp.json().get("items", [])
    secrets = []
    for item in items:
        name = item.get("metadata", {}).get("name", "")
        tls_crt_b64 = item.get("data", {}).get("tls.crt", "")
        if not name or not tls_crt_b64:
            continue
        secrets.append({"name": name, "tls_crt_b64": tls_crt_b64})
    return secrets


async def _list_apps_v1_objects(namespace: str, resource: str) -> List[Dict[str, Any]]:
    """Workloads in `namespace` with the fields a service profile needs.

    Same request `_list_apps_v1_names` already makes — it extracted names and
    dropped the rest. Replica counts and the container image are the difference
    between "payment-api" and "payment-api · Deployment · 3/3 · nginx:1.25".
    """
    resp = await _k8s_get(f"/apis/apps/v1/namespaces/{quote(namespace)}/{resource}")
    if resp is None or resp.status_code != 200:
        return []
    out: List[Dict[str, Any]] = []
    for item in resp.json().get("items", []):
        meta = item.get("metadata", {}) or {}
        spec = item.get("spec", {}) or {}
        status = item.get("status", {}) or {}
        name = meta.get("name", "")
        if not name:
            continue
        containers = ((spec.get("template", {}) or {}).get("spec", {}) or {}).get("containers", []) or []
        out.append({
            "name": name,
            # The pod TEMPLATE labels are what a Service selects on — the same
            # distinction service_index documents for attribution.
            "template_labels": (((spec.get("template", {}) or {}).get("metadata", {}) or {}).get("labels") or {}),
            "replicas_desired": spec.get("replicas"),
            # DaemonSets have no spec.replicas; their status carries the counts.
            "replicas_ready": status.get("readyReplicas", status.get("numberReady")),
            "images": [c["image"] for c in containers if c.get("image")],
        })
    return out


async def _list_apps_v1_names(namespace: str, resource: str) -> List[str]:
    """Shared list-and-extract-names helper for the apps/v1 workload kinds
    below — identical shape to list_services/list_pods, just against a
    different API group/resource."""
    resp = await _k8s_get(f"/apis/apps/v1/namespaces/{quote(namespace)}/{resource}")
    if resp.status_code != 200:
        logger.warning("list %s failed for namespace=%s (%s): %s", resource, namespace, resp.status_code, resp.text[:200])
        return []
    items = resp.json().get("items", [])
    return [name for item in items if (name := item.get("metadata", {}).get("name", ""))]


async def list_workloads(namespace: str) -> List[Dict[str, Any]]:
    """Every apps/v1 workload in `namespace`, tagged with its kind.

    Kind is the most classifying single fact about a service: a Deployment
    serves, a StatefulSet holds state, a DaemonSet runs per node. CronJobs live
    under batch/v1 and are fetched separately below.
    """
    out: List[Dict[str, Any]] = []
    for resource, kind in (
        ("deployments", "Deployment"),
        ("statefulsets", "StatefulSet"),
        ("daemonsets", "DaemonSet"),
    ):
        for w in await _list_apps_v1_objects(namespace, resource):
            out.append({**w, "kind": kind})
    for w in await _list_cronjobs(namespace):
        out.append(w)
    return out


async def _list_cronjobs(namespace: str) -> List[Dict[str, Any]]:
    """CronJobs (batch/v1) — the one workload kind that says "batch, on a
    schedule" rather than "serving", and the schedule itself is worth showing."""
    resp = await _k8s_get(f"/apis/batch/v1/namespaces/{quote(namespace)}/cronjobs")
    if resp is None or resp.status_code != 200:
        return []
    out: List[Dict[str, Any]] = []
    for item in resp.json().get("items", []):
        meta = item.get("metadata", {}) or {}
        spec = item.get("spec", {}) or {}
        name = meta.get("name", "")
        if not name:
            continue
        job = ((spec.get("jobTemplate", {}) or {}).get("spec", {}) or {}).get("template", {}) or {}
        containers = ((job.get("spec", {}) or {}).get("containers", []) or [])
        out.append({
            "kind": "CronJob",
            "name": name,
            "template_labels": ((job.get("metadata", {}) or {}).get("labels") or {}),
            "replicas_desired": None,
            "replicas_ready": None,
            "images": [c["image"] for c in containers if c.get("image")],
            "schedule": spec.get("schedule") or "",
        })
    return out


async def list_deployments(namespace: str) -> List[str]:
    """List Deployment names in `namespace` (apps/v1)."""
    return await _list_apps_v1_names(namespace, "deployments")


async def list_statefulsets(namespace: str) -> List[str]:
    """List StatefulSet names in `namespace` (apps/v1)."""
    return await _list_apps_v1_names(namespace, "statefulsets")


async def list_daemonsets(namespace: str) -> List[str]:
    """List DaemonSet names in `namespace` (apps/v1)."""
    return await _list_apps_v1_names(namespace, "daemonsets")


async def get_pod_logs(namespace: str, pod: str, tail_lines: int = 200) -> str:
    """Fetch a bounded, unredacted tail of a pod's current logs. Callers
    must redact before this text leaves the cluster (see log_redaction.py)."""
    params = {"tailLines": str(max(1, min(tail_lines, 1000)))}
    resp = await _k8s_get(f"/api/v1/namespaces/{quote(namespace)}/pods/{quote(pod)}/log", params)
    if resp.status_code != 200:
        logger.warning("get pod logs failed for %s/%s (%s): %s", namespace, pod, resp.status_code, resp.text[:200])
        return ""
    return resp.text


# ── Ingress/entry-point mapping (ROADMAP P18 use case #3) ───────────────────
# Ingress is core-ish (networking.k8s.io/v1, present since K8s 1.19) and
# needs no capability gate. Gateway API and OpenShift Route are genuinely
# optional/distribution-specific — main.py only calls list_gateways/
# list_httproutes/list_routes when discover_api_capabilities() said the
# corresponding group exists, but every function here still degrades
# gracefully (returns []) on a 404 regardless, same as every list function
# above.

async def list_ingresses(namespace: str) -> List[Dict[str, Any]]:
    """List Ingresses in `namespace` as
    `{"name", "hosts": [...], "backend_services": [...]}` — one flattened
    entry per Ingress object, not per host/backend pair (ingress.py does
    that flattening); `hosts`/`backend_services` are deduplicated, order-
    preserving lists gathered across every rule."""
    resp = await _k8s_get(f"/apis/networking.k8s.io/v1/namespaces/{quote(namespace)}/ingresses")
    if resp.status_code != 200:
        logger.warning("list ingresses failed for namespace=%s (%s): %s", namespace, resp.status_code, resp.text[:200])
        return []
    items = resp.json().get("items", [])
    result = []
    for item in items:
        name = item.get("metadata", {}).get("name", "")
        if not name:
            continue
        spec = item.get("spec", {})
        hosts: List[str] = []
        backends: List[str] = []
        for rule in spec.get("rules", []) or []:
            host = rule.get("host", "")
            if host and host not in hosts:
                hosts.append(host)
            for path in rule.get("http", {}).get("paths", []) or []:
                svc = path.get("backend", {}).get("service", {}).get("name", "")
                if svc and svc not in backends:
                    backends.append(svc)
        default_svc = spec.get("defaultBackend", {}).get("service", {}).get("name", "")
        if default_svc and default_svc not in backends:
            backends.append(default_svc)
        result.append({"name": name, "hosts": hosts, "backend_services": backends})
    return result


async def list_gateways(namespace: str) -> List[Dict[str, Any]]:
    """List Gateway API Gateways in `namespace` as
    `{"name", "listeners": [{"name", "hostname", "port"}]}`. Only called when
    discover_api_capabilities() found the gateway.networking.k8s.io group;
    still returns [] on a 404 regardless, same as every list function here."""
    resp = await _k8s_get(f"/apis/gateway.networking.k8s.io/v1/namespaces/{quote(namespace)}/gateways")
    if resp.status_code != 200:
        logger.warning("list gateways failed for namespace=%s (%s): %s", namespace, resp.status_code, resp.text[:200])
        return []
    items = resp.json().get("items", [])
    result = []
    for item in items:
        name = item.get("metadata", {}).get("name", "")
        if not name:
            continue
        listeners = [
            {"name": l.get("name", ""), "hostname": l.get("hostname", ""), "port": l.get("port", 0)}
            for l in item.get("spec", {}).get("listeners", []) or []
        ]
        result.append({"name": name, "listeners": listeners})
    return result


async def list_httproutes(namespace: str) -> List[Dict[str, Any]]:
    """List Gateway API HTTPRoutes in `namespace` as
    `{"name", "hostnames": [...], "parent_refs": [{"name", "namespace",
    "section_name"}], "backend_services": [...]}`. `parent_refs["namespace"]`
    defaults to `namespace` (this route's own) per the Gateway API spec when
    the route doesn't set one explicitly — resolved here, not left for the
    caller to default."""
    resp = await _k8s_get(f"/apis/gateway.networking.k8s.io/v1/namespaces/{quote(namespace)}/httproutes")
    if resp.status_code != 200:
        logger.warning("list httproutes failed for namespace=%s (%s): %s", namespace, resp.status_code, resp.text[:200])
        return []
    items = resp.json().get("items", [])
    result = []
    for item in items:
        name = item.get("metadata", {}).get("name", "")
        if not name:
            continue
        spec = item.get("spec", {})
        parent_refs = [
            {
                "name": ref.get("name", ""),
                "namespace": ref.get("namespace") or namespace,
                "section_name": ref.get("sectionName", ""),
            }
            for ref in spec.get("parentRefs", []) or []
        ]
        backends: List[str] = []
        for rule in spec.get("rules", []) or []:
            for ref in rule.get("backendRefs", []) or []:
                svc = ref.get("name", "")
                if svc and svc not in backends:
                    backends.append(svc)
        result.append({
            "name": name,
            "hostnames": spec.get("hostnames", []) or [],
            "parent_refs": parent_refs,
            "backend_services": backends,
        })
    return result


async def list_routes(namespace: str) -> List[Dict[str, str]]:
    """List OpenShift Routes in `namespace` as `{"name", "host",
    "backend_service"}` — primary `spec.to.name` only; weighted
    `alternateBackends` routing is out of scope for v1. Only called when
    discover_api_capabilities() found the route.openshift.io group."""
    resp = await _k8s_get(f"/apis/route.openshift.io/v1/namespaces/{quote(namespace)}/routes")
    if resp.status_code != 200:
        logger.warning("list routes failed for namespace=%s (%s): %s", namespace, resp.status_code, resp.text[:200])
        return []
    items = resp.json().get("items", [])
    result = []
    for item in items:
        name = item.get("metadata", {}).get("name", "")
        if not name:
            continue
        spec = item.get("spec", {})
        result.append({
            "name": name,
            "host": spec.get("host", ""),
            "backend_service": spec.get("to", {}).get("name", ""),
        })
    return result


# ── Watch streams + periodic scrape support (ADR 0027 — merged from the
# retired src/adapters/k8fy adapter) ─────────────────────────────────────────

async def watch_resource(path: str, params: Optional[Dict[str, str]] = None) -> AsyncIterator[Dict[str, Any]]:
    """Async generator over a K8s watch stream (`?watch=1`) at `path`. Yields
    parsed `{"type": "ADDED"|"MODIFIED"|"DELETED", "object": {...}}` dicts,
    one per line, until the connection drops or the caller stops iterating.
    Raises on connection failure — callers own reconnect/backoff (see
    watch.py's run_forever, same discipline as live_relay.py's WebSocket
    reconnect loop). No `resourceVersion` continuity: each (re)connect does a
    fresh LIST-then-WATCH, same behavior the retired k8fy adapter had — this
    is also what lets a freshly (re)started process re-populate current_state
    from scratch via ADDED events for everything that currently exists.
    """
    headers = k8s_headers()
    if not headers:
        raise RuntimeError("service account token unavailable — agentify-discovery requires in-cluster credentials")
    watch_params = {**(params or {}), "watch": "1"}
    async with httpx.AsyncClient(timeout=None, verify=False) as client:
        async with client.stream("GET", f"{K8S_API}{path}", headers=headers, params=watch_params) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line:
                    yield json.loads(line)


async def list_container_restarts(namespace: str) -> List[Dict[str, Any]]:
    """List per-container restart counts in `namespace` as one entry per
    container: `{"pod_id", "namespace", "container", "restarts"}`. Powers the
    periodic metrics scan (ADR 0027) — the append-only k8fy.metrics samples
    that make a restart trend over time readable (spec 006)."""
    resp = await _k8s_get(f"/api/v1/namespaces/{quote(namespace)}/pods")
    if resp.status_code != 200:
        logger.warning("list container restarts failed for namespace=%s (%s): %s", namespace, resp.status_code, resp.text[:200])
        return []
    items = resp.json().get("items", [])
    result = []
    for item in items:
        pod_id = item.get("metadata", {}).get("name", "")
        if not pod_id:
            continue
        for cs in item.get("status", {}).get("containerStatuses", []) or []:
            container = cs.get("name", "")
            if not container:
                continue
            result.append({
                "pod_id": pod_id, "namespace": namespace,
                "container": container, "restarts": cs.get("restartCount", 0),
            })
    return result


def parse_cert_expiry(cert_b64: str) -> Tuple[Optional[datetime], List[str]]:
    """Decode a base64 PEM certificate and return (NotAfter UTC, DNS names).

    DNS names come from the Subject Alternative Name extension first, falling
    back to the Subject CN when no SAN extension is present. Returns
    (None, []) on any parse error rather than raising — one bad secret must
    never abort a whole namespace's cert scan. Kept independent of
    live_tools.py's `_parse_cert_summary` (which returns a different, already
    Claude-facing summary shape) rather than refactored to share it — same
    x509-parsing primitives, deliberately not coupled to that function's
    tested, narrower return contract.
    """
    try:
        pem_bytes = base64.b64decode(cert_b64)
        cert = x509.load_pem_x509_certificate(pem_bytes)

        expires = getattr(cert, "not_valid_after_utc", None)
        if expires is None:
            expires = cert.not_valid_after.replace(tzinfo=timezone.utc)

        dns_names: List[str] = []
        try:
            from cryptography.x509.oid import ExtensionOID
            san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
            dns_names = [v.value for v in san.value if isinstance(v, x509.DNSName)]
        except x509.ExtensionNotFound:
            pass
        if not dns_names:
            cn_attrs = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
            if cn_attrs:
                dns_names = [cn_attrs[0].value]

        return expires, dns_names
    except Exception as exc:  # noqa: BLE001 — malformed cert data must degrade, never crash the scan
        logger.warning("failed to parse certificate: %s", exc)
        return None, []
    return result
