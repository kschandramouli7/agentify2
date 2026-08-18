"""Tests for DiagnoseSkill's live-fallback prefetch step (ROADMAP P16 / ADR
0023): resolve_service_clusters resolves which fleet cluster(s) run the
service being diagnosed, and one live_list_pods prefetch task (plus, per ADR
0024, one cluster-scoped get_service_health task) is added per resolved
cluster — routed through the existing process_tool_call /
_dispatch_live_diagnostic relay (ROADMAP P18 use case #9) and HandleAgentFetch
(ADR 0024) via self._fetch, so no new plumbing is exercised here beyond the
resolution + task-building logic itself.
"""

import pytest

from k8fy.skills import diagnose as diagnose_module
from k8fy.skills.diagnose import DiagnoseSkill


@pytest.mark.asyncio
async def test_prefetch_adds_one_live_task_per_resolved_cluster(monkeypatch):
    calls = []

    async def fake_resolve(namespace, service, backend_url):
        assert namespace == "payments"
        assert service == "payment-api"
        return ["cluster-42", "cluster-99"]

    async def fake_fetch(self, tool_name, args):
        calls.append((tool_name, dict(args)))
        return {"stub": True}

    async def fake_fetch_deps(namespace, backend_url):
        return []

    monkeypatch.setattr(diagnose_module, "resolve_service_clusters", fake_resolve)
    monkeypatch.setattr(diagnose_module, "fetch_service_dependencies", fake_fetch_deps)
    monkeypatch.setattr(DiagnoseSkill, "_fetch", fake_fetch)

    skill = DiagnoseSkill()
    prefetched = await skill._prefetch({}, {"namespace": "payments", "service_name": "payment-api"})

    assert prefetched.get("live_pods.cluster-42") == {"stub": True}
    assert prefetched.get("live_pods.cluster-99") == {"stub": True}
    assert prefetched.get("service_health.cluster-42") == {"stub": True}
    assert prefetched.get("service_health.cluster-99") == {"stub": True}

    live_call_args = [args for name, args in calls if name == "live_list_pods"]
    assert {"namespace": "payments", "cluster_id": "cluster-42"} in live_call_args
    assert {"namespace": "payments", "cluster_id": "cluster-99"} in live_call_args

    health_call_args = [args for name, args in calls if name == "get_service_health"]
    assert {"service_name": "payment-api", "namespace": "payments", "cluster_id": "cluster-42"} in health_call_args
    assert {"service_name": "payment-api", "namespace": "payments", "cluster_id": "cluster-99"} in health_call_args


@pytest.mark.asyncio
async def test_prefetch_skips_live_tasks_when_no_cluster_resolves(monkeypatch):
    async def fake_resolve(namespace, service, backend_url):
        return []

    async def fake_fetch(self, tool_name, args):
        return {"stub": True}

    async def fake_fetch_deps(namespace, backend_url):
        return []

    monkeypatch.setattr(diagnose_module, "resolve_service_clusters", fake_resolve)
    monkeypatch.setattr(diagnose_module, "fetch_service_dependencies", fake_fetch_deps)
    monkeypatch.setattr(DiagnoseSkill, "_fetch", fake_fetch)

    skill = DiagnoseSkill()
    prefetched = await skill._prefetch({}, {"namespace": "payments", "service_name": "payment-api"})

    assert not any(key.startswith("live_pods.") or key.startswith("service_health.cluster") for key in prefetched)


@pytest.mark.asyncio
async def test_prefetch_never_resolves_without_a_service_name(monkeypatch):
    resolve_called = False

    async def fake_resolve(namespace, service, backend_url):
        nonlocal resolve_called
        resolve_called = True
        return ["cluster-42"]

    async def fake_fetch(self, tool_name, args):
        return {"stub": True}

    async def fake_fetch_deps(namespace, backend_url):
        return []

    monkeypatch.setattr(diagnose_module, "resolve_service_clusters", fake_resolve)
    monkeypatch.setattr(diagnose_module, "fetch_service_dependencies", fake_fetch_deps)
    monkeypatch.setattr(DiagnoseSkill, "_fetch", fake_fetch)

    skill = DiagnoseSkill()
    # No service_name in context -> resolution never runs, same as the other
    # service_name-gated prefetch steps (service_health, similar_incidents).
    prefetched = await skill._prefetch({}, {"namespace": "payments"})

    assert resolve_called is False
    assert "similar_incidents" not in prefetched


@pytest.mark.asyncio
async def test_prefetch_adds_similar_incidents_task_when_service_name_present(monkeypatch):
    calls = []

    async def fake_resolve(namespace, service, backend_url):
        return []

    async def fake_fetch(self, tool_name, args):
        calls.append((tool_name, dict(args)))
        return {"similar_incidents": [{"summary": "past OOMKill"}]}

    async def fake_fetch_deps(namespace, backend_url):
        return []

    monkeypatch.setattr(diagnose_module, "resolve_service_clusters", fake_resolve)
    monkeypatch.setattr(diagnose_module, "fetch_service_dependencies", fake_fetch_deps)
    monkeypatch.setattr(DiagnoseSkill, "_fetch", fake_fetch)

    skill = DiagnoseSkill()
    prefetched = await skill._prefetch({}, {"namespace": "payments", "service_name": "payment-api"})

    assert prefetched["similar_incidents"] == {"similar_incidents": [{"summary": "past OOMKill"}]}
    similar_call_args = [args for name, args in calls if name == "get_similar_incidents"]
    assert similar_call_args == [{
        "namespace": "payments",
        "service": "payment-api",
        "description": "service health issues in payments/payment-api",
        "limit": 3,
    }]
