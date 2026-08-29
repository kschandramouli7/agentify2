"""Tests for live_diagnostics.py — read-only LIVE Kubernetes API calls.

Same httpx.MockTransport pattern as test_action_executor.py: no real cluster
or network access required. Asserts on the exact URLs/params used, since
these are the calls a human's "Run" button in the UI directly triggers.
"""

import httpx
import pytest

from k8fy import k8s_client, live_diagnostics as ld


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
async def test_live_list_pods_missing_token(monkeypatch):
    monkeypatch.setattr(k8s_client, "_SA_TOKEN_PATH", "/nonexistent/path/token")
    result = await ld.live_list_pods("payments")
    assert "error" in result


@pytest.mark.asyncio
async def test_live_list_pods_summarizes_status(sa_token, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"items": [
            {
                "metadata": {"name": "payment-worker-abc"},
                "spec": {"nodeName": "fargate-ip-1"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"ready": True, "restartCount": 3}],
                },
            },
            {
                "metadata": {"name": "payment-worker-def"},
                "spec": {"nodeName": "fargate-ip-2"},
                "status": {"phase": "Pending", "containerStatuses": []},
            },
        ]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await ld.live_list_pods("payments")

    assert captured["url"] == "https://kubernetes.default.svc/api/v1/namespaces/payments/pods"
    assert captured["auth"] == "Bearer test-token"
    assert result["namespace"] == "payments"
    assert result["pods"] == [
        {"name": "payment-worker-abc", "phase": "Running", "ready": True, "restart_count": 3, "node": "fargate-ip-1"},
        {"name": "payment-worker-def", "phase": "Pending", "ready": False, "restart_count": 0, "node": "fargate-ip-2"},
    ]


@pytest.mark.asyncio
async def test_live_list_pods_surfaces_k8s_error(sa_token, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await ld.live_list_pods("payments")
    assert "error" in result
    assert "403" in result["error"]


@pytest.mark.asyncio
async def test_live_get_pod_logs_redacts_secrets(sa_token, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            text='line one\nAuthorization: Bearer abcdef123456789012\npassword="hunter2hunter2"\nline four',
        )

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await ld.live_get_pod_logs("payments", "payment-worker-abc", tail_lines=50)

    assert captured["url"].startswith(
        "https://kubernetes.default.svc/api/v1/namespaces/payments/pods/payment-worker-abc/log"
    )
    assert captured["params"]["tailLines"] == "50"
    assert "Bearer abcdef123456789012" not in result["logs"]
    assert "hunter2hunter2" not in result["logs"]
    assert "line one" in result["logs"]
    assert "line four" in result["logs"]


@pytest.mark.asyncio
async def test_live_get_pod_logs_caps_tail_lines(sa_token, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    await ld.live_get_pod_logs("payments", "pod-x", tail_lines=999999)
    assert captured["params"]["tailLines"] == "1000"


@pytest.mark.asyncio
async def test_live_get_events_filters_by_pod(sa_token, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [
            {"type": "Warning", "reason": "BackOff", "message": "crash",
             "count": 5, "lastTimestamp": "2026-07-24T00:00:00Z",
             "involvedObject": {"name": "pod-x"}},
        ]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await ld.live_get_events("payments", pod="pod-x")

    assert captured["params"]["fieldSelector"] == "involvedObject.name=pod-x"
    assert result["events"][0]["reason"] == "BackOff"


@pytest.mark.asyncio
async def test_live_describe_pod_combines_status_and_events(sa_token, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"items": [
                {"type": "Warning", "reason": "Unhealthy", "message": "probe failed",
                 "count": 2, "lastTimestamp": "2026-07-24T00:00:00Z",
                 "involvedObject": {"name": "pod-x"}},
            ]})
        return httpx.Response(200, json={
            "spec": {"nodeName": "fargate-ip-1"},
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True", "reason": ""}],
                "containerStatuses": [{
                    "name": "worker", "image": "worker:v2", "ready": True,
                    "restartCount": 1, "state": {"running": {}}, "lastState": {"terminated": {}},
                }],
            },
        })

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await ld.live_describe_pod("payments", "pod-x")

    assert result["phase"] == "Running"
    assert result["node"] == "fargate-ip-1"
    assert result["containers"] == [{
        "name": "worker", "image": "worker:v2", "ready": True,
        "restart_count": 1, "state": "running", "last_state": "terminated",
    }]
    assert result["events"][0]["reason"] == "Unhealthy"


@pytest.mark.asyncio
async def test_dispatch_rejects_unknown_tool():
    from k8fy.tools import _dispatch_live_diagnostic
    result = await _dispatch_live_diagnostic("live_delete_pod", {}, "http://backend")
    assert "error" in result
    assert "Unknown live diagnostic tool" in result["error"]


# ── Missing-namespace/pod guards ──────────────────────────────────────────────
# An empty namespace segment (e.g. /api/v1/namespaces//pods) gets treated by
# the K8s API server as a CLUSTER-scoped request, which this agent's
# namespace-scoped RBAC Role always 403s on with a confusing
# "at the cluster scope" message. These functions must reject an empty
# namespace/pod before ever reaching the K8s API, not after.

@pytest.mark.asyncio
async def test_live_list_pods_rejects_empty_namespace():
    result = await ld.live_list_pods("")
    assert result == {"error": "namespace is required"}


@pytest.mark.asyncio
async def test_live_get_pod_logs_rejects_empty_namespace_or_pod():
    assert await ld.live_get_pod_logs("", "pod-x") == {"error": "namespace and pod are required"}
    assert await ld.live_get_pod_logs("payments", "") == {"error": "namespace and pod are required"}


@pytest.mark.asyncio
async def test_live_get_events_rejects_empty_namespace():
    result = await ld.live_get_events("")
    assert result == {"error": "namespace is required"}


@pytest.mark.asyncio
async def test_live_describe_pod_rejects_empty_namespace_or_pod():
    assert await ld.live_describe_pod("", "pod-x") == {"error": "namespace and pod are required"}
    assert await ld.live_describe_pod("payments", "") == {"error": "namespace and pod are required"}
