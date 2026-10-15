"""LabelScopedDependencySkill — the single-shot /api/query entry point for
ROADMAP P32 (ADR 0043), reached via Go's answerDependencies bypass. Thin
wiring over the same k8fy.agent functions test_chat_label_scoped.py
already covers in depth, so this only checks the three dispatch outcomes.
"""
import pytest

from k8fy.agent import settings
from k8fy.skills.label_scoped_dependency import LabelScopedDependencySkill

EDGES = [{"from_service": "checkout-ui", "to_service": "checkout-api", "evidence_count": 5}]


@pytest.mark.asyncio
async def test_reason_unresolvable_query_returns_a_clear_message():
    # Go's intent regex can match a bare APM token even when no alias is
    # configured for it (Go has no access to that Python-side config) —
    # must not crash or answer nothing.
    monkeypatch_removed = settings.label_key_aliases_raw
    settings.label_key_aliases_raw = "{}"
    try:
        skill = LabelScopedDependencySkill()
        resp = await skill.reason("label_scoped_dependencies", {}, {"question": "what does APM123456 touch"})
    finally:
        settings.label_key_aliases_raw = monkeypatch_removed
    assert resp.status == "ok"
    assert "couldn't resolve" in resp.answer


@pytest.mark.asyncio
async def test_reason_untracked_key(monkeypatch):
    monkeypatch.setattr(settings, "tracked_label_keys_raw", "team")
    skill = LabelScopedDependencySkill()
    resp = await skill.reason("label_scoped_dependencies", {}, {"question": "apm-id=APM123456"})
    assert resp.status == "ok"
    assert resp.tier == "tier1"
    assert "isn't a label this deployment tracks" in resp.answer


@pytest.mark.asyncio
async def test_reason_tracked_with_matches(monkeypatch):
    monkeypatch.setattr(settings, "tracked_label_keys_raw", "apm-id")
    services = [{"cluster_id": "cluster-a", "namespace": "payments", "service": "checkout-ui"}]

    async def fake_fetch_services(label_key, label_value, backend_url):
        return services

    async def fake_fetch_deps(svc, backend_url):
        return EDGES

    monkeypatch.setattr("k8fy.service_topology.fetch_services_by_label", fake_fetch_services)
    monkeypatch.setattr("k8fy.service_topology.fetch_service_dependencies_for_services", fake_fetch_deps)

    skill = LabelScopedDependencySkill()
    resp = await skill.reason("label_scoped_dependencies", {}, {"question": "apm-id=APM123456"})
    assert resp.status == "ok"
    assert resp.tier == "tier1"
    assert resp.sources == ["service_labels", "service_dependencies"]
    assert "checkout-ui" in resp.answer
    assert resp.details["service_graph"]["dependencies"] == EDGES


@pytest.mark.asyncio
async def test_reason_degrades_gracefully_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(settings, "tracked_label_keys_raw", "apm-id")

    async def fake_fetch_services(label_key, label_value, backend_url):
        raise RuntimeError("backend unreachable")

    monkeypatch.setattr("k8fy.service_topology.fetch_services_by_label", fake_fetch_services)

    skill = LabelScopedDependencySkill()
    resp = await skill.reason("label_scoped_dependencies", {}, {"question": "apm-id=APM123456"})
    assert resp.status == "error"
    assert "temporarily unavailable" in resp.answer
