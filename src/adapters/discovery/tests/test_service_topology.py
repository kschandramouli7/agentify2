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


# ── extract_external_mentions: beyond the namespace boundary ─────────────────
#
# The in-cluster miner is safe because it validates candidates against a real
# Service list. There is no such list for the public internet, so every guard
# in this function is a heuristic — and these tests ARE the specification of
# where it stops. Loosening any of them lets version numbers, stack traces and
# file names become "services we call".

# Every namespace's REAL Service names. Both segments of a cross-namespace
# mention are validated against this, which is what stops a trace UUID passing
# as a service name.
_SERVICES_BY_NS = {
    "payments": {"payment", "payment-api", "payment-batch", "payment-worker"},
    "vault": {"vault"},
    "agentify": {"agentify-backend", "agentify-agent", "agentify-frontend"},
    "kube-system": {"kube-dns"},
}


def _ext(line, namespace="agentify"):
    return st.extract_external_mentions(line, namespace, _SERVICES_BY_NS)


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


def test_a_uuid_cannot_pass_as_a_service_name():
    """Regression for the three phantom boxes on 2026-09-05.

    _HOSTNAME_RE's character class accepts hex and hyphens, so a trace UUID
    followed by a real namespace matched as a service call and drew
    "c53b9dca-f4c0-44f9-9… / another namespace" on the architecture diagram
    three times over.

    Validating only the namespace segment was not enough. Both segments are now
    checked against the real Service list, which removes the class rather than
    pattern-matching UUIDs — a random token, a hash, a build id and anything
    else shaped like a DNS label all fail the same way now.
    """
    assert _ext("trace c53b9dca-f4c0-44f9-9abc-def012345678.vault completed") == set()
    assert _ext("embedded incident 7f3ba3ef-c5a0-18d9-54f0-58bbea53372a.payments") == set()
    assert _ext("build 4a9f2c1e-0000-1111-2222-333344445555.agentify done") == set()


def test_a_real_service_in_another_namespace_still_matches():
    """The fix must not cost the capability it was built for: vault.vault is a
    real Service in a real namespace and must still produce an edge."""
    got = _ext("GET http://vault.vault.svc.cluster.local:8200/v1/pki/issue -> 200")
    assert ("cross_namespace", "vault.vault") in got
    got2 = _ext("calling https://payment-api.payments.svc.cluster.local/")
    assert ("cross_namespace", "payment-api.payments") in got2


def test_an_unknown_service_in_a_known_namespace_is_rejected():
    """The namespace being real is not sufficient — that was the bug."""
    assert _ext("thing not-a-service.payments happened") == set()


def test_no_service_map_disables_cross_namespace_mining():
    """An inventory failure must produce NO edges rather than unvalidated
    ones — the same fail-closed choice the collector makes for the cycle."""
    assert st.extract_external_mentions(
        "GET http://vault.vault.svc.cluster.local:8200/x", "agentify", {}
    ) == set()
