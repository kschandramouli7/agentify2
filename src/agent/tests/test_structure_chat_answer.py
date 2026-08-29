"""Tests for K8fyAgent._structure_chat_answer() — the second, schema-constrained
call that restructures reason_chat()'s free-form answer into sections.

The load-bearing assertion is the namespace override: the model has been
observed guessing "default" for a recommended_action's namespace instead of
using the conversation's real one — since RBAC (agent-live-diagnostics) only
grants access within the actual namespace, a wrong guess fails with 403 at
click-time. The fix never trusts the model for this field; the conversation's
own context always wins.

ADR 0028 extends the same distrust to cluster routing: CHAT_REASONING_SCHEMA
doesn't even allow the model to emit cluster_id/cluster_ids, so
_structure_chat_answer resolves it deterministically via
resolve_service_clusters (ADR 0023) and injects it — 0 resolved (or no
service in context) leaves arguments untouched (today's local-cluster
fallback, correct for non-fleet deployments); exactly 1 resolved -> singular
cluster_id; 2+ resolved and no pod in arguments -> cluster_ids (plural,
triggers tools.py's fan-out); 2+ resolved and pod present -> best-effort
singular cluster_id (a pod name is already cluster-specific, so fan-out
can't disambiguate it).
"""

import json
from types import SimpleNamespace

import pytest

from k8fy.agent import K8fyAgent


def _fake_response(payload: dict, usage=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        usage=usage or SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
    )


def _base_payload(**overrides):
    payload = {
        "status": "healthy",
        "severity": "info",
        "confidence": 0.9,
        "incident_summary": "payment-worker is healthy.",
        "timeline": ["2026-07-24: stable"],
        "findings": ["0 restarts"],
        "likely_cause": None,
        "recommendations": ["No action needed"],
        "recommended_actions": [],
    }
    payload.update(overrides)
    return payload


def _patch_resolve(monkeypatch, clusters):
    """Stub resolve_service_clusters and return a call tracker (list of
    (namespace, service) tuples it was called with)."""
    calls = []

    async def fake_resolve(namespace, service, backend_url):
        calls.append((namespace, service))
        return clusters

    monkeypatch.setattr("k8fy.service_topology.resolve_service_clusters", fake_resolve)
    return calls


@pytest.mark.asyncio
async def test_structure_chat_answer_overrides_hallucinated_namespace(monkeypatch):
    _patch_resolve(monkeypatch, [])
    agent = K8fyAgent()
    payload = _base_payload(recommended_actions=[{
        "label": "Verify live pod status for payment-worker",
        "tool": "live_list_pods",
        "arguments": {"namespace": "default", "pod": None, "container": None, "tail_lines": None, "previous": None},
    }])

    async def fake_create(**kwargs):
        return _fake_response(payload)

    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    details, usage = await agent._structure_chat_answer(
        "payment-worker is healthy.", {"namespace": "payments", "service": "payment-worker"},
    )

    assert details["recommended_actions"] == [{
        "label": "Verify live pod status for payment-worker",
        "tool": "live_list_pods",
        "arguments": {"namespace": "payments"},
    }]
    assert usage == (10, 5, 0, 0)


@pytest.mark.asyncio
async def test_structure_chat_answer_keeps_other_arguments(monkeypatch):
    _patch_resolve(monkeypatch, [])
    agent = K8fyAgent()
    payload = _base_payload(recommended_actions=[{
        "label": "Check payment-worker logs",
        "tool": "live_get_pod_logs",
        "arguments": {"namespace": "default", "pod": "payment-worker-abc", "container": None, "tail_lines": 100, "previous": None},
    }])

    async def fake_create(**kwargs):
        return _fake_response(payload)

    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    details, _ = await agent._structure_chat_answer(
        "answer", {"namespace": "payments", "service": "payment-worker"},
    )

    assert details["recommended_actions"][0]["arguments"] == {
        "namespace": "payments", "pod": "payment-worker-abc", "tail_lines": 100,
    }


@pytest.mark.asyncio
async def test_structure_chat_answer_no_context_namespace_leaves_model_value(monkeypatch):
    _patch_resolve(monkeypatch, [])
    agent = K8fyAgent()
    payload = _base_payload(recommended_actions=[{
        "label": "Verify live pod status",
        "tool": "live_list_pods",
        "arguments": {"namespace": "default", "pod": None, "container": None, "tail_lines": None, "previous": None},
    }])

    async def fake_create(**kwargs):
        return _fake_response(payload)

    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    details, _ = await agent._structure_chat_answer("answer", {})

    # No namespace known from context — can't override with an empty value,
    # so the model's (possibly wrong) guess passes through unchanged.
    assert details["recommended_actions"][0]["arguments"]["namespace"] == "default"


@pytest.mark.asyncio
async def test_structure_chat_answer_drops_action_with_no_namespace_anywhere(monkeypatch):
    # Neither context nor the model supplied a namespace — offering this
    # button would always 403 the K8s API server as a cluster-scoped
    # request (RBAC is namespace-scoped). Drop it rather than offer a
    # guaranteed-broken Run button.
    _patch_resolve(monkeypatch, [])
    agent = K8fyAgent()
    payload = _base_payload(recommended_actions=[{
        "label": "Verify live pod status",
        "tool": "live_list_pods",
        "arguments": {"namespace": None, "pod": None, "container": None, "tail_lines": None, "previous": None},
    }])

    async def fake_create(**kwargs):
        return _fake_response(payload)

    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    details, _ = await agent._structure_chat_answer("answer", {})

    assert details["recommended_actions"] == []


@pytest.mark.asyncio
async def test_structure_chat_answer_drops_pod_specific_action_with_no_pod(monkeypatch):
    _patch_resolve(monkeypatch, [])
    agent = K8fyAgent()
    payload = _base_payload(recommended_actions=[{
        "label": "Check payment-worker logs",
        "tool": "live_get_pod_logs",
        "arguments": {"namespace": "default", "pod": None, "container": None, "tail_lines": 100, "previous": None},
    }])

    async def fake_create(**kwargs):
        return _fake_response(payload)

    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    details, _ = await agent._structure_chat_answer(
        "answer", {"namespace": "payments", "service": "payment-worker"},
    )

    assert details["recommended_actions"] == []


@pytest.mark.asyncio
async def test_structure_chat_answer_keeps_valid_actions_and_drops_only_broken_ones(monkeypatch):
    _patch_resolve(monkeypatch, [])
    agent = K8fyAgent()
    payload = _base_payload(recommended_actions=[
        {
            "label": "List pods",
            "tool": "live_list_pods",
            "arguments": {"namespace": "default", "pod": None, "container": None, "tail_lines": None, "previous": None},
        },
        {
            "label": "Check logs with no pod",
            "tool": "live_get_pod_logs",
            "arguments": {"namespace": "default", "pod": None, "container": None, "tail_lines": 100, "previous": None},
        },
    ])

    async def fake_create(**kwargs):
        return _fake_response(payload)

    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    details, _ = await agent._structure_chat_answer(
        "answer", {"namespace": "payments", "service": "payment-worker"},
    )

    assert len(details["recommended_actions"]) == 1
    assert details["recommended_actions"][0]["label"] == "List pods"


@pytest.mark.asyncio
async def test_structure_chat_answer_empty_text_short_circuits(monkeypatch):
    agent = K8fyAgent()

    async def fake_create(**kwargs):
        raise AssertionError("should not call the API for empty answer text")

    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    details, usage = await agent._structure_chat_answer("   ", {"namespace": "payments"})
    assert details == {}
    assert usage == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_structure_chat_answer_degrades_on_api_error(monkeypatch):
    _patch_resolve(monkeypatch, [])
    agent = K8fyAgent()

    async def fake_create(**kwargs):
        raise RuntimeError("API unavailable")

    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    details, usage = await agent._structure_chat_answer("some answer", {"namespace": "payments"})
    assert details == {}
    assert usage == (0, 0, 0, 0)


# ── ADR 0028: cluster-routing injection ──────────────────────────────────────

@pytest.mark.asyncio
async def test_structure_chat_answer_injects_singular_cluster_id_when_one_resolves(monkeypatch):
    _patch_resolve(monkeypatch, ["cluster-a"])
    agent = K8fyAgent()
    payload = _base_payload(recommended_actions=[{
        "label": "List payment-worker pods",
        "tool": "live_list_pods",
        "arguments": {"namespace": "default", "pod": None, "container": None, "tail_lines": None, "previous": None},
    }])

    async def fake_create(**kwargs):
        return _fake_response(payload)

    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    details, _ = await agent._structure_chat_answer(
        "answer", {"namespace": "payments", "service": "payment-worker"},
    )

    assert details["recommended_actions"][0]["arguments"] == {
        "namespace": "payments", "cluster_id": "cluster-a",
    }


@pytest.mark.asyncio
async def test_structure_chat_answer_injects_cluster_ids_when_multiple_resolve_and_no_pod(monkeypatch):
    _patch_resolve(monkeypatch, ["cluster-a", "cluster-b"])
    agent = K8fyAgent()
    payload = _base_payload(recommended_actions=[{
        "label": "List payment-worker pods",
        "tool": "live_list_pods",
        "arguments": {"namespace": "default", "pod": None, "container": None, "tail_lines": None, "previous": None},
    }])

    async def fake_create(**kwargs):
        return _fake_response(payload)

    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    details, _ = await agent._structure_chat_answer(
        "answer", {"namespace": "payments", "service": "payment-worker"},
    )

    args = details["recommended_actions"][0]["arguments"]
    assert args["cluster_ids"] == ["cluster-a", "cluster-b"]
    assert "cluster_id" not in args


@pytest.mark.asyncio
async def test_structure_chat_answer_uses_best_effort_single_cluster_when_pod_present(monkeypatch):
    _patch_resolve(monkeypatch, ["cluster-a", "cluster-b"])
    agent = K8fyAgent()
    payload = _base_payload(recommended_actions=[{
        "label": "Check payment-worker logs",
        "tool": "live_get_pod_logs",
        "arguments": {"namespace": "default", "pod": "payment-worker-abc", "container": None, "tail_lines": 100, "previous": None},
    }])

    async def fake_create(**kwargs):
        return _fake_response(payload)

    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    details, _ = await agent._structure_chat_answer(
        "answer", {"namespace": "payments", "service": "payment-worker"},
    )

    args = details["recommended_actions"][0]["arguments"]
    # A pod name is already cluster-specific — fan-out can't disambiguate it,
    # so this takes the best-effort first-resolved-cluster path (ADR 0028),
    # never cluster_ids.
    assert args["cluster_id"] == "cluster-a"
    assert "cluster_ids" not in args


@pytest.mark.asyncio
async def test_structure_chat_answer_skips_resolution_without_service_in_context(monkeypatch):
    calls = _patch_resolve(monkeypatch, ["cluster-a"])
    agent = K8fyAgent()
    payload = _base_payload(recommended_actions=[{
        "label": "List payment-worker pods",
        "tool": "live_list_pods",
        "arguments": {"namespace": "default", "pod": None, "container": None, "tail_lines": None, "previous": None},
    }])

    async def fake_create(**kwargs):
        return _fake_response(payload)

    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    # namespace present, but no service — resolution requires both.
    details, _ = await agent._structure_chat_answer("answer", {"namespace": "payments"})

    assert calls == []
    args = details["recommended_actions"][0]["arguments"]
    assert "cluster_id" not in args
    assert "cluster_ids" not in args


@pytest.mark.asyncio
async def test_structure_chat_answer_no_clusters_resolved_leaves_arguments_unchanged(monkeypatch):
    _patch_resolve(monkeypatch, [])
    agent = K8fyAgent()
    payload = _base_payload(recommended_actions=[{
        "label": "List payment-worker pods",
        "tool": "live_list_pods",
        "arguments": {"namespace": "default", "pod": None, "container": None, "tail_lines": None, "previous": None},
    }])

    async def fake_create(**kwargs):
        return _fake_response(payload)

    monkeypatch.setattr(agent.client.messages, "create", fake_create)

    details, _ = await agent._structure_chat_answer(
        "answer", {"namespace": "payments", "service": "payment-worker"},
    )

    # 0 resolved clusters -> single-cluster/non-fleet deployment -> today's
    # local-execution fallback stays correct, arguments unchanged.
    assert details["recommended_actions"][0]["arguments"] == {"namespace": "payments"}
