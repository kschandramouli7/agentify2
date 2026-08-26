"""Tests for inventory.py's push_inventory.

Mirrors test_service_topology.py's coverage of push_dependency (same
httpx.MockTransport pattern) — push_inventory shares the same bearer-token +
best-effort-swallow-on-failure conventions.
"""

import json

import httpx
import pytest

from discovery import inventory

_RealAsyncClient = httpx.AsyncClient


def _client_factory(transport: httpx.MockTransport):
    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = transport
        return _RealAsyncClient(**kwargs)
    return factory


@pytest.mark.asyncio
async def test_push_inventory_sends_bearer_token_and_namespace_services(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    await inventory.push_inventory(
        {"payments": ["payment-api"], "checkout": []}, "http://backend", "secret-token",
    )

    assert seen["auth"] == "Bearer secret-token"
    assert seen["url"] == "http://backend/api/cluster-inventory"
    body = json.loads(seen["body"])
    assert {"name": "payments", "services": ["payment-api"]} in body["namespaces"]
    assert {"name": "checkout", "services": []} in body["namespaces"]


@pytest.mark.asyncio
async def test_push_inventory_carries_each_service_selector(monkeypatch):
    # ADR 0029: main.py's _namespace_services now hands push_inventory the
    # richer {"name": ..., "selector": {...}} shape, not bare names.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    await inventory.push_inventory(
        {"payments": [{"name": "payment-api", "selector": {"app": "payment-api"}}]},
        "http://backend", "secret-token",
    )

    body = json.loads(seen["body"])
    assert body["namespaces"] == [
        {"name": "payments", "services": [{"name": "payment-api", "selector": {"app": "payment-api"}}]},
    ]


@pytest.mark.asyncio
async def test_push_inventory_degrades_silently_on_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    # Should not raise — best-effort, same convention as push_dependency.
    await inventory.push_inventory({"payments": ["payment-api"]}, "http://backend", "secret-token")
