"""lookup_services_by_label tool (ROADMAP P32, ADR 0043) — the
conversational fallback for phrasing the Tier-1 fast path doesn't
recognize. _lookup_services_by_label calls config.settings.get_settings()
fresh on every invocation (it is NOT cached, unlike k8fy.agent's
module-level `settings`), so these tests set real env vars via
monkeypatch.setenv rather than patching a settings instance's attributes.
"""
import pytest

from k8fy.tools import TOOLS, process_tool_call

_BASE_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "LANGFUSE_PUBLIC_KEY": "pk-test",
    "LANGFUSE_SECRET_KEY": "sk-test",
}


def _env(monkeypatch, **extra):
    for k, v in {**_BASE_ENV, **extra}.items():
        monkeypatch.setenv(k, v)


def test_tool_is_registered_with_a_generic_schema():
    tool = next(t for t in TOOLS if t["name"] == "lookup_services_by_label")
    assert set(tool["input_schema"]["required"]) == {"label_key", "label_value"}
    # The SCHEMA itself must stay generic (label_key/label_value, never a
    # hardcoded apm_id property) -- the description text may still use APM
    # as one illustrative example of a label type, which is fine.
    assert set(tool["input_schema"]["properties"].keys()) == {"label_key", "label_value"}


@pytest.mark.asyncio
async def test_untracked_key_returns_the_tracked_list_not_a_guess(monkeypatch):
    _env(monkeypatch, TRACKED_LABEL_KEYS="team,cost-center")
    result = await process_tool_call("lookup_services_by_label", {"label_key": "apm-id", "label_value": "APM123456"}, "http://backend")
    assert result["tracked"] is False
    assert result["tracked_keys"] == ["team", "cost-center"]
    assert "error" in result


@pytest.mark.asyncio
async def test_tracked_key_returns_services_and_out_of_scope_edges(monkeypatch):
    _env(monkeypatch, TRACKED_LABEL_KEYS="apm-id")
    services = [{"cluster_id": "cluster-a", "namespace": "payments", "service": "checkout-ui"}]
    edges = [{"from_service": "checkout-ui", "to_service": "billing", "evidence_count": 3}]

    async def fake_fetch_services(label_key, label_value, backend_url):
        assert (label_key, label_value) == ("apm-id", "APM123456")
        return services

    async def fake_fetch_deps(svc, backend_url):
        assert svc == services
        return edges

    monkeypatch.setattr("k8fy.service_topology.fetch_services_by_label", fake_fetch_services)
    monkeypatch.setattr("k8fy.service_topology.fetch_service_dependencies_for_services", fake_fetch_deps)

    result = await process_tool_call(
        "lookup_services_by_label", {"label_key": "apm-id", "label_value": "APM123456"}, "http://backend",
    )
    assert result["tracked"] is True
    assert result["services"] == services
    assert result["dependencies"] == edges
    # billing isn't tagged apm-id=APM123456 but is connected to a tagged
    # service -- must still be surfaced, distinguished (ADR 0043 Decision #5).
    assert result["out_of_scope_services"] == ["billing"]


@pytest.mark.asyncio
async def test_tracked_key_with_no_matches(monkeypatch):
    _env(monkeypatch, TRACKED_LABEL_KEYS="apm-id")

    async def fake_fetch_services(label_key, label_value, backend_url):
        return []

    monkeypatch.setattr("k8fy.service_topology.fetch_services_by_label", fake_fetch_services)

    result = await process_tool_call(
        "lookup_services_by_label", {"label_key": "apm-id", "label_value": "APM-NEVER-SEEN"}, "http://backend",
    )
    assert result["tracked"] is True
    assert result["services"] == []
    assert result["dependencies"] == []
