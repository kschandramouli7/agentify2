"""DependencyGraphSkill — handles the `dependencies` intent (ROADMAP P18 #2).

Deterministic only — zero Claude calls, like RemediationExecutorSkill.

"What are the upstream and downstream dependencies of payment-api?" is a graph
traversal with one correct answer, already sitting in Postgres. A model adds
latency and cost, and introduces a chance of paraphrasing counts that are
already easy to misread (docs/SERVICE_DEPENDENCIES.md §3). The same reasoning
ADR 0029 applies to mining the graph — "a plain extraction task; no Claude call
belongs anywhere in this pipeline" — applies to reading it back.

WHY THIS REUSES THE CHAT PATH'S BUILDERS

`_build_service_graph` and `_dependency_answer` live in k8fy/agent.py and are
what the chat route (`_chat_route`) already uses. Reimplementing the prose here
would give two answers to the same question that drift apart, and the answer
carries the lower-bound caveat that keeps this data honest — the worst possible
thing to have two versions of. So Go's inferIntent and the chat router differ
only in HOW they decide the intent; once decided, one function composes the
answer for both entry points.

WHY IT RETURNS tier1

No model call means no tokens, no cost, and no prompt. Reporting that honestly
matters twice over: the cost rollups, and prompt provenance — see the tier1
guard in agent.py's `_with_system_prompt`.
"""

import logging
from typing import Any, Dict, Optional

from k8fy.agent import K8fyAgent, _build_service_graph, _dependency_answer
from models.response import AgentResponse

logger = logging.getLogger(__name__)


class DependencyGraphSkill(K8fyAgent):
    """Service-dependency reporter — deterministic, no Claude call."""

    def __init__(self) -> None:
        # `system_prompt` (not prompt_name/prompt_fallback) on purpose: it pins an
        # exact string and skips Langfuse entirely, which is right for a skill
        # that never reaches the model. Resolving a prompt here would be a
        # network call for text nothing reads.
        super().__init__(system_prompt="", tools=[])

    async def reason(
        self, intent: str, data: Dict[str, Any], context: Optional[Dict[str, Any]] = None
    ) -> AgentResponse:
        if context is None:
            context = {}

        # The question text is what resolves which service to focus. Go forwards
        # it in the context; fall back to `data` for callers that do not.
        question = context.get("question") or data.get("question") or ""
        messages = [{"role": "user", "content": str(question)}]

        try:
            graph = await _build_service_graph(messages, context, self.backend_url)
        except Exception as e:  # noqa: BLE001
            logger.warning("dependency skill: graph fetch failed: %s", e)
            graph = None

        namespace = context.get("namespace") or "this namespace"
        if not graph:
            # An honest empty answer beats a model guess here: the reasons a
            # namespace legitimately has no edges are known and finite, and
            # listing the top ones is more useful than speculation.
            return AgentResponse(
                answer=(
                    f"No service-dependency evidence has been mined for {namespace}.\n\n"
                    "That is expected rather than broken when nothing in the namespace logs a "
                    "callee's hostname — the graph is built from log text, not observed network "
                    "traffic. The usual reasons, in order of likelihood: the caller logs only a "
                    "path and never the host; the caller has no Kubernetes Service, so its calls "
                    "cannot be attributed; or the pod is multi-container, which currently returns "
                    "no logs at all.\n\n"
                    "See docs/SERVICE_DEPENDENCIES.md for the full list and how to verify."
                ),
                status="ok",
                confidence=1.0,
                sources=["service_dependencies"],
                tool_calls=[],
                details={},
                tier="tier1",
            )

        answer, details = _dependency_answer(graph)
        logger.info(
            "dependency skill answered deterministically: namespace=%s focus=%s edges=%d",
            graph["namespace"], graph.get("focus"), len(graph["dependencies"]),
        )
        return AgentResponse(
            answer=answer,
            status="ok",
            # Certainty about the evidence, not about completeness — the answer
            # text states the lower-bound caveat itself.
            confidence=1.0,
            sources=["service_dependencies"],
            tool_calls=[],
            details=details,
            tier="tier1",
        )
