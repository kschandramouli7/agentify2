"""Tests for _get_similar_incidents (P8 — semantic memory layer, ADR 0018):
the tool DiagnoseSkill's prefetch calls to retrieve past incidents similar
to the one being diagnosed. Same httpx.MockTransport pattern as
test_remote_live_fetch.py; Voyage AI's client is monkeypatched rather than
called for real.
"""

import httpx
import pytest

from k8fy.tools import _get_similar_incidents

_RealAsyncClient = httpx.AsyncClient


def _client_factory(transport: httpx.MockTransport):
    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = transport
        return _RealAsyncClient(**kwargs)
    return factory


class _FakeSettings:
    def __init__(self, voyage_api_key="", voyage_model="voyage-3-lite"):
        self.voyage_api_key = voyage_api_key
        self.voyage_model = voyage_model


def _patch_settings(monkeypatch, voyage_api_key=""):
    monkeypatch.setattr("config.settings.get_settings", lambda: _FakeSettings(voyage_api_key=voyage_api_key))


@pytest.mark.asyncio
async def test_queries_backend_with_namespace_service_limit(monkeypatch):
    _patch_settings(monkeypatch)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=[{"summary": "OOMKilled last week", "likely_cause": "memory leak"}])

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await _get_similar_incidents("http://backend", "payments", "payment-worker", "crash looping", limit=3)

    assert captured["url"].startswith("http://backend/api/incidents/similar?")
    assert "namespace=payments" in captured["url"]
    assert "service=payment-worker" in captured["url"]
    assert "limit=3" in captured["url"]
    assert result == {"similar_incidents": [{"summary": "OOMKilled last week", "likely_cause": "memory leak"}]}


@pytest.mark.asyncio
async def test_without_voyage_key_does_not_include_vec_param(monkeypatch):
    _patch_settings(monkeypatch, voyage_api_key="")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    await _get_similar_incidents("http://backend", "payments", "payment-worker", "crash looping")

    assert "vec=" not in captured["url"]


@pytest.mark.asyncio
async def test_with_voyage_key_embeds_description_into_vec_param(monkeypatch):
    _patch_settings(monkeypatch, voyage_api_key="test-key")

    class _FakeEmbedResult:
        embeddings = [[0.1, 0.2, 0.3]]

    class _FakeVoyageClient:
        def __init__(self, api_key):
            assert api_key == "test-key"

        def embed(self, texts, model):
            assert texts == ["crash looping"]
            return _FakeEmbedResult()

    import voyageai
    monkeypatch.setattr(voyageai, "Client", _FakeVoyageClient)

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    await _get_similar_incidents("http://backend", "payments", "payment-worker", "crash looping")

    assert "vec=0.1" in captured["url"] or "vec=0.100000" in captured["url"]


@pytest.mark.asyncio
async def test_embed_failure_falls_back_to_keyword_search(monkeypatch):
    _patch_settings(monkeypatch, voyage_api_key="test-key")

    class _FakeVoyageClient:
        def __init__(self, api_key):
            pass

        def embed(self, texts, model):
            raise RuntimeError("voyage API unavailable")

    import voyageai
    monkeypatch.setattr(voyageai, "Client", _FakeVoyageClient)

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await _get_similar_incidents("http://backend", "payments", "payment-worker", "crash looping")

    assert "vec=" not in captured["url"]
    assert result == {"similar_incidents": [], "note": "No past incidents found for this service."}


@pytest.mark.asyncio
async def test_empty_result_returns_helpful_note(monkeypatch):
    _patch_settings(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await _get_similar_incidents("http://backend", "payments", "payment-worker", "crash looping")

    assert result == {"similar_incidents": [], "note": "No past incidents found for this service."}


@pytest.mark.asyncio
async def test_non_200_backend_response_is_reported_not_raised(monkeypatch):
    _patch_settings(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="db unavailable")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await _get_similar_incidents("http://backend", "payments", "payment-worker", "crash looping")

    assert result == {"similar_incidents": [], "note": "Backend returned 500"}


@pytest.mark.asyncio
async def test_request_exception_is_swallowed_not_raised(monkeypatch):
    _patch_settings(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await _get_similar_incidents("http://backend", "payments", "payment-worker", "crash looping")

    assert result["similar_incidents"] == []
    assert "error" in result
