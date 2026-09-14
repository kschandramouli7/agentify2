"""Tests for k8s_client.py: the ingress/entry-point mapping additions
(ROADMAP P18 use case #3 — list_ingresses, list_gateways, list_httproutes,
list_routes, discover_api_capabilities's group-probing) and the watch/scrape
primitives merged in from the retired k8fy adapter (ADR 0027 —
watch_resource, list_container_restarts, parse_cert_expiry).

Same httpx.MockTransport pattern as test_inventory.py/test_service_topology.py,
plus a k8s_headers() monkeypatch so _k8s_get doesn't need a real mounted
service-account token file.
"""

import httpx
import pytest
from cryptography import x509

from discovery import k8s_client

_RealAsyncClient = httpx.AsyncClient


def _client_factory(transport: httpx.MockTransport):
    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = transport
        return _RealAsyncClient(**kwargs)
    return factory


@pytest.fixture(autouse=True)
def _fake_sa_token(monkeypatch):
    monkeypatch.setattr(k8s_client, "k8s_headers", lambda content_type="application/json": {"Authorization": "Bearer fake"})


def _mock(monkeypatch, handler):
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))


# ── list_services ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_services_reads_expected_failure_annotation(monkeypatch):
    """ADR 0032: agentify.io/expected-failure is read from the same response
    list_services already fetches — no extra K8s call."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{
            "metadata": {
                "name": "legacy-stub",
                "annotations": {"agentify.io/expected-failure": "legacy stub, decommission ticket JIRA-123"},
            },
            "spec": {"selector": {"app": "legacy-stub"}, "type": "ClusterIP"},
        }]})
    _mock(monkeypatch, handler)

    result = await k8s_client.list_services("payments")
    assert result[0]["expected_failure_reason"] == "legacy stub, decommission ticket JIRA-123"


@pytest.mark.asyncio
async def test_list_services_defaults_expected_failure_reason_to_empty(monkeypatch):
    """No annotation, or no annotations block at all, must both yield ""
    rather than a missing key or a KeyError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [
            {"metadata": {"name": "no-annotations-block"}, "spec": {"selector": {"app": "a"}}},
            {"metadata": {"name": "other-annotations", "annotations": {"some.other/key": "x"}},
             "spec": {"selector": {"app": "b"}}},
        ]})
    _mock(monkeypatch, handler)

    result = await k8s_client.list_services("payments")
    by_name = {s["name"]: s for s in result}
    assert by_name["no-annotations-block"]["expected_failure_reason"] == ""
    assert by_name["other-annotations"]["expected_failure_reason"] == ""


# ── list_pod_security_contexts / list_network_policy_count (ROADMAP P30) ────

@pytest.mark.asyncio
async def test_list_pod_security_contexts_extracts_container_settings(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/api/v1/namespaces/payments/pods" in str(request.url)
        return httpx.Response(200, json={"items": [{
            "metadata": {"name": "payment-api-abc"},
            "spec": {
                "securityContext": {"runAsNonRoot": True},
                "containers": [{
                    "name": "worker",
                    "securityContext": {
                        "readOnlyRootFilesystem": True,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                }],
            },
        }]})
    _mock(monkeypatch, handler)

    result = await k8s_client.list_pod_security_contexts("payments")
    assert result == [{
        "name": "payment-api-abc",
        "containers": [{
            "name": "worker",
            "run_as_non_root": True,
            "read_only_root_filesystem": True,
            "allow_privilege_escalation": False,
            "drops_capabilities": True,
        }],
    }]


@pytest.mark.asyncio
async def test_list_pod_security_contexts_run_as_non_root_falls_back_to_pod_level(monkeypatch):
    """runAsNonRoot is the one field with a real pod-level equivalent — a
    container that sets nothing should inherit the pod's value, not read as
    unset."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{
            "metadata": {"name": "p"},
            "spec": {
                "securityContext": {"runAsNonRoot": True},
                "containers": [{"name": "c", "securityContext": {}}],
            },
        }]})
    _mock(monkeypatch, handler)

    result = await k8s_client.list_pod_security_contexts("payments")
    assert result[0]["containers"][0]["run_as_non_root"] is True


@pytest.mark.asyncio
async def test_list_pod_security_contexts_missing_fields_are_none_not_false(monkeypatch):
    """Unset must stay distinguishable from explicitly-insecure — the check
    layer treats both as findings, but conflating them here would make an
    unset value indistinguishable from a value nobody could have set."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{
            "metadata": {"name": "p"},
            "spec": {"containers": [{"name": "c"}]},
        }]})
    _mock(monkeypatch, handler)

    result = await k8s_client.list_pod_security_contexts("payments")
    c = result[0]["containers"][0]
    assert c["run_as_non_root"] is None
    assert c["read_only_root_filesystem"] is None
    assert c["allow_privilege_escalation"] is None
    assert c["drops_capabilities"] is False  # bool(), never None


@pytest.mark.asyncio
async def test_list_pod_security_contexts_returns_empty_on_404(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    _mock(monkeypatch, handler)
    assert await k8s_client.list_pod_security_contexts("payments") == []


@pytest.mark.asyncio
async def test_list_network_policy_count_counts_items(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/apis/networking.k8s.io/v1/namespaces/payments/networkpolicies" in str(request.url)
        return httpx.Response(200, json={"items": [{"metadata": {"name": "default-deny"}}, {"metadata": {"name": "allow-ingress"}}]})
    _mock(monkeypatch, handler)
    assert await k8s_client.list_network_policy_count("payments") == 2


@pytest.mark.asyncio
async def test_list_network_policy_count_zero_when_none_exist(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})
    _mock(monkeypatch, handler)
    assert await k8s_client.list_network_policy_count("payments") == 0


@pytest.mark.asyncio
async def test_list_network_policy_count_zero_on_404(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    _mock(monkeypatch, handler)
    assert await k8s_client.list_network_policy_count("payments") == 0


# ── list_ingresses ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_ingresses_flattens_hosts_and_backends(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/apis/networking.k8s.io/v1/namespaces/payments/ingresses" in str(request.url)
        return httpx.Response(200, json={"items": [{
            "metadata": {"name": "shop-ingress"},
            "spec": {
                "rules": [
                    {"host": "shop.example.com", "http": {"paths": [{"backend": {"service": {"name": "storefront"}}}]}},
                    {"host": "api.example.com", "http": {"paths": [{"backend": {"service": {"name": "api-gw"}}}]}},
                ],
            },
        }]})
    _mock(monkeypatch, handler)

    result = await k8s_client.list_ingresses("payments")
    assert result == [{
        "name": "shop-ingress",
        "hosts": ["shop.example.com", "api.example.com"],
        "backend_services": ["storefront", "api-gw"],
        "has_tls": False,
    }]


@pytest.mark.asyncio
async def test_list_ingresses_falls_back_to_default_backend(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{
            "metadata": {"name": "catch-all"},
            "spec": {"defaultBackend": {"service": {"name": "fallback-svc"}}},
        }]})
    _mock(monkeypatch, handler)

    result = await k8s_client.list_ingresses("payments")
    assert result == [{"name": "catch-all", "hosts": [], "backend_services": ["fallback-svc"], "has_tls": False}]


@pytest.mark.asyncio
async def test_list_ingresses_returns_empty_on_404(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    _mock(monkeypatch, handler)

    assert await k8s_client.list_ingresses("payments") == []


@pytest.mark.asyncio
async def test_list_ingresses_detects_tls_block(monkeypatch):
    """ROADMAP P30 phase 1: has_tls must be True whenever spec.tls is
    present, regardless of its contents — Phase 1's check only needs
    presence/absence, not which hosts/secrets are covered."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{
            "metadata": {"name": "secure-ingress"},
            "spec": {
                "tls": [{"hosts": ["shop.example.com"], "secretName": "shop-tls"}],
                "rules": [{"host": "shop.example.com", "http": {"paths": []}}],
            },
        }]})
    _mock(monkeypatch, handler)

    result = await k8s_client.list_ingresses("payments")
    assert result[0]["has_tls"] is True


# ── list_pod_health ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_pod_health_counts_ready_via_ready_condition(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/api/v1/namespaces/payments/pods" in str(request.url)
        return httpx.Response(200, json={"items": [
            {"metadata": {"name": "pod-a"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "pod-b"}, "status": {"conditions": [{"type": "Ready", "status": "False"}]}},
            {"metadata": {"name": "pod-c"}, "status": {"conditions": [{"type": "PodScheduled", "status": "True"}]}},
        ]})
    _mock(monkeypatch, handler)

    assert await k8s_client.list_pod_health("payments") == {"total": 3, "ready": 1}


@pytest.mark.asyncio
async def test_list_pod_health_missing_conditions_counts_as_not_ready(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"metadata": {"name": "pending-pod"}, "status": {}}]})
    _mock(monkeypatch, handler)

    assert await k8s_client.list_pod_health("payments") == {"total": 1, "ready": 0}


@pytest.mark.asyncio
async def test_list_pod_health_empty_namespace(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})
    _mock(monkeypatch, handler)

    assert await k8s_client.list_pod_health("payments") == {"total": 0, "ready": 0}


@pytest.mark.asyncio
async def test_list_pod_health_returns_zeros_on_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    _mock(monkeypatch, handler)

    assert await k8s_client.list_pod_health("payments") == {"total": 0, "ready": 0}


# ── list_gateways ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_gateways_extracts_listeners(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/apis/gateway.networking.k8s.io/v1/namespaces/payments/gateways" in str(request.url)
        return httpx.Response(200, json={"items": [{
            "metadata": {"name": "main-gateway"},
            "spec": {"listeners": [{"name": "https", "hostname": "shop.example.com", "port": 443}]},
        }]})
    _mock(monkeypatch, handler)

    result = await k8s_client.list_gateways("payments")
    assert result == [{
        "name": "main-gateway",
        "listeners": [{"name": "https", "hostname": "shop.example.com", "port": 443}],
    }]


@pytest.mark.asyncio
async def test_list_gateways_returns_empty_when_crd_not_installed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    _mock(monkeypatch, handler)

    assert await k8s_client.list_gateways("payments") == []


# ── list_httproutes ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_httproutes_extracts_parent_refs_and_backends(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{
            "metadata": {"name": "shop-route"},
            "spec": {
                "hostnames": ["shop.example.com"],
                "parentRefs": [{"name": "main-gateway", "sectionName": "https"}],
                "rules": [{"backendRefs": [{"name": "storefront"}]}],
            },
        }]})
    _mock(monkeypatch, handler)

    result = await k8s_client.list_httproutes("payments")
    assert result == [{
        "name": "shop-route",
        "hostnames": ["shop.example.com"],
        "parent_refs": [{"name": "main-gateway", "namespace": "payments", "section_name": "https"}],
        "backend_services": ["storefront"],
    }]


@pytest.mark.asyncio
async def test_list_httproutes_parent_ref_namespace_explicit_override(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{
            "metadata": {"name": "cross-ns-route"},
            "spec": {"parentRefs": [{"name": "shared-gateway", "namespace": "gateway-infra"}], "rules": []},
        }]})
    _mock(monkeypatch, handler)

    result = await k8s_client.list_httproutes("payments")
    assert result[0]["parent_refs"] == [{"name": "shared-gateway", "namespace": "gateway-infra", "section_name": ""}]


# ── list_routes ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_routes_extracts_host_and_primary_backend(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/apis/route.openshift.io/v1/namespaces/payments/routes" in str(request.url)
        return httpx.Response(200, json={"items": [{
            "metadata": {"name": "shop-route"},
            "spec": {"host": "shop.apps.example.com", "to": {"name": "storefront"}},
        }]})
    _mock(monkeypatch, handler)

    result = await k8s_client.list_routes("payments")
    assert result == [{"name": "shop-route", "host": "shop.apps.example.com", "backend_service": "storefront"}]


@pytest.mark.asyncio
async def test_list_routes_returns_empty_on_non_openshift_cluster(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    _mock(monkeypatch, handler)

    assert await k8s_client.list_routes("payments") == []


# ── discover_api_capabilities group probing ─────────────────────────────────

@pytest.mark.asyncio
async def test_discover_api_capabilities_detects_both_optional_groups(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/version"):
            return httpx.Response(200, json={"gitVersion": "v1.30.0"})
        if "gateway.networking.k8s.io" in url:
            return httpx.Response(200, json={})
        if "route.openshift.io" in url:
            return httpx.Response(404)
        raise AssertionError(f"unexpected URL {url}")
    _mock(monkeypatch, handler)

    caps = await k8s_client.discover_api_capabilities()
    assert caps["gateway_api"] is True
    assert caps["openshift_route"] is False


@pytest.mark.asyncio
async def test_discover_api_capabilities_both_absent_on_vanilla_k8s(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/version"):
            return httpx.Response(200, json={"gitVersion": "v1.30.0"})
        return httpx.Response(404)
    _mock(monkeypatch, handler)

    caps = await k8s_client.discover_api_capabilities()
    assert caps["gateway_api"] is False
    assert caps["openshift_route"] is False


# ── watch_resource / list_container_restarts / parse_cert_expiry
# (ADR 0027, merged from the retired k8fy adapter) ──────────────────────────

@pytest.mark.asyncio
async def test_watch_resource_yields_parsed_events(monkeypatch):
    body = (
        b'{"type": "ADDED", "object": {"metadata": {"name": "pod-a"}}}\n'
        b'{"type": "MODIFIED", "object": {"metadata": {"name": "pod-a"}}}\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("watch") == "1"
        return httpx.Response(200, content=body)
    _mock(monkeypatch, handler)

    events = [event async for event in k8s_client.watch_resource("/api/v1/pods")]
    assert [e["type"] for e in events] == ["ADDED", "MODIFIED"]
    assert events[0]["object"]["metadata"]["name"] == "pod-a"


@pytest.mark.asyncio
async def test_watch_resource_raises_on_connection_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    _mock(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError):
        async for _ in k8s_client.watch_resource("/api/v1/pods"):
            pass


@pytest.mark.asyncio
async def test_list_container_restarts_one_entry_per_container(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{
            "metadata": {"name": "pod-a"},
            "status": {"containerStatuses": [
                {"name": "app", "restartCount": 3},
                {"name": "sidecar", "restartCount": 0},
            ]},
        }]})
    _mock(monkeypatch, handler)

    result = await k8s_client.list_container_restarts("payments")
    assert result == [
        {"pod_id": "pod-a", "namespace": "payments", "container": "app", "restarts": 3},
        {"pod_id": "pod-a", "namespace": "payments", "container": "sidecar", "restarts": 0},
    ]


@pytest.mark.asyncio
async def test_list_container_restarts_returns_empty_on_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    _mock(monkeypatch, handler)

    assert await k8s_client.list_container_restarts("payments") == []


def _self_signed_cert_pem(common_name: str, days_from_now: int) -> bytes:
    """Same synthetic-cert helper as test_live_tools.py — no live cluster or
    real TLS secrets available in this environment."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import base64 as _base64
    import datetime as _datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = _datetime.datetime.now(_datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _datetime.timedelta(days=abs(days_from_now) + 1))
        .not_valid_after(now + _datetime.timedelta(days=days_from_now))
        .sign(key, hashes.SHA256())
    )
    return _base64.b64encode(cert.public_bytes(serialization.Encoding.PEM)).decode("ascii")


def test_parse_cert_expiry_returns_expiry_and_common_name():
    cert_b64 = _self_signed_cert_pem("payment-api.payments.svc", days_from_now=45)
    expires_at, dns_names = k8s_client.parse_cert_expiry(cert_b64)
    assert expires_at is not None
    assert dns_names == ["payment-api.payments.svc"]  # CN fallback, no SAN extension


def test_parse_cert_expiry_returns_none_on_malformed_data():
    expires_at, dns_names = k8s_client.parse_cert_expiry("not-valid-base64-pem")
    assert expires_at is None
    assert dns_names == []


# ── get_pod_logs (ROADMAP OPS-9) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_pod_logs_single_container_pod_needs_no_retry(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="log line one\n")
    _mock(monkeypatch, handler)

    out = await k8s_client.get_pod_logs("payments", "api-1")
    assert out == "log line one\n"
    assert len(calls) == 1, "a single-container pod must not trigger the retry path"


@pytest.mark.asyncio
async def test_get_pod_logs_retries_with_first_container_on_ambiguous_400(monkeypatch):
    """Before this fix, every multi-container pod (a sidecar in every pod in a
    service-mesh cluster) returned "" here — the collector never read a
    single log line from anywhere. K8s' own 400 body names the valid
    container choices; this must retry once using the first one."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "container=" not in str(request.url):
            return httpx.Response(
                400,
                text='{"kind":"Status","status":"Failure","message":'
                     '"a container name must be specified for pod payment-api-x, '
                     'choose one of: [app istio-proxy]","reason":"BadRequest","code":400}',
            )
        assert "container=app" in str(request.url)
        return httpx.Response(200, text="app container logs\n")
    _mock(monkeypatch, handler)

    out = await k8s_client.get_pod_logs("payments", "payment-api-x")
    assert out == "app container logs\n"
    assert len(calls) == 2, "must retry exactly once with the resolved container"


@pytest.mark.asyncio
async def test_get_pod_logs_explicit_container_is_never_retried(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(400, text="a container name must be specified, choose one of: [app istio-proxy]")
    _mock(monkeypatch, handler)

    out = await k8s_client.get_pod_logs("payments", "payment-api-x", container="istio-proxy")
    assert out == ""
    assert len(calls) == 1, "an explicitly wrong container must not be silently swapped for another"


@pytest.mark.asyncio
async def test_get_pod_logs_400_without_choices_still_gives_up_cleanly(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"message":"some unrelated bad request"}')
    _mock(monkeypatch, handler)

    assert await k8s_client.get_pod_logs("payments", "pod-x") == ""
