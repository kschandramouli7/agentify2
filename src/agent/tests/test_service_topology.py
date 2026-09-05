"""Tests for service_topology.py — mining a service-dependency graph out of
log text (service-topology brainstorm, Option 2).

`extract_service_mentions` is a pure function — no network — and is the
highest-value test surface here since it's a precision-over-recall
extraction heuristic: false positives would feed wrong correlations into
diagnosis. The network-facing functions use the same httpx.MockTransport
pattern as test_live_diagnostics.py.
"""

import httpx
import pytest

from k8fy import service_topology as st

_RealAsyncClient = httpx.AsyncClient


def _client_factory(transport: httpx.MockTransport):
    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = transport
        return _RealAsyncClient(**kwargs)
    return factory


# ── extract_service_mentions (pure function) ─────────────────────────────────

def test_extract_accepts_fully_qualified_mention():
    log_text = "2026-08-02 calling http://payment-backend.payments.svc.cluster.local:8080/charge"
    found = st.extract_service_mentions(log_text, "payments", {"payment-backend"})
    assert found == {"payment-backend"}


def test_extract_accepts_partially_qualified_mention():
    log_text = "connecting to payment-backend.payments now"
    found = st.extract_service_mentions(log_text, "payments", {"payment-backend"})
    assert found == {"payment-backend"}


def test_extract_rejects_bare_unqualified_mention():
    # No second DNS label at all — the regex itself never matches this,
    # cross-validation never even gets a candidate to check.
    log_text = "payment-backend restarted due to OOMKilled"
    found = st.extract_service_mentions(log_text, "payments", {"payment-backend"})
    assert found == set()


def test_extract_rejects_wrong_namespace():
    log_text = "calling payment-backend.orders.svc.cluster.local"
    found = st.extract_service_mentions(log_text, "payments", {"payment-backend"})
    assert found == set()


def test_extract_rejects_unknown_service():
    log_text = "calling unknown-svc.payments.svc.cluster.local"
    found = st.extract_service_mentions(log_text, "payments", {"payment-backend"})
    assert found == set()


def test_extract_finds_multiple_distinct_services():
    log_text = (
        "step 1: payment-ui.payments.svc.cluster.local ok\n"
        "step 2: payment-backend.payments ok\n"
    )
    found = st.extract_service_mentions(log_text, "payments", {"payment-ui", "payment-backend"})
    assert found == {"payment-ui", "payment-backend"}


def test_extract_accepts_bare_name_after_a_scheme():
    """The case that made this miner blind in real namespaces.

    In-cluster callers use the short name — Kubernetes resolves it via the pod's
    search domain — so requiring "<service>.<namespace>" meant agentify's own
    services produced zero edges while addressing each other constantly.
    """
    log_text = 'POST http://agentify-backend:8080/api/query'
    found = st.extract_service_mentions(log_text, "agentify", {"agentify-backend"})
    assert found == {"agentify-backend"}


def test_extract_accepts_bare_name_with_a_port_and_no_scheme():
    log_text = "dialing agentify-agent:8001 for reasoning"
    found = st.extract_service_mentions(log_text, "agentify", {"agentify-agent"})
    assert found == {"agentify-agent"}


def test_extract_still_rejects_a_bare_name_in_prose():
    """The false-positive guard. A service named after a common word must not be
    matched by ordinary log text — only by a hostname context."""
    for text in [
        "payment-backend restarted due to OOMKilled",
        "payment: 200",              # structured log pair, space before the value
        "processing payment now",
        "retrying payment-backend after 3s",
    ]:
        assert st.extract_service_mentions(text, "payments", {"payment-backend", "payment"}) == set(), text


def test_extract_bare_name_still_validated_against_known_services():
    log_text = "GET http://not-a-service:8080/health"
    assert st.extract_service_mentions(log_text, "agentify", {"agentify-backend"}) == set()


def test_extract_qualified_url_with_port_does_not_double_match_the_last_label():
    """A FQDN ending ".local:8080" must not yield "local" as a service."""
    log_text = "http://payment-backend.payments.svc.cluster.local:8080/charge"
    found = st.extract_service_mentions(log_text, "payments", {"payment-backend", "local"})
    assert found == {"payment-backend"}


def test_extract_bare_name_is_namespace_local():
    """A short name resolves in the pod's OWN namespace, so it is validated
    against that namespace's Service list — a service of the same name elsewhere
    must not produce an edge."""
    log_text = "http://payment-backend:8080/charge"
    # Scanning "orders", whose Service list does not contain payment-backend.
    assert st.extract_service_mentions(log_text, "orders", {"orders-api"}) == set()


def test_extract_empty_inputs_return_empty_set():
    assert st.extract_service_mentions("", "payments", {"payment-backend"}) == set()
    assert st.extract_service_mentions("payment-backend.payments", "payments", set()) == set()


# ── get_known_services ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_known_services_filters_to_namespace(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            "payments/payment-backend",
            "payments/payment-ui",
            "orders/order-service",
        ])
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    services = await st.get_known_services("payments", "http://backend")
    assert services == {"payment-backend", "payment-ui"}


@pytest.mark.asyncio
async def test_get_known_services_degrades_to_empty_on_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    services = await st.get_known_services("payments", "http://backend")
    assert services == set()


# ── fetch_service_dependencies ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_service_dependencies_returns_list(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("namespace") == "payments"
        return httpx.Response(200, json=[{"from_service": "payment-ui", "to_service": "payment-backend"}])
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    deps = await st.fetch_service_dependencies("payments", "http://backend")
    assert deps == [{"from_service": "payment-ui", "to_service": "payment-backend"}]


@pytest.mark.asyncio
async def test_fetch_service_dependencies_passes_cross_cluster_edges_through_untouched(monkeypatch):
    """ROADMAP P18 use case #4: fetch_service_dependencies is a thin pass-
    through (Client.ListServiceDependencies already returns every cluster's
    edges for a tenant/namespace together — see the Go-side test in
    postgres_test.go) — so once the Hub starts including entries from more
    than one cluster, this function must forward cluster_id verbatim rather
    than dropping it, with zero code change here."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"from_service": "checkout-ui", "to_service": "checkout-api", "cluster_id": "cluster-a"},
            {"from_service": "checkout-ui", "to_service": "checkout-api", "cluster_id": "cluster-b"},
        ])
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    deps = await st.fetch_service_dependencies("payments", "http://backend")
    assert {d["cluster_id"] for d in deps} == {"cluster-a", "cluster-b"}


@pytest.mark.asyncio
async def test_fetch_service_dependencies_degrades_to_empty_on_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    deps = await st.fetch_service_dependencies("payments", "http://backend")
    assert deps == []


# ── resolve_service_clusters (ROADMAP P16 / ADR 0023) ────────────────────────

@pytest.mark.asyncio
async def test_resolve_service_clusters_returns_ids(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("namespace") == "payments"
        assert request.url.params.get("service") == "payment-api"
        return httpx.Response(200, json={"cluster_ids": ["cluster-42", "cluster-99"]})
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    clusters = await st.resolve_service_clusters("payments", "payment-api", "http://backend")
    assert clusters == ["cluster-42", "cluster-99"]


@pytest.mark.asyncio
async def test_resolve_service_clusters_empty_on_no_match(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"cluster_ids": []})
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    clusters = await st.resolve_service_clusters("payments", "unknown-svc", "http://backend")
    assert clusters == []


@pytest.mark.asyncio
async def test_resolve_service_clusters_degrades_to_empty_on_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    clusters = await st.resolve_service_clusters("payments", "payment-api", "http://backend")
    assert clusters == []


# ── mine_service_dependencies (orchestration) ────────────────────────────────

@pytest.mark.asyncio
async def test_mine_service_dependencies_upserts_validated_edges(monkeypatch):
    known_services_calls = []
    upserted = []

    async def fake_get_known_services(namespace, backend_url):
        known_services_calls.append(namespace)
        return {"payment-backend", "payment-ui"}

    async def fake_upsert(namespace, from_service, to_service, backend_url):
        upserted.append((namespace, from_service, to_service))

    monkeypatch.setattr(st, "get_known_services", fake_get_known_services)
    monkeypatch.setattr(st, "upsert_service_dependency", fake_upsert)

    log_text = "calling payment-backend.payments.svc.cluster.local now"
    await st.mine_service_dependencies("payments", "payment-ui", log_text, "http://backend")

    assert known_services_calls == ["payments"]
    assert upserted == [("payments", "payment-ui", "payment-backend")]


@pytest.mark.asyncio
async def test_mine_service_dependencies_skips_self_loop(monkeypatch):
    async def fake_get_known_services(namespace, backend_url):
        return {"payment-ui"}

    async def fake_upsert(*args, **kwargs):
        raise AssertionError("should not upsert a self-loop")

    monkeypatch.setattr(st, "get_known_services", fake_get_known_services)
    monkeypatch.setattr(st, "upsert_service_dependency", fake_upsert)

    log_text = "payment-ui.payments.svc.cluster.local health check ok"
    await st.mine_service_dependencies("payments", "payment-ui", log_text, "http://backend")


@pytest.mark.asyncio
async def test_mine_service_dependencies_no_known_services_is_noop(monkeypatch):
    async def fake_get_known_services(namespace, backend_url):
        return set()

    async def fake_upsert(*args, **kwargs):
        raise AssertionError("should not upsert when there are no known services")

    monkeypatch.setattr(st, "get_known_services", fake_get_known_services)
    monkeypatch.setattr(st, "upsert_service_dependency", fake_upsert)

    await st.mine_service_dependencies("payments", "payment-ui", "some log text", "http://backend")


# ── extract_external_mentions: beyond the namespace boundary ─────────────────
#
# The in-cluster miner is safe because it validates candidates against a real
# Service list. There is no such list for the public internet, so every guard
# in this function is a heuristic — and these tests ARE the specification of
# where it stops. Loosening any of them lets version numbers, stack traces and
# file names become "services we call".

_NAMESPACES = {"payments", "vault", "agentify", "kube-system"}


def _ext(line, namespace="agentify"):
    return st.extract_external_mentions(line, namespace, _NAMESPACES)


def test_captures_a_cross_namespace_call():
    """The case that motivated this: the agent calls Vault, in another
    namespace, and the same-namespace miner drops it by design."""
    got = _ext("GET http://vault.vault.svc.cluster.local:8200/v1/pki/issue -> 200")
    assert ("cross_namespace", "vault.vault") in got


def test_cross_namespace_requires_a_TRACKED_namespace():
    """The one guard here that is not a heuristic. "foo.bar" is only an edge if
    "bar" is a namespace the Hub actually tracks; otherwise it is a dotted
    string that happens to look like one."""
    assert _ext("calling http://thing.not-a-real-namespace/x") == set()
    assert ("cross_namespace", "payment-api.payments") in _ext(
        "calling https://payment-api.payments.svc.cluster.local/"
    )


def test_the_same_namespace_is_left_to_the_other_miner():
    """No double-counting: an in-namespace target must not appear here as well,
    or the same call becomes two edges in two trust tiers."""
    got = _ext("http://agentify-backend.agentify.svc.cluster.local:8080/x", namespace="agentify")
    assert not any(k == "cross_namespace" for k, _ in got)


@pytest.mark.parametrize("line,host", [
    ('POST https://api.anthropic.com/v1/messages "200 OK"', "api.anthropic.com"),
    ("resolve prompt from https://us.cloud.langfuse.com/api/public/v2/prompts", "us.cloud.langfuse.com"),
    ("psql postgresql://agentify.abc123.ap-southeast-2.rds.amazonaws.com:5432/db",
     "agentify.abc123.ap-southeast-2.rds.amazonaws.com"),
])
def test_captures_real_external_egress(line, host):
    assert ("external", host) in _ext(line)


@pytest.mark.parametrize("line", [
    "started agentify v1.2.3 (build 4567)",                          # version number
    "at com.example.payments.Handler.process(Handler.java:42)",      # stack trace
    "loaded config.yaml and values.yml",                             # file names
    "reading go.sum and package.json",                               # more file names
    "took 1.234s for 99.9th percentile",                             # measurements
])
def test_rejects_dotted_strings_that_are_not_hosts(line):
    """These are what a log is actually full of. Every one of them would become
    a fabricated external dependency without the hostname-context, TLD-shape
    and suffix guards."""
    assert _ext(line) == set()


@pytest.mark.parametrize("line", [
    "GET http://localhost:8080/health",
    "dialing http://10.0.1.5:5432",
    "connect http://127.0.0.1:6379",
    "probe http://169.254.169.254/latest/meta-data",   # link-local / IMDS
    "http://192.168.1.10:3000",
])
def test_rejects_loopback_private_and_ip_targets(line):
    """An IP is not a name we can put on a diagram, and loopback/private
    targets are not egress. IMDS in particular would be alarming noise."""
    assert _ext(line) == set()


def test_rejects_in_cluster_suffixes_from_the_external_tier():
    """*.svc.cluster.local and *.local are in-cluster by definition and are
    handled by the cross-namespace pass; they must not ALSO be reported as
    external egress."""
    got = _ext("http://payment.payments.svc.cluster.local/")
    assert not any(k == "external" for k, _ in got)
    assert _ext("http://printer.local/") == set()


def test_a_single_label_host_is_not_external():
    """A bare name is in-cluster by the pod's search domain; treating it as
    external would duplicate every same-namespace edge."""
    assert _ext("GET http://payment-api:8080/") == set()


def test_empty_and_malformed_input_is_safe():
    assert _ext("") == set()
    assert st.extract_external_mentions("x", "ns", set()) == set()
    assert _ext("http://..//") == set()
    assert _ext("http://" + "a" * 300 + ".com/") == set()   # over the DNS length limit
