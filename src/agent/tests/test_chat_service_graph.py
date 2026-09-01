"""Chat-side surfacing of the mined dependency graph.

Covers the two things that can silently regress:
  1. detection — a dependency question must attach the graph, and an ordinary
     question must NOT (a graph on every answer is noise, and it costs a
     backend round-trip per turn);
  2. delivery — the edges must reach `details["service_graph"]` intact, because
     the UI draws from them. `_structure_chat_answer` rebuilds details from the
     PROSE, so anything not attached explicitly is lost.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from k8fy.agent import (  # noqa: E402
    _build_service_graph,
    _chat_route,
    _dependency_answer,
    _focus_service,
    _looks_like_dependency_question,
    _reach,
)

EDGES = [
    {"id": "1", "from_service": "payment-batch", "to_service": "payment-worker", "evidence_count": 187},
    {"id": "2", "from_service": "payment-batch", "to_service": "payment-api", "evidence_count": 187},
    {"id": "3", "from_service": "payment-worker", "to_service": "payment-api", "evidence_count": 13},
]


def _user(text):
    return [{"role": "user", "content": text}]


@pytest.mark.parametrize("question", [
    "what are the upstream and downstream dependencies for payment-api?",
    "What are the dependencies of payment-worker?",
    "who calls payment-api?",
    "show me the call graph for payments",
    "what's the blast radius if payment-api goes down",
    "which services depend on payment?",
    "what would be affected if payment-worker fails",
    "list the callers and callees of payment",
])
def test_dependency_questions_are_detected(question):
    assert _looks_like_dependency_question(_user(question)) is True


@pytest.mark.parametrize("question", [
    "why is payment-worker crashing?",
    "is payment-api healthy?",
    "show me the logs for payment-api",
    "what changed recently in payments?",
    "are the certs expiring soon?",
    "restart the payment-worker deployment",
    "what's the CPU usage of payment-api",
])
def test_ordinary_questions_are_not_detected(question):
    """False positives cost a backend call and push the answer down the page."""
    assert _looks_like_dependency_question(_user(question)) is False


def test_only_the_latest_user_turn_counts():
    """A conversation that discussed dependencies earlier must not attach a
    graph to every later answer."""
    convo = [
        {"role": "user", "content": "what are the dependencies of payment-api"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "ok now show me its logs"},
    ]
    assert _looks_like_dependency_question(convo) is False


def test_no_user_turn_at_all_is_safe():
    assert _looks_like_dependency_question([{"role": "assistant", "content": "deps?"}]) is False
    assert _looks_like_dependency_question([]) is False


def test_non_string_content_does_not_raise():
    """Tool-result turns carry a list, not a string."""
    msgs = [{"role": "user", "content": [{"type": "tool_result", "content": "dependencies"}]}]
    assert isinstance(_looks_like_dependency_question(msgs), bool)


SERVICES = ["payment", "payment-api", "payment-batch", "payment-worker"]


def test_focus_prefers_the_longest_matching_service():
    """"payment-api" must not be resolved to "payment" just because that
    substring appears first."""
    assert _focus_service(_user("dependencies of payment-api"), {}, SERVICES) == "payment-api"
    assert _focus_service(_user("what depends on payment?"), {}, SERVICES) == "payment"


def test_focus_falls_back_to_context_then_none():
    assert _focus_service(_user("what are the dependencies"), {"service": "payment-api"}, SERVICES) == "payment-api"
    # A context service that isn't in the graph must not be focused — the UI
    # would then focus a node that doesn't exist.
    assert _focus_service(_user("deps?"), {"service": "not-in-graph"}, SERVICES) is None
    assert _focus_service(_user("deps?"), {}, SERVICES) is None


def test_focus_never_invents_a_service():
    assert _focus_service(_user("dependencies of totally-made-up"), {}, SERVICES) is None


@pytest.mark.asyncio
async def test_build_service_graph_returns_edges_and_focus(monkeypatch):
    async def fake_fetch(namespace, backend_url):
        assert namespace == "payments"
        return EDGES

    monkeypatch.setattr("k8fy.service_topology.fetch_service_dependencies", fake_fetch)
    graph = await _build_service_graph(
        _user("upstream and downstream dependencies of payment-api"),
        {"namespace": "payments"}, "http://backend",
    )
    assert graph is not None
    assert graph["namespace"] == "payments"
    assert graph["focus"] == "payment-api"
    # The edges must arrive unchanged — the UI's line weights and counts come
    # straight from these.
    assert graph["dependencies"] == EDGES


@pytest.mark.asyncio
async def test_build_service_graph_needs_a_namespace():
    assert await _build_service_graph(_user("deps?"), {}, "http://backend") is None
    assert await _build_service_graph(_user("deps?"), {"namespace": ""}, "http://backend") is None


@pytest.mark.asyncio
async def test_build_service_graph_returns_none_when_empty(monkeypatch):
    """An empty graph attaches nothing, so the UI shows no empty diagram."""
    async def fake_fetch(namespace, backend_url):
        return []

    monkeypatch.setattr("k8fy.service_topology.fetch_service_dependencies", fake_fetch)
    assert await _build_service_graph(_user("deps?"), {"namespace": "payments"}, "http://backend") is None


@pytest.mark.asyncio
async def test_build_service_graph_swallows_fetch_failure(monkeypatch):
    """A missing graph must never turn a chat answer into an error."""
    async def boom(namespace, backend_url):
        raise RuntimeError("backend down")

    monkeypatch.setattr("k8fy.service_topology.fetch_service_dependencies", boom)
    assert await _build_service_graph(_user("deps?"), {"namespace": "payments"}, "http://backend") is None


# ── Deterministic routing ─────────────────────────────────────────────────────


@pytest.mark.parametrize("question", [
    "what are the upstream and downstream dependencies for payment-api?",
    "who calls payment-api?",
    "show me the call graph for payments",
    "which services depend on payment?",
    "what's the blast radius of payment-api going down",
    "list the callers and callees of payment",
])
def test_pure_dependency_questions_route_deterministically(question):
    assert _chat_route(_user(question)) == "dependencies"


@pytest.mark.parametrize("question", [
    # The graph alone cannot answer these — it holds no health, log, cert,
    # metric or change data. Short-circuiting would drop half the question.
    "why is payment-api slow, does it depend on vault?",
    "what are payment-api's dependencies and is it healthy?",
    "dependencies of payment-api - also show me its logs",
    "what changed in payment-api's dependencies recently?",
    "is payment-api's dependency on payment causing the timeout?",
    "what depends on payment-api and should I restart it?",
    "why is payment-worker crashing? what does it depend on?",
])
def test_mixed_questions_still_go_to_the_model(question):
    """A dependency mention does not license skipping synthesis.

    These must return None so reason_chat runs the full loop — the graph is
    still attached to the answer, so nothing is lost by NOT short-circuiting.
    """
    assert _chat_route(_user(question)) is None


@pytest.mark.parametrize("question", [
    "is payment-api healthy?",
    "show me the logs",
    "restart payment-worker",
])
def test_non_dependency_questions_do_not_route(question):
    assert _chat_route(_user(question)) is None


# ── The deterministic answer ──────────────────────────────────────────────────


def test_focused_answer_states_both_directions_and_the_caveat():
    answer, details = _dependency_answer(
        {"namespace": "payments", "focus": "payment-api", "dependencies": EDGES}
    )
    assert "payment-api is called by 2 services and calls nothing." == details["incident_summary"]
    assert "payment-batch, payment-worker" in answer          # upstream, sorted
    assert "none observed" in answer                          # downstream, honestly empty
    assert "lower bound" in answer                            # the caveat is never omitted
    assert details["service_graph"]["dependencies"] == EDGES
    # No health verdict is asserted — this is a topology report.
    assert details["severity"] == "info"
    assert details["likely_cause"] is None


def test_focused_answer_reports_the_blast_radius_with_hop_distance():
    answer, details = _dependency_answer(
        {"namespace": "payments", "focus": "payment-api", "dependencies": EDGES}
    )
    assert "2 services would be affected" in answer
    assert "payment-worker (direct)" in answer
    # payment-batch reaches payment-api directly AND via payment-worker; BFS
    # must report the shorter one.
    assert "payment-batch (direct)" in answer


def test_entry_point_is_described_as_having_no_upstream_impact():
    answer, details = _dependency_answer(
        {"namespace": "payments", "focus": "payment-batch", "dependencies": EDGES}
    )
    assert "called by nothing" in details["incident_summary"]
    assert "no upstream impact is known" in answer


def test_namespace_wide_answer_when_no_service_is_named():
    answer, details = _dependency_answer(
        {"namespace": "payments", "focus": None, "dependencies": EDGES}
    )
    assert "3 services with 3 observed calls" in details["incident_summary"]
    assert "payment-batch" in answer   # the entry point
    assert "Name a service" in answer  # tells the user how to narrow it
    assert details["service_graph"]["focus"] is None


def test_reach_matches_a_breadth_first_reading():
    """The prose and the diagram must not disagree, so both are BFS over the
    same edges. payment-batch reaches payment-api directly and via
    payment-worker; the answer is 1, not 2."""
    up = _reach(EDGES, "payment-api", forward=False)
    assert up == {"payment-batch": 1, "payment-worker": 1}
    down = _reach(EDGES, "payment-batch", forward=True)
    assert down == {"payment-worker": 1, "payment-api": 1}


def test_reach_terminates_on_a_cycle():
    cyclic = [
        {"id": "a", "from_service": "a", "to_service": "b", "evidence_count": 1},
        {"id": "b", "from_service": "b", "to_service": "c", "evidence_count": 1},
        {"id": "c", "from_service": "c", "to_service": "a", "evidence_count": 1},
    ]
    assert _reach(cyclic, "a", forward=True) == {"b": 1, "c": 2}


def test_answer_never_contains_an_unformatted_count():
    """Guards the wording: "calls 0" and "called by 0 services" read badly and
    were shipped once."""
    for focus in ("payment-batch", "payment-worker", "payment-api"):
        _, details = _dependency_answer(
            {"namespace": "payments", "focus": focus, "dependencies": EDGES}
        )
        assert " 0 " not in details["incident_summary"]
        assert "calls 0." not in details["incident_summary"]


# ── End to end through reason_chat ────────────────────────────────────────────
#
# The tests above prove the routing decision and the answer text. These prove
# the decision is actually WIRED: that a routed turn makes no model call, and
# that an unrouted one still gets the graph attached.


class _ExplodingMessages:
    """Any model call in a deterministic path is a bug, so make it loud."""

    async def create(self, **kwargs):
        raise AssertionError("reason_chat called the model on a deterministic route")


class _FakeClient:
    def __init__(self):
        self.messages = _ExplodingMessages()


def _bare_agent():
    """A K8fyAgent with only what reason_chat needs, no real client.

    _static_system_prompt is set so @_with_system_prompt resolves without
    touching Langfuse — the decorator runs before the deterministic route, by
    design (see its comment on keeping the turn traced).
    """
    from k8fy.agent import K8fyAgent

    agent = K8fyAgent.__new__(K8fyAgent)
    agent.client = _FakeClient()
    agent.backend_url = "http://backend"
    agent.max_iterations = 5
    agent.model = "test"
    agent.max_tokens = 100
    agent.effort = "low"
    agent._tools = []
    agent._static_system_prompt = "test system prompt"
    agent._prompt_name = "k8fy/chat"
    agent._prompt_fallback = "fallback"
    return agent


@pytest.mark.asyncio
async def test_reason_chat_answers_a_dependency_question_without_a_model_call(monkeypatch):
    async def fake_fetch(namespace, backend_url):
        return EDGES

    monkeypatch.setattr("k8fy.service_topology.fetch_service_dependencies", fake_fetch)

    agent = _bare_agent()

    resp = await type(agent).reason_chat(
        agent,
        _user("what are the upstream and downstream dependencies for payment-api?"),
        {"namespace": "payments", "service": "payment-api"},
    )

    assert resp.tier == "tier1"
    # No model call means no tokens and no cost — the whole point of routing.
    assert resp.input_tokens == 0 and resp.output_tokens == 0
    assert resp.estimated_cost_usd == 0.0
    assert resp.tool_calls == []
    assert resp.details["service_graph"]["focus"] == "payment-api"
    assert "payment-batch, payment-worker" in resp.answer
    assert resp.sources == ["service_dependencies"]


@pytest.mark.asyncio
async def test_reason_chat_falls_through_to_the_model_when_no_graph_exists(monkeypatch):
    """A dependency question with no mined evidence is better answered by the
    model, which can explain why the namespace is empty. A bare "no data" would
    be a dead end, so the route must NOT swallow the turn."""
    async def empty(namespace, backend_url):
        return []

    monkeypatch.setattr("k8fy.service_topology.fetch_service_dependencies", empty)

    agent = _bare_agent()

    # Reaching the model raises AssertionError from _ExplodingMessages; the
    # broad except in reason_chat converts it into an error response. Either way
    # the turn was NOT answered deterministically, which is what we assert.
    resp = await type(agent).reason_chat(
        agent, _user("who calls payment-api?"), {"namespace": "payments"},
    )
    assert resp.tier != "tier1"
    assert "service_graph" not in (resp.details or {})


@pytest.mark.asyncio
async def test_deterministic_answer_is_not_stamped_with_a_prompt_version(monkeypatch):
    """@_with_system_prompt stamps prompt provenance on every AgentResponse.

    A tier1 answer had no prompt, and prompt_version is what the promotion gate
    (docs/PROMPT_LIFECYCLE.md) measures a candidate by — so attributing a
    deterministic answer to a version would quietly distort promotion
    decisions in the version's favour.
    """
    async def fake_fetch(namespace, backend_url):
        return EDGES

    monkeypatch.setattr("k8fy.service_topology.fetch_service_dependencies", fake_fetch)
    agent = _bare_agent()
    resp = await type(agent).reason_chat(
        agent, _user("who calls payment-api?"), {"namespace": "payments"},
    )
    assert resp.tier == "tier1"
    assert resp.prompt_name is None
    assert resp.prompt_version is None
