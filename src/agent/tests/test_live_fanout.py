"""Tests for _dispatch_live_diagnostic's multi-cluster fan-out branch (ADR
0028): when `arguments["cluster_ids"]` (plural) is present, the same tool
call is relayed to every listed cluster in parallel via _remote_live_fetch
(unchanged, reused as-is — same httpx.MockTransport pattern as
test_remote_live_fetch.py), and the results are deterministically merged —
no LLM involved, matching Diagram 7/8's no-LLM property.
"""

import json

import httpx
import pytest

from k8fy.tools import _dispatch_live_diagnostic, _merge_fanout_results

_RealAsyncClient = httpx.AsyncClient


def _client_factory(transport: httpx.MockTransport):
    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = transport
        return _RealAsyncClient(**kwargs)
    return factory


def _cluster_id_from_request(request: httpx.Request) -> str:
    return json.loads(request.content)["cluster_id"]


@pytest.mark.asyncio
async def test_fanout_merges_pods_from_every_cluster_tagged_with_cluster_id(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        cid = _cluster_id_from_request(request)
        pods = {
            "cluster-a": [{"name": "payment-worker-1", "phase": "Running"}],
            "cluster-b": [{"name": "payment-worker-2", "phase": "Running"}],
        }[cid]
        return httpx.Response(200, json={"namespace": "payments", "pods": pods})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await _dispatch_live_diagnostic(
        "live_list_pods",
        {"namespace": "payments", "cluster_ids": ["cluster-a", "cluster-b"]},
        "http://backend",
    )

    assert result["clusters_queried"] == ["cluster-a", "cluster-b"]
    assert result["clusters_failed"] == []
    assert result["pods"] == [
        {"name": "payment-worker-1", "phase": "Running", "cluster_id": "cluster-a"},
        {"name": "payment-worker-2", "phase": "Running", "cluster_id": "cluster-b"},
    ]


@pytest.mark.asyncio
async def test_fanout_surfaces_partial_failure_without_dropping_the_other_cluster(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        cid = _cluster_id_from_request(request)
        if cid == "cluster-a":
            return httpx.Response(502, text="cluster not connected")
        return httpx.Response(200, json={"namespace": "payments", "pods": [{"name": "payment-worker-2"}]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await _dispatch_live_diagnostic(
        "live_list_pods",
        {"namespace": "payments", "cluster_ids": ["cluster-a", "cluster-b"]},
        "http://backend",
    )

    assert result["clusters_queried"] == ["cluster-b"]
    assert len(result["clusters_failed"]) == 1
    assert result["clusters_failed"][0]["cluster_id"] == "cluster-a"
    assert "502" in result["clusters_failed"][0]["error"]
    assert result["pods"] == [{"name": "payment-worker-2", "cluster_id": "cluster-b"}]


@pytest.mark.asyncio
async def test_fanout_of_one_still_merges_correctly(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"namespace": "payments", "pods": [{"name": "payment-worker-1"}]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await _dispatch_live_diagnostic(
        "live_list_pods",
        {"namespace": "payments", "cluster_ids": ["cluster-a"]},
        "http://backend",
    )

    assert result["clusters_queried"] == ["cluster-a"]
    assert result["pods"] == [{"name": "payment-worker-1", "cluster_id": "cluster-a"}]


@pytest.mark.asyncio
async def test_fanout_strips_cluster_id_and_cluster_ids_from_forwarded_args(monkeypatch):
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"namespace": "payments", "pods": []})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    await _dispatch_live_diagnostic(
        "live_list_pods",
        {"namespace": "payments", "cluster_ids": ["cluster-a", "cluster-b"]},
        "http://backend",
    )

    for body in captured:
        assert body["args"] == {"namespace": "payments"}
        assert "cluster_ids" not in body["args"]
        assert "cluster_id" not in body["args"]


@pytest.mark.asyncio
async def test_fanout_merges_events_field_for_live_get_events(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        cid = _cluster_id_from_request(request)
        events = [{"reason": f"event-from-{cid}"}]
        return httpx.Response(200, json={"namespace": "payments", "pod": None, "events": events})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    result = await _dispatch_live_diagnostic(
        "live_get_events",
        {"namespace": "payments", "cluster_ids": ["cluster-a", "cluster-b"]},
        "http://backend",
    )

    assert result["events"] == [
        {"reason": "event-from-cluster-a", "cluster_id": "cluster-a"},
        {"reason": "event-from-cluster-b", "cluster_id": "cluster-b"},
    ]


def test_merge_fanout_results_handles_a_raised_exception():
    result = _merge_fanout_results(
        "live_list_pods",
        ["cluster-a", "cluster-b"],
        [RuntimeError("connection refused"), {"namespace": "payments", "pods": [{"name": "payment-worker-2"}]}],
    )

    assert result["clusters_queried"] == ["cluster-b"]
    assert result["clusters_failed"] == [{"cluster_id": "cluster-a", "error": "connection refused"}]
    assert result["pods"] == [{"name": "payment-worker-2", "cluster_id": "cluster-b"}]


def test_merge_fanout_results_defaults_to_items_field_for_unknown_tool():
    result = _merge_fanout_results(
        "some_future_tool",
        ["cluster-a"],
        [{"items": [{"x": 1}]}],
    )

    assert result["items"] == [{"x": 1, "cluster_id": "cluster-a"}]
