"""Chat-side label-scoped dependency traceability (ROADMAP P32, ADR 0043).

Covers the Tier-1 fast path (_resolve_label_query, _chat_route) and the
deterministic answer (_build_label_scoped_graph, _label_scoped_answer) —
same split test_chat_service_graph.py already uses for the plain
dependencies route, since both are "no model call" paths with their own
correctness surface.
"""
import pytest

from k8fy.agent import (
    _build_label_scoped_graph,
    _chat_route,
    _label_scoped_answer,
    _resolve_label_query,
    settings,
)

EDGES = [
    {"from_service": "checkout-ui", "to_service": "checkout-api", "evidence_count": 42},
    {"from_service": "checkout-api", "to_service": "billing", "evidence_count": 7},
]


def _user(text):
    return [{"role": "user", "content": text}]


# ── _resolve_label_query ─────────────────────────────────────────────────────

def test_resolve_label_query_explicit_key_value():
    assert _resolve_label_query("show me team=checkout please") == ("team", "checkout")


def test_resolve_label_query_apm_token_with_alias_configured(monkeypatch):
    monkeypatch.setattr(settings, "label_key_aliases_raw", '{"apm": "apm-id"}')
    assert _resolve_label_query("what does APM123456 touch") == ("apm-id", "APM123456")


def test_resolve_label_query_apm_token_without_alias_is_none(monkeypatch):
    # No alias configured at all -- we have no idea what key "APM" shorthand
    # means on this deployment, so this must fall through to the model, not
    # guess (ADR 0043 Decision #6).
    monkeypatch.setattr(settings, "label_key_aliases_raw", "{}")
    assert _resolve_label_query("what does APM123456 touch") is None


def test_resolve_label_query_explicit_kv_wins_over_apm_alias(monkeypatch):
    monkeypatch.setattr(settings, "label_key_aliases_raw", '{"apm": "apm-id"}')
    # Both patterns are technically present; explicit syntax needs no config
    # lookup to trust, so it's checked first and wins.
    assert _resolve_label_query("team=checkout, also mentions APM123456") == ("team", "checkout")


def test_resolve_label_query_no_match_is_none():
    assert _resolve_label_query("is payment healthy?") is None


def test_resolve_label_query_does_not_match_ordinary_english():
    # No whitespace allowed around "=" -- must not fire on ordinary phrasing.
    assert _resolve_label_query("if x = 5 then what happens") is None


# ── _chat_route ───────────────────────────────────────────────────────────────

def test_chat_route_label_scoped_explicit_kv():
    assert _chat_route(_user("team=checkout")) == "label_scoped_dependencies"


def test_chat_route_label_scoped_apm_with_alias(monkeypatch):
    monkeypatch.setattr(settings, "label_key_aliases_raw", '{"apm": "apm-id"}')
    assert _chat_route(_user("show me the dependency flow for APM123456")) == "label_scoped_dependencies"


def test_chat_route_diagnostic_phrasing_wins_over_label_scoped(monkeypatch):
    monkeypatch.setattr(settings, "label_key_aliases_raw", '{"apm": "apm-id"}')
    assert _chat_route(_user("why is APM123456 failing")) is None  # None -> falls through to the model


def test_chat_route_ordinary_question_is_not_label_scoped():
    assert _chat_route(_user("who calls payment-api?")) == "dependencies"


# ── _build_label_scoped_graph ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_label_scoped_graph_untracked_key(monkeypatch):
    monkeypatch.setattr(settings, "tracked_label_keys_raw", "team,cost-center")
    graph = await _build_label_scoped_graph("apm-id", "APM123456", "http://backend")
    assert graph["tracked"] is False
    assert graph["tracked_keys"] == ["team", "cost-center"]


@pytest.mark.asyncio
async def test_build_label_scoped_graph_tracked_but_no_matches(monkeypatch):
    monkeypatch.setattr(settings, "tracked_label_keys_raw", "apm-id")

    async def fake_fetch_services(label_key, label_value, backend_url):
        return []

    monkeypatch.setattr("k8fy.service_topology.fetch_services_by_label", fake_fetch_services)

    graph = await _build_label_scoped_graph("apm-id", "APM-NEVER-SEEN", "http://backend")
    assert graph["tracked"] is True
    assert graph["services"] == []
    assert graph["dependencies"] == []


@pytest.mark.asyncio
async def test_build_label_scoped_graph_tracked_with_matches(monkeypatch):
    monkeypatch.setattr(settings, "tracked_label_keys_raw", "apm-id")
    services = [
        {"cluster_id": "cluster-a", "namespace": "payments", "service": "checkout-ui"},
        {"cluster_id": "cluster-a", "namespace": "payments", "service": "checkout-api"},
    ]

    async def fake_fetch_services(label_key, label_value, backend_url):
        assert (label_key, label_value) == ("apm-id", "APM123456")
        return services

    async def fake_fetch_deps(svc, backend_url):
        assert svc == services
        return EDGES

    monkeypatch.setattr("k8fy.service_topology.fetch_services_by_label", fake_fetch_services)
    monkeypatch.setattr("k8fy.service_topology.fetch_service_dependencies_for_services", fake_fetch_deps)

    graph = await _build_label_scoped_graph("apm-id", "APM123456", "http://backend")
    assert graph["tracked"] is True
    assert graph["services"] == services
    assert graph["dependencies"] == EDGES


# ── _label_scoped_answer ─────────────────────────────────────────────────────

def test_label_scoped_answer_untracked_key():
    answer, details = _label_scoped_answer({
        "label_key": "apm-id", "label_value": "APM123456", "tracked": False, "tracked_keys": ["team"],
    })
    assert "isn't a label this deployment tracks" in answer
    assert "team" in answer
    assert details["tracked"] is False


def test_label_scoped_answer_tracked_but_empty():
    answer, details = _label_scoped_answer({
        "label_key": "apm-id", "label_value": "APM-NEVER-SEEN", "tracked": True, "services": [], "dependencies": [],
    })
    assert "No services found tagged apm-id=APM-NEVER-SEEN" in answer
    assert details["services"] == []


def test_label_scoped_answer_surfaces_out_of_scope_downstream():
    # The core ADR 0043 Decision #5 requirement: billing isn't tagged
    # apm-id=APM123456 at all, but it's connected to a tagged service, so
    # it must still appear -- distinguished, not hidden, not silently
    # folded into the tagged set.
    graph = {
        "label_key": "apm-id", "label_value": "APM123456", "tracked": True,
        "services": [
            {"cluster_id": "cluster-a", "namespace": "payments", "service": "checkout-ui"},
            {"cluster_id": "cluster-a", "namespace": "payments", "service": "checkout-api"},
        ],
        "dependencies": EDGES,
    }
    answer, details = _label_scoped_answer(graph)
    assert "checkout-ui" in answer and "checkout-api" in answer
    assert "billing" in answer
    assert details["services"] == ["checkout-api", "checkout-ui"]
    assert details["out_of_scope_services"] == ["billing"]
    assert details["service_graph"]["dependencies"] == EDGES
    assert details["service_graph"]["focus"] is None
    assert "payments" in details["service_graph"]["namespace"]
