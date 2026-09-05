"""Entry point for agentify-discovery (ADR 0022 / ROADMAP P18).

A deterministic, non-agentic, per-cluster collector. One long-running
Deployment per cluster, not a CronJob + separate API server (ADR 0022
Decision #6). Three independent background tasks (`_run_all`):
- **Scan cycle** (`_run`, fixed `SCAN_INTERVAL_SECONDS` ticker): namespace/
  service/deployment inventory, ingress/entry-point mapping, fleet health
  snapshot, per-namespace log-mined service-dependency edges, periodic
  metrics/cert scraping (ADR 0027).
- **Live relay** (`live_relay.py`, ADR 0022 Decision #7 / ROADMAP P18 use
  case #9): the persistent outbound connection for on-demand drill-down.
- **Watch streams** (`watch.py`, ADR 0027): continuous pod/service/
  deployment change events, merged in from the retired `src/adapters/k8fy`
  adapter — this is what keeps `current_state`/`events` current in
  real time between scan cycles, not the scan cycle itself.
"""

import asyncio
import logging
import signal
import sys
import threading
from typing import Any, Dict, List, Optional, Set

from . import k8s_client, live_relay, normalize, watch
from .config import Config, load_from_env
from .service_index import INDEX, service_for_labels
from .health import serve_health
from .health_snapshot import push_health
from .ingress import build_ingress_entries, build_route_entries, correlate_gateway_routes, push_ingress
from .inventory import push_inventory
from .log_redaction import redact_log_text
from .service_topology import (
    extract_external_mentions,
    extract_service_mentions,
    push_dependency,
    push_scan_coverage,
)

logger = logging.getLogger("agentify.discovery")


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter('{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def _service_for_pod(pod_labels: Dict[str, str], services: List[Dict[str, Any]]) -> Optional[str]:
    """Which Service (by name) a pod belongs to.

    Thin wrapper kept for this module's callers and tests; the implementation
    moved to service_index so watch.py can share it (main imports watch, so the
    reverse import would be circular).
    """
    return service_for_labels(pod_labels, services)


async def _scan_namespace(ns: str, cfg: Config, known_namespaces: Optional[Set[str]] = None) -> None:
    services = await k8s_client.list_services(ns)
    # Seed the shared index from the list this scan already fetched. Free, and
    # it closes the startup window in which the pods watch can deliver events
    # before the services watch has been heard from.
    INDEX.seed(ns, services)
    known = {s["name"] for s in services}
    if not known:
        return

    # Every known Service gets its scan_cycles advanced, including ones with no
    # pods at all and ones whose pods never make the sample. That is the whole
    # point of a denominator: "scanned 2880 times, sampled twice" is a different
    # problem from "called rarely", and evidence_count cannot tell them apart
    # (ROADMAP P27 phase 1).
    coverage: Dict[str, Dict[str, int]] = {
        name: {"scan_cycles": 1, "pods_seen": 0, "pods_sampled": 0, "logs_readable": 0, "log_lines": 0}
        for name in known
    }

    # Attribute the FULL pod list before truncating: pods_seen counts what
    # exists, pods_sampled counts what we looked at. Using the truncated list
    # for both would report full coverage of a 5-pod sample and hide the
    # sampling entirely — the exact self-flattery this item exists to prevent.
    all_pods = await k8s_client.list_pods(ns)
    attributed = [(pod, _service_for_pod(pod["labels"], services)) for pod in all_pods]
    for _pod, from_service in attributed:
        if from_service in coverage:
            coverage[from_service]["pods_seen"] += 1

    for pod, from_service in attributed[: cfg.max_pods_per_namespace]:
        if not from_service:
            continue  # can't attribute this pod's mentions to an edge without a from_service
        coverage[from_service]["pods_sampled"] += 1

        raw_logs = await k8s_client.get_pod_logs(ns, pod["name"], tail_lines=cfg.log_tail_lines)
        if not raw_logs:
            # Counted as sampled-but-unreadable, deliberately distinct from
            # "read it and found no mentions": an unreadable log is a platform
            # problem (OPS-9 returns "" for every multi-container pod), while an
            # empty extraction is a real observation.
            continue
        coverage[from_service]["logs_readable"] += 1
        coverage[from_service]["log_lines"] += raw_logs.count("\n") + 1
        logs = redact_log_text(raw_logs)

        for to_service in extract_service_mentions(logs, ns, known):
            if to_service == from_service:
                continue  # self-mention, not a dependency
            await push_dependency(ns, from_service, to_service, cfg.backend_url, cfg.collector_token)

        # Beyond the namespace boundary (ROADMAP P27 phase 3): the calls that
        # made the old diagram claim each namespace was a closed system —
        # vault.vault, api.anthropic.com, an RDS endpoint. A weaker tier by
        # construction (nothing validates a public hostname), pushed with its
        # kind so the two never merge.
        for kind, target in extract_external_mentions(logs, ns, known_namespaces or set()):
            await push_dependency(
                ns, from_service, target, cfg.backend_url, cfg.collector_token, target_kind=kind,
            )

    await push_scan_coverage(ns, coverage, cfg.backend_url, cfg.collector_token)


async def _service_profiles(ns: str, services: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach each Service's workload facts to it — the "service profile".

    Every field here comes from requests the collector already made and then
    discarded: list_services returned spec.type/ports and kept only the
    selector; the workload list was reduced to a boolean ("does this namespace
    have workloads"). Nothing new is fetched and no new RBAC is needed.

    Workloads are matched to Services by the SAME selector-to-pod-label rule
    used everywhere else (service_index), applied to the workload's pod
    TEMPLATE labels — a Service selects the pods, and a workload's own metadata
    labels commonly differ from its template's.

    A Service with no matching workload keeps a profile with no kind, which is
    itself informative: something is fronting pods nothing in this namespace
    declares.
    """
    workloads = await k8s_client.list_workloads(ns)
    profiles = []
    for svc in services:
        selector = svc.get("selector") or {}
        match = None
        if selector:
            for w in workloads:
                labels = w.get("template_labels") or {}
                if all(labels.get(k) == v for k, v in selector.items()):
                    match = w
                    break
        profiles.append({
            "service": svc["name"],
            "service_type": ("Headless" if svc.get("headless") else svc.get("type") or "ClusterIP"),
            "ports": svc.get("ports") or [],
            "workload_kind": (match or {}).get("kind") or "",
            "replicas_desired": (match or {}).get("replicas_desired"),
            "replicas_ready": (match or {}).get("replicas_ready"),
            "image": ((match or {}).get("images") or [None])[0],
            "schedule": (match or {}).get("schedule") or "",
        })
    return profiles


async def _namespace_services(ns: str) -> Optional[List[Dict[str, Any]]]:
    """This namespace's services (name + selector) if it's "active" — has at
    least one Service, Deployment, StatefulSet, or DaemonSet — else None
    (excludes empty namespaces the ServiceAccount can merely list). ROADMAP
    P18 use case #1 only needed the active/inactive bool; ROADMAP P16 / ADR
    0023's service->cluster registry needs the real names too, and ADR
    0029 (Glue-based dependency mining) needs each one's selector on top of
    that — all three already on hand via this one list_services call
    (main.py's own _service_for_pod already matches against the exact same
    selector dicts for its live from_service resolution), nothing extra to
    fetch.
    """
    services = await k8s_client.list_services(ns)
    if services:
        return services  # [{"name": ..., "selector": {...}}, ...] already
    if await k8s_client.list_deployments(ns) or await k8s_client.list_statefulsets(ns) or await k8s_client.list_daemonsets(ns):
        return []  # active (has workloads) but no Service fronts them
    return None  # inactive


async def _scan_inventory(namespaces: List[str], cfg: Config) -> None:
    namespace_services: Dict[str, List[Dict[str, Any]]] = {}
    for ns in namespaces:
        services = await _namespace_services(ns)
        if services is None:
            continue
        # Enrich each Service entry with its workload profile before pushing.
        # The profile rides the EXISTING inventory push rather than a second
        # request: cluster_services already is the registry of "what services
        # exist", so "what they are" belongs on the same row. A separate table
        # could disagree with this one.
        try:
            profiles = {p["service"]: p for p in await _service_profiles(ns, services)}
        except Exception:  # noqa: BLE001
            logger.exception("service profile build failed for namespace=%s", ns)
            profiles = {}
        namespace_services[ns] = [
            {**svc, **{k: v for k, v in profiles.get(svc["name"], {}).items() if k != "service"}}
            for svc in services
        ]
    if namespace_services:
        await push_inventory(namespace_services, cfg.backend_url, cfg.collector_token)


async def _scan_ingress(namespaces: List[str], cfg: Config, caps: Optional[Dict[str, Any]]) -> None:
    """Ingress/entry-point mapping (ROADMAP P18 use case #3). Ingress needs no
    capability gate (core-ish, tolerates a missing API on its own); Gateway
    API and OpenShift Route are only scanned when discover_api_capabilities
    found the corresponding group, so a cluster without either CRD installed
    never pays for a 404 per namespace per cycle.
    """
    gateway_api = bool(caps and caps.get("gateway_api"))
    openshift_route = bool(caps and caps.get("openshift_route"))

    gateways_by_key: Dict[Any, Dict[str, Any]] = {}
    if gateway_api:
        for ns in namespaces:
            for gw in await k8s_client.list_gateways(ns):
                gateways_by_key[(ns, gw["name"])] = gw

    entries: List[Dict[str, str]] = []
    for ns in namespaces:
        entries.extend(build_ingress_entries(ns, await k8s_client.list_ingresses(ns)))
        if gateway_api:
            entries.extend(correlate_gateway_routes(ns, await k8s_client.list_httproutes(ns), gateways_by_key))
        if openshift_route:
            entries.extend(build_route_entries(ns, await k8s_client.list_routes(ns)))

    if entries:
        await push_ingress(entries, cfg.backend_url, cfg.collector_token)


async def _scan_health(namespaces: List[str], cfg: Config, caps: Optional[Dict[str, Any]]) -> None:
    """Fleet-wide health/version snapshot (ROADMAP P18 use case #5). Sums
    pod readiness across every namespace and pairs it with the K8s server
    version discover_api_capabilities already fetched at startup. A missing
    `caps` (capability check itself failed) still pushes the pod counts —
    only the version string degrades to empty, same "don't block on missing
    capability info" convention _scan_ingress established.
    """
    k8s_version = caps.get("gitVersion", "") if caps else ""
    pods_total = 0
    pods_ready = 0
    for ns in namespaces:
        counts = await k8s_client.list_pod_health(ns)
        pods_total += counts["total"]
        pods_ready += counts["ready"]
    await push_health(k8s_version, pods_total, pods_ready, cfg.backend_url, cfg.collector_token)


async def _scan_metrics(namespaces: List[str], cfg: Config) -> None:
    """Periodic container-restart-count sampling (ADR 0027, merged from the
    retired k8fy adapter's separate SCRAPE_INTERVAL timer — folded into this
    scan cycle instead, one ticker driving everything per ADR 0022 Decision
    #6). Each sample is its own append-only k8fy.metrics row (spec 006), so
    the cadence coarsening from the old 30s default to SCAN_INTERVAL_SECONDS
    (60s default) is the main behavior change — configurable if a faster
    cadence turns out to matter."""
    for ns in namespaces:
        for sample in await k8s_client.list_container_restarts(ns):
            event = normalize.normalize_metric_event(
                sample["pod_id"], sample["namespace"], sample["container"], sample["restarts"],
            )
            await normalize.push_event(event, cfg.backend_url, cfg.collector_token)


async def _scan_certificates(namespaces: List[str], cfg: Config) -> None:
    """Periodic TLS certificate expiry scraping (ADR 0027, merged from the
    retired k8fy adapter's separate CERT_CHECK_INTERVAL timer — folded into
    this scan cycle the same way _scan_metrics is). Reuses list_tls_secrets
    (already built for live_get_certificates, ADR 0024) and
    k8s_client.parse_cert_expiry for the same x509 parsing, pushed as
    k8fy.certificates ingested-store events (distinct from
    live_get_certificates' on-demand, never-persisted answers)."""
    for ns in namespaces:
        for secret in await k8s_client.list_tls_secrets(ns):
            expires_at, dns_names = k8s_client.parse_cert_expiry(secret["tls_crt_b64"])
            event = normalize.normalize_certificate_event(secret["name"], ns, expires_at, dns_names)
            await normalize.push_event(event, cfg.backend_url, cfg.collector_token)


async def _scan_once(cfg: Config, caps: Optional[Dict[str, Any]]) -> None:
    namespaces = await k8s_client.list_namespaces(exclude=set(cfg.namespace_exclude))
    try:
        await _scan_inventory(namespaces, cfg)
    except Exception:
        logger.exception("inventory scan failed")
    try:
        await _scan_ingress(namespaces, cfg, caps)
    except Exception:
        logger.exception("ingress scan failed")
    try:
        await _scan_health(namespaces, cfg, caps)
    except Exception:
        logger.exception("health scan failed")
    try:
        await _scan_metrics(namespaces, cfg)
    except Exception:
        logger.exception("metrics scan failed")
    try:
        await _scan_certificates(namespaces, cfg)
    except Exception:
        logger.exception("certificate scan failed")
    # The namespaces this cluster actually has, passed down so a
    # "<service>.<namespace>" mention can be validated on its namespace
    # segment. This is the one guard in the external tier that is not a
    # heuristic, so it matters that the list is the real one.
    known_namespaces = set(namespaces)
    for ns in namespaces:
        try:
            await _scan_namespace(ns, cfg, known_namespaces)
        except Exception:
            logger.exception("scan failed for namespace=%s", ns)


async def _run(cfg: Config, shutdown: asyncio.Event) -> None:
    caps = await k8s_client.discover_api_capabilities()
    if caps:
        logger.info(
            "connected to Kubernetes %s (gateway_api=%s, openshift_route=%s)",
            caps.get("gitVersion", "unknown"), caps.get("gateway_api"), caps.get("openshift_route"),
        )

    while not shutdown.is_set():
        logger.info("scan cycle starting")
        await _scan_once(cfg, caps)
        # Attribution coverage, every cycle. A `service` field that is silently
        # absent on most pod events would be believed by everything downstream;
        # this is the counter that makes that visible instead (ROADMAP P27).
        cov = INDEX.coverage()
        logger.info(
            "scan cycle complete (pod->service attribution: %s of %s resolved%s, %s namespaces indexed, "
            "%s attempts against an unindexed namespace)",
            cov["resolved"], cov["attempts"],
            "" if cov["rate"] is None else f" = {cov['rate'] * 100:.0f}%",
            cov["namespaces_indexed"], cov["unindexed_namespace"],
        )
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=cfg.scan_interval_seconds)
        except asyncio.TimeoutError:
            pass  # normal: next cycle starts


async def _run_all(cfg: Config, shutdown: asyncio.Event) -> None:
    """Runs the periodic scan-and-push loop, the on-demand live-relay
    connection (ADR 0022 Decision #7 / ROADMAP P18 use case #9), and the
    continuous watch streams (ADR 0027) as three independent background
    tasks — a drop in one never affects the others. All three already stop
    cleanly once `shutdown` is set.
    """
    await asyncio.gather(_run(cfg, shutdown), live_relay.run_forever(cfg, shutdown), watch.run_forever(cfg, shutdown))


def main() -> None:
    _configure_logging()
    cfg = load_from_env()
    if not cfg.collector_token:
        logger.warning(
            "COLLECTOR_TOKEN is not set — event ingestion (POST /api/ingest) still "
            "works unscoped (ADR 0024's DefaultTenantID default), but every "
            "fleet-only push (inventory/ingress/health/dependencies) and the live "
            "drill-down connection will be rejected with 401"
        )
    logger.info("agentify-discovery starting", extra={"backend_url": cfg.backend_url})

    threading.Thread(target=serve_health, args=(cfg.health_port,), name="health", daemon=True).start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    shutdown = asyncio.Event()

    def _handle_sigterm(*_args: Any) -> None:
        # Finish the in-flight scan cycle (CLAUDE.md graceful-shutdown
        # convention); don't abort mid-namespace. The 60s default
        # SCAN_INTERVAL_SECONDS cycle is well under the pod's
        # terminationGracePeriodSeconds, so this always completes in time.
        logger.info("SIGTERM received, finishing current scan cycle before exit")
        loop.call_soon_threadsafe(shutdown.set)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        loop.run_until_complete(_run_all(cfg, shutdown))
    except KeyboardInterrupt:
        logger.info("agentify-discovery shutting down")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
