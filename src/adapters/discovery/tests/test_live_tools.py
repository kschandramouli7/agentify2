"""Tests for live_tools.py — read-only LIVE Kubernetes API calls served over
the collector's persistent connection (ROADMAP P18 use case #9).

Adapted from src/agent/tests/test_live_diagnostics.py — same
httpx.MockTransport pattern, since live_tools.py is copied from that
module's logic and rehosted on this package's own k8s_client.py.
"""

import base64
import datetime

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from discovery import k8s_client, live_tools

_RealAsyncClient = httpx.AsyncClient


def _client_factory(transport: httpx.MockTransport):
    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = transport
        return _RealAsyncClient(**kwargs)
    return factory


@pytest.fixture
def sa_token(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("test-token")
    monkeypatch.setattr(k8s_client, "_SA_TOKEN_PATH", str(token_file))


@pytest.mark.asyncio
async def test_live_list_pods_summarizes_status(sa_token, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [
            {
                "metadata": {"name": "payment-worker-abc"},
                "spec": {"nodeName": "fargate-ip-1"},
                "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 3}]},
            },
        ]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await live_tools.live_list_pods("payments")

    assert result["namespace"] == "payments"
    assert result["pods"] == [
        {"name": "payment-worker-abc", "phase": "Running", "ready": True, "restart_count": 3, "node": "fargate-ip-1"},
    ]


@pytest.mark.asyncio
async def test_live_get_pod_logs_redacts(sa_token, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="connecting with password=hunter2")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await live_tools.live_get_pod_logs("payments", "payment-worker-abc")

    assert "hunter2" not in result["logs"]


@pytest.mark.asyncio
async def test_live_get_events_sorts_and_caps(sa_token, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [
            {"type": "Normal", "reason": "Started", "lastTimestamp": "2026-08-01T00:00:00Z", "involvedObject": {"name": "pod-a"}},
            {"type": "Warning", "reason": "OOMKilled", "lastTimestamp": "2026-08-03T00:00:00Z", "involvedObject": {"name": "pod-a"}},
        ]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await live_tools.live_get_events("payments")

    assert result["events"][0]["reason"] == "OOMKilled"  # most recent first


@pytest.mark.asyncio
async def test_live_describe_pod_error_propagates(sa_token, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await live_tools.live_describe_pod("payments", "missing-pod")

    assert "error" in result


# ── live_get_certificates (ROADMAP P16/P18, ADR 0024) ────────────────────────

def _self_signed_cert_pem(common_name: str, days_from_now: int) -> bytes:
    """Build a real, synthetic self-signed X.509 cert for testing — no live
    cluster or real TLS secrets are available in this session, so this is
    the closest hermetic equivalent (per the plan's stated verification
    approach)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=abs(days_from_now) + 1))
        .not_valid_after(now + datetime.timedelta(days=days_from_now))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _b64(pem_bytes: bytes) -> str:
    return base64.b64encode(pem_bytes).decode("ascii")


@pytest.mark.asyncio
async def test_live_get_certificates_returns_parsed_summary_only(sa_token, monkeypatch):
    cert_pem = _self_signed_cert_pem("payment-api.payments.svc", days_from_now=45)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "fieldSelector=type%3Dkubernetes.io%2Ftls" in str(request.url) or "type=kubernetes.io/tls" in str(request.url)
        return httpx.Response(200, json={"items": [
            {"metadata": {"name": "payment-api-tls"}, "data": {"tls.crt": _b64(cert_pem), "tls.key": "should-never-be-read"}},
        ]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await live_tools.live_get_certificates("payments")

    assert result["namespace"] == "payments"
    assert len(result["certificates"]) == 1
    cert = result["certificates"][0]
    assert cert["name"] == "payment-api-tls"
    assert cert["common_name"] == "payment-api.payments.svc"
    assert 44 <= cert["days_until_expiry"] <= 45
    # The raw cert PEM/base64 and the private key must never appear anywhere
    # in the returned summary.
    assert _b64(cert_pem) not in str(result)
    assert "should-never-be-read" not in str(result)
    assert set(cert.keys()) == {"name", "common_name", "expiry_date", "days_until_expiry"}


@pytest.mark.asyncio
async def test_live_get_certificates_flags_expired_cert(sa_token, monkeypatch):
    cert_pem = _self_signed_cert_pem("expired.payments.svc", days_from_now=-10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [
            {"metadata": {"name": "expired-tls"}, "data": {"tls.crt": _b64(cert_pem)}},
        ]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await live_tools.live_get_certificates("payments")

    assert result["certificates"][0]["days_until_expiry"] < 0


@pytest.mark.asyncio
async def test_live_get_certificates_no_matching_secrets(sa_token, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await live_tools.live_get_certificates("payments")

    assert result == {"namespace": "payments", "certificates": []}


@pytest.mark.asyncio
async def test_live_get_certificates_skips_unparseable_secret(sa_token, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [
            {"metadata": {"name": "corrupt-tls"}, "data": {"tls.crt": "bm90LWEtY2VydA=="}},
        ]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await live_tools.live_get_certificates("payments")

    assert result == {"namespace": "payments", "certificates": []}


# ── dispatch ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_routes_to_live_list_pods(sa_token, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await live_tools.dispatch("live_list_pods", {"namespace": "payments"})

    assert result == {"namespace": "payments", "pods": []}


@pytest.mark.asyncio
async def test_dispatch_routes_to_live_get_certificates(sa_token, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await live_tools.dispatch("live_get_certificates", {"namespace": "payments"})

    assert result == {"namespace": "payments", "certificates": []}


@pytest.mark.asyncio
async def test_dispatch_raises_on_unrecognized_tool():
    with pytest.raises(KeyError):
        await live_tools.dispatch("rotate_vault_cert", {})


# ── Missing-namespace/pod guards ──────────────────────────────────────────────
# An empty namespace segment (e.g. /api/v1/namespaces//pods) gets treated by
# the K8s API server as a CLUSTER-scoped request, which this collector's
# namespace-scoped RBAC Role always 403s on with a confusing
# "at the cluster scope" message. These functions must reject an empty
# namespace/pod before ever reaching the K8s API, not after.

@pytest.mark.asyncio
async def test_live_list_pods_rejects_empty_namespace():
    assert await live_tools.live_list_pods("") == {"error": "namespace is required"}


@pytest.mark.asyncio
async def test_live_get_pod_logs_rejects_empty_namespace_or_pod():
    assert await live_tools.live_get_pod_logs("", "pod-x") == {"error": "namespace and pod are required"}
    assert await live_tools.live_get_pod_logs("payments", "") == {"error": "namespace and pod are required"}


@pytest.mark.asyncio
async def test_live_get_events_rejects_empty_namespace():
    assert await live_tools.live_get_events("") == {"error": "namespace is required"}


@pytest.mark.asyncio
async def test_live_describe_pod_rejects_empty_namespace_or_pod():
    assert await live_tools.live_describe_pod("", "pod-x") == {"error": "namespace and pod are required"}
    assert await live_tools.live_describe_pod("payments", "") == {"error": "namespace and pod are required"}


@pytest.mark.asyncio
async def test_live_get_certificates_rejects_empty_namespace():
    assert await live_tools.live_get_certificates("") == {"error": "namespace is required"}
