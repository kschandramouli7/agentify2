"""Tests for the "trace <input>" chat route (ROADMAP P29) — mirrors
test_chat_service_graph.py's structure: detection (does the right text route
to "trace"?) and delivery (does reason_chat return tier1, no model call, with
details["call_trace"] intact?).
"""

import pytest

from k8fy.agent import K8fyAgent, _chat_route, _trace_answer, _trace_query_text


def _user(text):
    return [{"role": "user", "content": text}]


class _ExplodingMessages:
    """Any model call on this route is a bug, so make it loud."""

    async def create(self, **kwargs):
        raise AssertionError("reason_chat called the model on the trace route")


class _FakeClient:
    def __init__(self):
        self.messages = _ExplodingMessages()


def _bare_agent():
    agent = K8fyAgent.__new__(K8fyAgent)
    agent.client = _FakeClient()
    agent.backend_url = "http://backend"
    agent.athena_config = {"workgroup": "wg", "database": "db", "table": "tbl", "region": "us-east-1"}
    agent.max_iterations = 5
    agent.model = "test"
    agent.max_tokens = 100
    agent.effort = "low"
    agent._tools = []
    agent._static_system_prompt = "test system prompt"
    agent._prompt_name = "k8fy/chat"
    agent._prompt_fallback = "fallback"
    return agent


# ── Detection ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "trace a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
    "Trace POST /charge",
    "TRACE /charge",
    "  trace  550e8400-e29b-41d4-a716-446655440000",
])
def test_chat_route_recognizes_trace_trigger(text):
    assert _chat_route(_user(text)) == "trace"


def test_chat_route_trace_wins_over_dependency_keywords():
    """An explicit trigger word must never lose to the inferred-intent
    dependency heuristic, even when the rest of the message also mentions
    dependency-ish words."""
    assert _chat_route(_user("trace POST /charge, this looks like an upstream dependency issue")) == "trace"


def test_trace_query_text_extracts_the_input():
    assert _trace_query_text(_user("trace POST /charge")) == "POST /charge"
    assert _trace_query_text(_user("what are my dependencies?")) is None


# ── _trace_answer prose ───────────────────────────────────────────────────────

def test_trace_answer_invalid_input():
    answer, details = _trace_answer("nonsense", {"error": "bad shape", "error_kind": "invalid_input"})
    assert "doesn't look like a trace ID" in answer
    assert "call_trace" not in details


def test_trace_answer_query_error():
    answer, details = _trace_answer("POST /charge", {"error": "Athena timed out", "error_kind": "query_failed"})
    assert "Trace search failed" in answer
    assert "Athena timed out" in answer


def test_trace_answer_empty_hops_states_the_genuine_limit():
    result = {"kind": "url_path", "query": "POST /charge", "hours_back": 24, "hops": [], "unattributed_count": 0}
    answer, details = _trace_answer("POST /charge", result)
    assert "No log lines mentioned" in answer
    assert details["call_trace"]["hops"] == []


def test_trace_answer_hops_found():
    result = {
        "kind": "url_path", "query": "POST /charge", "hours_back": 24,
        "hops": [
            {"seq": 1, "timestamp": "2026-09-15T10:00:00+00:00", "cluster_id": "cluster-a",
             "namespace": "payments", "service": "frontend", "pod_name": "frontend-abc",
             "outcome": None, "log_excerpt": "POST /charge"},
            {"seq": 2, "timestamp": "2026-09-15T10:00:02+00:00", "cluster_id": "cluster-a",
             "namespace": "payments", "service": "payment-api", "pod_name": "payment-api-abc",
             "outcome": "success", "log_excerpt": "POST /charge 200"},
        ],
        "unattributed_count": 0,
    }
    answer, details = _trace_answer("POST /charge", result)
    assert "2 log lines" in answer
    assert "frontend" in answer and "payment-api" in answer
    assert details["call_trace"]["hops"] == result["hops"]


# ── reason_chat wiring ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reason_chat_answers_a_trace_question_without_a_model_call(monkeypatch):
    canned = {
        "kind": "trace_id", "query": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", "hours_back": 24,
        "hops": [{
            "seq": 1, "timestamp": "2026-09-15T10:00:00+00:00", "cluster_id": "cluster-a",
            "namespace": "payments", "service": "payment-api", "pod_name": "payment-api-abc",
            "outcome": "success", "log_excerpt": "handling trace",
        }],
        "unattributed_count": 0,
    }

    async def fake_search_trace(query_text, backend_url, athena_config, hours_back=24):
        return canned

    monkeypatch.setattr("k8fy.trace_search.search_trace", fake_search_trace)

    agent = _bare_agent()
    resp = await agent.reason_chat(_user("trace a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"), {})

    assert resp.tier == "tier1"
    assert resp.input_tokens == 0 and resp.output_tokens == 0
    assert resp.tool_calls == []
    assert resp.details["call_trace"]["hops"] == canned["hops"]
    assert resp.sources == ["raw_logs (Athena)"]


@pytest.mark.asyncio
async def test_reason_chat_falls_through_to_model_on_unexpected_search_exception(monkeypatch):
    async def fake_search_trace(query_text, backend_url, athena_config, hours_back=24):
        raise RuntimeError("boom")

    monkeypatch.setattr("k8fy.trace_search.search_trace", fake_search_trace)

    agent = _bare_agent()
    # _ExplodingMessages raises AssertionError once the model is actually
    # called; reason_chat's own broad except converts it into an error
    # response — either way, proves the turn was NOT answered by the trace
    # route (which would have returned tier1 above).
    resp = await agent.reason_chat(_user("trace a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"), {})
    assert resp.status == "error"
