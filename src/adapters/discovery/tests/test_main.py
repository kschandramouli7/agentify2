"""Tests for main.py's _service_for_pod and _namespace_services — new
logic (not copied from src/agent), so it gets its own coverage.
_service_for_pod matches a pod to the Service that selects it via the same
label-selector semantics K8s itself uses to build Service endpoints.
_namespace_services decides which namespaces ROADMAP P18 use case #1's
inventory push considers "active", and carries each service's name (ROADMAP
P16 / ADR 0023) and selector (ADR 0029) through for the service->cluster
registry and the Glue-based dependency miner respectively.
"""

import pytest

from discovery import k8s_client, main
from discovery.config import Config
from discovery.main import (
    _namespace_services, _scan_certificates, _scan_health, _scan_ingress, _scan_metrics, _service_for_pod,
)


def test_matches_pod_via_selector():
    services = [{"name": "payment-backend", "selector": {"app": "payment-backend"}}]
    assert _service_for_pod({"app": "payment-backend", "pod-template-hash": "abc123"}, services) == "payment-backend"


def test_no_match_returns_none():
    services = [{"name": "payment-backend", "selector": {"app": "payment-backend"}}]
    assert _service_for_pod({"app": "payment-ui"}, services) is None


def test_selector_must_be_fully_satisfied():
    services = [{"name": "payment-backend", "selector": {"app": "payment-backend", "tier": "backend"}}]
    # Missing the "tier" label -> not a match, even though "app" matches.
    assert _service_for_pod({"app": "payment-backend"}, services) is None


def test_empty_selector_never_matches():
    services = [{"name": "manually-managed", "selector": {}}]
    assert _service_for_pod({"app": "anything"}, services) is None


def test_picks_first_matching_service_among_several():
    services = [
        {"name": "payment-ui", "selector": {"app": "payment-ui"}},
        {"name": "payment-backend", "selector": {"app": "payment-backend"}},
    ]
    assert _service_for_pod({"app": "payment-backend"}, services) == "payment-backend"


# ── _namespace_services ───────────────────────────────────────────────────────

async def _empty(namespace):
    return []


async def _nonempty(namespace):
    return ["something"]


@pytest.mark.asyncio
async def test_namespace_active_via_services_returns_name_and_selector(monkeypatch):
    async def services(namespace):
        return [{"name": "payment-api", "selector": {"app": "payment-api"}}, {"name": "payment-worker", "selector": {}}]

    monkeypatch.setattr(k8s_client, "list_services", services)
    monkeypatch.setattr(k8s_client, "list_deployments", _empty)
    monkeypatch.setattr(k8s_client, "list_statefulsets", _empty)
    monkeypatch.setattr(k8s_client, "list_daemonsets", _empty)
    assert await _namespace_services("payments") == [
        {"name": "payment-api", "selector": {"app": "payment-api"}},
        {"name": "payment-worker", "selector": {}},
    ]


@pytest.mark.asyncio
async def test_namespace_active_via_daemonset_only_returns_empty_list(monkeypatch):
    monkeypatch.setattr(k8s_client, "list_services", _empty)
    monkeypatch.setattr(k8s_client, "list_deployments", _empty)
    monkeypatch.setattr(k8s_client, "list_statefulsets", _empty)
    monkeypatch.setattr(k8s_client, "list_daemonsets", _nonempty)
    # Active (has a workload) but no Service fronts it -> empty list, not None.
    assert await _namespace_services("kube-monitoring") == []


@pytest.mark.asyncio
async def test_namespace_inactive_with_no_workloads_returns_none(monkeypatch):
    monkeypatch.setattr(k8s_client, "list_services", _empty)
    monkeypatch.setattr(k8s_client, "list_deployments", _empty)
    monkeypatch.setattr(k8s_client, "list_statefulsets", _empty)
    monkeypatch.setattr(k8s_client, "list_daemonsets", _empty)
    assert await _namespace_services("empty-ns") is None


# ── _scan_ingress (ROADMAP P18 use case #3) ──────────────────────────────────

def _cfg() -> Config:
    return Config(backend_url="http://backend", collector_token="tok", scan_interval_seconds=60, max_pods_per_namespace=5, log_tail_lines=200)


def _fail_if_called(name):
    async def fn(*args, **kwargs):
        raise AssertionError(f"{name} should not have been called")
    return fn


@pytest.mark.asyncio
async def test_scan_ingress_skips_gateway_and_route_when_caps_absent(monkeypatch):
    monkeypatch.setattr(k8s_client, "list_ingresses", lambda ns: _return([]))
    monkeypatch.setattr(k8s_client, "list_gateways", _fail_if_called("list_gateways"))
    monkeypatch.setattr(k8s_client, "list_httproutes", _fail_if_called("list_httproutes"))
    monkeypatch.setattr(k8s_client, "list_routes", _fail_if_called("list_routes"))
    monkeypatch.setattr(main, "push_ingress", _fail_if_called("push_ingress"))

    await _scan_ingress(["payments"], _cfg(), None)


@pytest.mark.asyncio
async def test_scan_ingress_calls_gateway_api_when_capability_present(monkeypatch):
    called = {"gateways": False, "httproutes": False}

    async def gateways(ns):
        called["gateways"] = True
        return [{"name": "main-gateway", "listeners": [{"name": "https", "hostname": "shop.example.com", "port": 443}]}]

    async def httproutes(ns):
        called["httproutes"] = True
        return [{"name": "shop-route", "hostnames": [], "parent_refs": [{"name": "main-gateway", "namespace": ns, "section_name": ""}], "backend_services": ["storefront"]}]

    monkeypatch.setattr(k8s_client, "list_ingresses", lambda ns: _return([]))
    monkeypatch.setattr(k8s_client, "list_gateways", gateways)
    monkeypatch.setattr(k8s_client, "list_httproutes", httproutes)
    monkeypatch.setattr(k8s_client, "list_routes", _fail_if_called("list_routes"))

    pushed = {}

    async def fake_push(entries, backend_url, token):
        pushed["entries"] = entries

    monkeypatch.setattr(main, "push_ingress", fake_push)

    await _scan_ingress(["payments"], _cfg(), {"gateway_api": True, "openshift_route": False})

    assert called["gateways"] and called["httproutes"]
    assert pushed["entries"] == [{"namespace": "payments", "kind": "httproute", "name": "shop-route", "host": "shop.example.com", "backend_service": "storefront"}]


@pytest.mark.asyncio
async def test_scan_ingress_calls_openshift_route_when_capability_present(monkeypatch):
    monkeypatch.setattr(k8s_client, "list_ingresses", lambda ns: _return([]))
    monkeypatch.setattr(k8s_client, "list_gateways", _fail_if_called("list_gateways"))
    monkeypatch.setattr(k8s_client, "list_httproutes", _fail_if_called("list_httproutes"))

    async def routes(ns):
        return [{"name": "shop-route", "host": "shop.apps.example.com", "backend_service": "storefront"}]

    monkeypatch.setattr(k8s_client, "list_routes", routes)

    pushed = {}

    async def fake_push(entries, backend_url, token):
        pushed["entries"] = entries

    monkeypatch.setattr(main, "push_ingress", fake_push)

    await _scan_ingress(["payments"], _cfg(), {"gateway_api": False, "openshift_route": True})

    assert pushed["entries"] == [{"namespace": "payments", "kind": "route", "name": "shop-route", "host": "shop.apps.example.com", "backend_service": "storefront"}]


@pytest.mark.asyncio
async def test_scan_ingress_skips_push_when_no_entries(monkeypatch):
    monkeypatch.setattr(k8s_client, "list_ingresses", lambda ns: _return([]))
    monkeypatch.setattr(main, "push_ingress", _fail_if_called("push_ingress"))

    await _scan_ingress(["payments"], _cfg(), None)


async def _return(value):
    return value


# ── _scan_health (ROADMAP P18 use case #5) ───────────────────────────────────

@pytest.mark.asyncio
async def test_scan_health_sums_pod_counts_across_namespaces_and_uses_caps_version(monkeypatch):
    counts = {"payments": {"total": 5, "ready": 4}, "checkout": {"total": 3, "ready": 3}}

    async def pod_health(ns):
        return counts[ns]

    monkeypatch.setattr(k8s_client, "list_pod_health", pod_health)

    pushed = {}

    async def fake_push(k8s_version, pods_total, pods_ready, backend_url, token):
        pushed.update(k8s_version=k8s_version, pods_total=pods_total, pods_ready=pods_ready)

    monkeypatch.setattr(main, "push_health", fake_push)

    await _scan_health(["payments", "checkout"], _cfg(), {"gitVersion": "v1.30.0"})

    assert pushed == {"k8s_version": "v1.30.0", "pods_total": 8, "pods_ready": 7}


@pytest.mark.asyncio
async def test_scan_health_still_pushes_counts_when_caps_is_none(monkeypatch):
    monkeypatch.setattr(k8s_client, "list_pod_health", lambda ns: _return({"total": 2, "ready": 2}))

    pushed = {}

    async def fake_push(k8s_version, pods_total, pods_ready, backend_url, token):
        pushed.update(k8s_version=k8s_version, pods_total=pods_total, pods_ready=pods_ready)

    monkeypatch.setattr(main, "push_health", fake_push)

    await _scan_health(["payments"], _cfg(), None)


# ── _scan_metrics / _scan_certificates (ADR 0027, merged from the retired
# k8fy adapter's separate SCRAPE_INTERVAL/CERT_CHECK_INTERVAL timers) ────────

@pytest.mark.asyncio
async def test_scan_metrics_pushes_one_event_per_container(monkeypatch):
    async def container_restarts(ns):
        return [
            {"pod_id": "pod-a", "namespace": ns, "container": "app", "restarts": 2},
            {"pod_id": "pod-a", "namespace": ns, "container": "sidecar", "restarts": 0},
        ]

    monkeypatch.setattr(k8s_client, "list_container_restarts", container_restarts)

    pushed = []

    async def fake_push_event(event, backend_url, collector_token):
        pushed.append(event)

    monkeypatch.setattr(main.normalize, "push_event", fake_push_event)

    await _scan_metrics(["payments"], _cfg())

    assert len(pushed) == 2
    assert {e["payload"]["container"] for e in pushed} == {"app", "sidecar"}
    assert all(e["event_namespace"] == "k8fy.metrics" for e in pushed)


@pytest.mark.asyncio
async def test_scan_metrics_no_containers_pushes_nothing(monkeypatch):
    monkeypatch.setattr(k8s_client, "list_container_restarts", lambda ns: _return([]))
    monkeypatch.setattr(main.normalize, "push_event", _fail_if_called("push_event"))

    await _scan_metrics(["payments"], _cfg())


@pytest.mark.asyncio
async def test_scan_certificates_pushes_one_event_per_secret(monkeypatch):
    async def tls_secrets(ns):
        return [{"name": "tls-cert-a", "tls_crt_b64": "irrelevant-for-this-test"}]

    monkeypatch.setattr(k8s_client, "list_tls_secrets", tls_secrets)
    monkeypatch.setattr(k8s_client, "parse_cert_expiry", lambda b64: (None, []))

    pushed = []

    async def fake_push_event(event, backend_url, collector_token):
        pushed.append(event)

    monkeypatch.setattr(main.normalize, "push_event", fake_push_event)

    await _scan_certificates(["payments"], _cfg())

    assert len(pushed) == 1
    assert pushed[0]["event_namespace"] == "k8fy.certificates"
    assert pushed[0]["payload"]["secret"] == "tls-cert-a"


@pytest.mark.asyncio
async def test_scan_certificates_no_tls_secrets_pushes_nothing(monkeypatch):
    monkeypatch.setattr(k8s_client, "list_tls_secrets", lambda ns: _return([]))
    monkeypatch.setattr(main.normalize, "push_event", _fail_if_called("push_event"))

    await _scan_certificates(["payments"], _cfg())
