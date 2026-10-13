"""Tests for log_platform_splunk.py (ADR 0041). httpx.AsyncClient is
monkeypatched via MockTransport (same pattern as test_service_topology.py)
so no real Splunk instance is contacted — this connector is design-first,
unit-tested against mocked HTTP, not yet live-verified (see the ADR).
"""

import httpx
import pytest

from k8fy import log_platform_splunk as sp

_RealAsyncClient = httpx.AsyncClient


def _client_factory(transport: httpx.MockTransport):
    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = transport
        return _RealAsyncClient(**kwargs)
    return factory


_CONFIG = {"url": "https://splunk.example.com:8089", "index": "k8s_logs", "token": "fake-token"}


@pytest.mark.asyncio
async def test_query_splunk_logs_rejects_invalid_namespace():
    result = await sp.query_splunk_logs("payments'; DROP TABLE x; --", None, _CONFIG)
    assert "error" in result
    assert "invalid namespace" in result["error"]


@pytest.mark.asyncio
async def test_query_splunk_logs_rejects_invalid_pod():
    result = await sp.query_splunk_logs("payments", "pod'; --", _CONFIG)
    assert "error" in result
    assert "invalid pod" in result["error"]


@pytest.mark.asyncio
async def test_query_splunk_logs_missing_config():
    result = await sp.query_splunk_logs("payments", None, {})
    assert "error" in result
    assert "not configured" in result["error"]


@pytest.mark.asyncio
async def test_query_splunk_logs_success(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/services/search/jobs":
            assert request.headers["authorization"] == "Bearer fake-token"
            return httpx.Response(200, json={"sid": "job-1"})
        if request.method == "GET" and request.url.path == "/services/search/jobs/job-1":
            return httpx.Response(200, json={"entry": [{"content": {"dispatchState": "DONE"}}]})
        if request.method == "GET" and request.url.path == "/services/search/jobs/job-1/results":
            return httpx.Response(200, json={"results": [
                {"kubernetes.pod_name": "payment-worker-abc", "kubernetes.container_name": "worker", "_raw": "line one"},
                {"kubernetes.pod_name": "payment-worker-abc", "kubernetes.container_name": "worker", "_raw": "line two"},
            ]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await sp.query_splunk_logs("payments", "payment-worker-abc", _CONFIG)
    assert result["namespace"] == "payments"
    assert result["pod"] == "payment-worker-abc"
    assert "line one" in result["logs"]
    assert "line two" in result["logs"]
    assert "[payment-worker-abc/worker]" in result["logs"]


@pytest.mark.asyncio
async def test_query_splunk_logs_polls_until_done(monkeypatch):
    """A job that isn't DONE on the first status check must be polled again,
    not treated as failed or returned empty."""
    poll_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"sid": "job-1"})
        if request.url.path == "/services/search/jobs/job-1":
            poll_count["n"] += 1
            state = "RUNNING" if poll_count["n"] == 1 else "DONE"
            return httpx.Response(200, json={"entry": [{"content": {"dispatchState": state}}]})
        if request.url.path == "/services/search/jobs/job-1/results":
            return httpx.Response(200, json={"results": [{"_raw": "found it"}]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await sp.query_splunk_logs("payments", None, _CONFIG)
    assert "found it" in result["logs"]
    assert poll_count["n"] == 2


@pytest.mark.asyncio
async def test_query_splunk_logs_job_failed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"sid": "job-1"})
        return httpx.Response(200, json={"entry": [{"content": {"dispatchState": "FAILED"}}]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await sp.query_splunk_logs("payments", None, _CONFIG)
    assert "error" in result
    assert "failed" in result["error"]


@pytest.mark.asyncio
async def test_query_splunk_logs_empty_results(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"sid": "job-1"})
        if request.url.path.endswith("/results"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"entry": [{"content": {"dispatchState": "DONE"}}]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await sp.query_splunk_logs("payments", None, _CONFIG)
    assert result == {"namespace": "payments", "pod": None, "logs": ""}


@pytest.mark.asyncio
async def test_query_splunk_logs_degrades_on_connection_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await sp.query_splunk_logs("payments", None, _CONFIG)
    assert "error" in result
    assert "connection refused" in result["error"]


@pytest.mark.asyncio
async def test_query_splunk_logs_degrades_on_http_status_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"messages": [{"text": "unauthorized"}]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await sp.query_splunk_logs("payments", None, _CONFIG)
    assert "error" in result


def test_build_spl_includes_index_and_namespace():
    spl = sp._build_spl("k8s_logs", "payments", None, 50)
    assert "index=k8s_logs" in spl
    assert 'kubernetes.namespace_name="payments"' in spl
    assert "| head 50" in spl


def test_build_spl_includes_pod_when_given():
    spl = sp._build_spl("k8s_logs", "payments", "payment-worker-abc", 50)
    assert 'kubernetes.pod_name="payment-worker-abc"' in spl
