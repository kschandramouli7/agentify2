"""Tests for service_topology.py.

`extract_service_mentions` is copied verbatim from
src/agent/k8fy/service_topology.py — these cases mirror
src/agent/tests/test_service_topology.py's pure-function coverage exactly,
since the logic (and its precision-over-recall guarantees) must not drift
between the two copies. `push_dependency` is new to this package (it adds
the Bearer credential the original upsert_service_dependency never sent),
so it gets its own coverage of that specific behavior.
"""

import httpx
import pytest

from discovery import service_topology as st

_RealAsyncClient = httpx.AsyncClient


def _client_factory(transport: httpx.MockTransport):
    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = transport
        return _RealAsyncClient(**kwargs)
    return factory


# ── extract_service_mentions (pure function, mirrors src/agent's coverage) ──

def test_extract_accepts_fully_qualified_mention():
    log_text = "2026-08-02 calling http://payment-backend.payments.svc.cluster.local:8080/charge"
    found = st.extract_service_mentions(log_text, "payments", {"payment-backend"})
    assert found == {"payment-backend"}


def test_extract_accepts_partially_qualified_mention():
    log_text = "connecting to payment-backend.payments now"
    found = st.extract_service_mentions(log_text, "payments", {"payment-backend"})
    assert found == {"payment-backend"}


def test_extract_rejects_bare_unqualified_mention():
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


# ── push_dependency ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_push_dependency_sends_bearer_token(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    await st.push_dependency("payments", "payment-ui", "payment-backend", "http://backend", "secret-token")

    assert seen["auth"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_push_dependency_degrades_silently_on_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    # Should not raise — best-effort, same convention as the original.
    await st.push_dependency("payments", "payment-ui", "payment-backend", "http://backend", "secret-token")
