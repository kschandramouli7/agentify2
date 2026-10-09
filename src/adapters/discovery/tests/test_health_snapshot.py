"""Tests for health_snapshot.py's push_health (ROADMAP P18 use case #5).

Same httpx.MockTransport pattern as test_inventory.py.
"""

import json

import httpx
import pytest

from discovery import health_snapshot

_RealAsyncClient = httpx.AsyncClient


def _client_factory(transport: httpx.MockTransport):
    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = transport
        return _RealAsyncClient(**kwargs)
    return factory


@pytest.mark.asyncio
async def test_push_health_sends_bearer_token_and_payload(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    await health_snapshot.push_health("v1.30.0", 42, 40, "http://backend", "secret-token")

    assert seen["auth"] == "Bearer secret-token"
    assert seen["url"] == "http://backend/api/cluster-health"
    # platform defaults to "" (the sentinel), always sent explicitly, never
    # omitted — platform-labeling extension, 2026-10-09.
    assert json.loads(seen["body"]) == {"k8s_version": "v1.30.0", "pods_total": 42, "pods_ready": 40, "platform": ""}


@pytest.mark.asyncio
async def test_push_health_sends_the_detected_platform(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    await health_snapshot.push_health("v1.28.5-eks-abc123", 1, 1, "http://backend", "secret-token", platform="eks")

    assert json.loads(seen["body"])["platform"] == "eks"


@pytest.mark.asyncio
async def test_push_health_degrades_silently_on_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    # Should not raise — best-effort, same convention as push_inventory/push_ingress.
    await health_snapshot.push_health("v1.30.0", 1, 1, "http://backend", "secret-token")
