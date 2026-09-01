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
    _focus_service,
    _looks_like_dependency_question,
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
