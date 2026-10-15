"""LabelScopedDependencySkill — handles the `label_scoped_dependencies`
intent (ROADMAP P32, ADR 0043).

Deterministic only — zero Claude calls, same posture DependencyGraphSkill's
own module docstring already argues for the plain `dependencies` intent:
"which services carry this label" and "the graph scoped to them" are both
facts with one correct answer, already sitting in Postgres/the Hub's own
endpoints.

WHY THIS REUSES THE CHAT PATH'S BUILDERS

`_resolve_label_query`, `_build_label_scoped_graph` and `_label_scoped_answer`
live in k8fy/agent.py and are what the chat route (`_chat_route`) already
uses — same "one function composes the answer for both entry points" reasoning
DependencyGraphSkill's own docstring gives for the plain dependencies intent.
"""

import logging
from typing import Any, Dict, Optional

from k8fy.agent import K8fyAgent, _build_label_scoped_graph, _label_scoped_answer, _resolve_label_query
from models.response import AgentResponse

logger = logging.getLogger(__name__)


class LabelScopedDependencySkill(K8fyAgent):
    """Label-scoped dependency reporter — deterministic, no Claude call."""

    def __init__(self) -> None:
        super().__init__(system_prompt="", tools=[])

    async def reason(
        self, intent: str, data: Dict[str, Any], context: Optional[Dict[str, Any]] = None
    ) -> AgentResponse:
        if context is None:
            context = {}

        question = str(context.get("question") or data.get("question") or "")
        resolved = _resolve_label_query(question)
        if resolved is None:
            # Can genuinely happen: Go's labelScopedDependencyRE matches a
            # bare APM-shaped token unconditionally (it has no access to
            # the agent's own alias config), while this resolution DOES
            # require settings.label_key_aliases to map "apm" to a real
            # key. An operator who never configured that alias lands here
            # rather than silently answering nothing.
            return AgentResponse(
                answer=(
                    "I spotted what looks like a label-scoped reference, but couldn't "
                    "resolve it to a tracked label key. If you meant an explicit label, "
                    "use the form key=value (e.g. team=checkout)."
                ),
                status="ok", confidence=1.0, sources=[], tool_calls=[], details={}, tier="tier1",
            )

        label_key, label_value = resolved
        try:
            graph = await _build_label_scoped_graph(label_key, label_value, self.backend_url)
        except Exception as e:  # noqa: BLE001
            logger.warning("label-scoped dependency skill: graph fetch failed: %s", e)
            return AgentResponse(
                answer="The label-scoped dependency lookup is temporarily unavailable.",
                status="error", confidence=0.0, sources=[], tool_calls=[], details={}, tier="tier1",
            )

        answer, details = _label_scoped_answer(graph)
        logger.info(
            "label-scoped dependency skill answered deterministically: key=%s tracked=%s services=%d",
            label_key, graph["tracked"], len(graph.get("services") or []),
        )
        return AgentResponse(
            answer=answer,
            status="ok",
            confidence=1.0,
            sources=["service_labels", "service_dependencies"],
            tool_calls=[],
            details=details,
            tier="tier1",
        )
