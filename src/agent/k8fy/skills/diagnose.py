"""DiagnoseSkill — Pattern A: parallel multi-signal pre-fetch, one Opus call (spec 010).

Replaces the advisor/executor agentic loop with a deterministic pre-fetch of every
signal the diagnosis needs, followed by a single Opus call with all data assembled.

Pre-fetch sequence (all parallel via asyncio.gather):
  1. get_service_health(service_name, namespace)        — service-level status
  2. get_pod_events(pod_id, namespace)                  — for every pod in initial data
  3. get_metrics_history(namespace, service_name, asc)  — restart time-series
  4. get_change_history(namespace, service_name)        — deploy/rollout correlation
  5. get_logs(namespace, pod, previous=True)            — only for crashing pods
     (restarts >= _CRASH_RESTART_THRESHOLD or phase in _CRASH_PHASES)
  6. get_service_dependencies                           — the already-mined graph
     for this namespace (service_topology.py), so Claude can consider upstream/
     downstream services, not just the one being asked about
  7. live_list_pods per resolved fleet cluster (ROADMAP P16 / ADR 0023) — a
     live snapshot from every cluster resolve_service_clusters finds running
     this service, relayed through agentify-discovery's persistent
     connection (ROADMAP P18 use case #9). Empty for deployments with no
     registered fleet clusters — a no-op, not a behavior change.

After the fetch, up to _MAX_TOPOLOGY_PODS pods' logs (not conditioned on crash
state — routine logs mention routine downstream calls more than crash traces
do) are mined for service-dependency evidence (service_topology.py) and
recorded for next time. Best-effort, never blocks or fails the diagnosis.

Cost: (4 + crashing-pod-count + up to _MAX_TOPOLOGY_PODS) parallel backend
fetches + exactly 1 Opus call. Previously: advisor/executor loop with 2–7
tool iterations (unpredictable cost).
"""

import asyncio
import logging
from typing import Any, Dict, List

from k8fy.agent import ADVISOR_MODEL, DIAGNOSE_REASONING_SCHEMA, K8fyAgent
from k8fy.prompts import DIAGNOSE_PROMPT
from k8fy.service_topology import fetch_service_dependencies, mine_service_dependencies, resolve_service_clusters
from k8fy.tools import TOOLS
from models.response import AgentResponse

_DIAGNOSE_TOOLS = [
    t for t in TOOLS
    if t["name"] in {
        "get_service_health",
        "query_pod",
        "get_pod_events",
        "get_logs",
        "get_metrics_history",
        "get_change_history",
    }
]

logger = logging.getLogger(__name__)

_CRASH_RESTART_THRESHOLD = 3
_CRASH_PHASES = {"Failed", "Unknown", "CrashLoopBackOff"}

# P10 — Context budget: cap each signal source so the pre-fetched context
# never blows past the Claude context window. Values are approximate token
# limits for each source type. Truncation is most-recent-first.
_MAX_LOG_LINES      = 60    # ~1 500 tokens per pod log
_MAX_EVENT_ROWS     = 20    # ~600 tokens per pod events list
_MAX_METRICS_ROWS   = 50    # ~1 500 tokens for restart time-series
_MAX_CHANGE_ROWS    = 10    # ~800 tokens for deploy/rollout history
_MAX_SIMILAR        = 3     # how many past incidents to surface (each ~200 tokens)

# Service-topology mining (service_topology.py) — capped low since each pod
# costs one extra get_logs() call (1-3s when it hits Athena); this is a
# best-effort side channel, not a core signal, so it stays cheap.
_MAX_TOPOLOGY_PODS = 2


class DiagnoseSkill(K8fyAgent):
    """Failure-mode + causal correlation expert — Pattern A: parallel pre-fetch + one Opus call."""

    def __init__(self) -> None:
        super().__init__(
            prompt_name="k8fy/diagnose",
            prompt_fallback=DIAGNOSE_PROMPT,
            tools=_DIAGNOSE_TOOLS,
            output_schema=DIAGNOSE_REASONING_SCHEMA,
        )
        # Diagnosis warrants the most capable model; override the default.
        self.model = ADVISOR_MODEL  # claude-opus-4-8

    async def reason(
        self, intent: str, data: Dict[str, Any], context: Dict[str, Any] | None = None
    ) -> AgentResponse:
        if context is None:
            context = {}
        prefetched = await self._prefetch(data, context)
        return await self._reason_pattern_a(intent, data, context, prefetched)

    async def _prefetch(
        self, data: Dict[str, Any], context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Fire all predictable diagnostic tool calls in parallel before the Opus call."""
        namespace = context.get("namespace", "default")
        service_name = context.get("service_name") or context.get("service")
        tasks: Dict[str, Any] = {}

        # 1. Service-level health summary
        if service_name:
            tasks["service_health"] = self._fetch(
                "get_service_health",
                {"service_name": service_name, "namespace": namespace},
            )

        # 2. Pod events — for every pod present in the initial data
        for pod_id in _all_pod_ids(data):
            tasks[f"events.{pod_id}"] = self._fetch(
                "get_pod_events",
                {"pod_id": pod_id, "namespace": namespace},
            )

        # 3. Restart time-series — always, asc so Claude can read the trend
        metrics_args: Dict[str, Any] = {"namespace": namespace, "order": "asc"}
        if service_name:
            metrics_args["service_name"] = service_name
        tasks["metrics_history"] = self._fetch("get_metrics_history", metrics_args)

        # 4. Deploy/rollout history — always, for symptom-onset correlation
        change_args: Dict[str, Any] = {"namespace": namespace}
        if service_name:
            change_args["service_name"] = service_name
        tasks["change_history"] = self._fetch("get_change_history", change_args)

        # 5. Crash logs — only for pods that look like they are crashing.
        #    Use the live pod IDs from service_health when available: the registry
        #    snapshot in `data` can be up to 30 s stale after a rollout, causing 404s.
        #    service_health is fetched above and reflects the current K8s state.
        #    Fall back to pod IDs from `data` if service_health hasn't landed yet.
        #    get_logs reads the Glue/Athena test harness first when
        #    configured, falling back to the live cluster — still a
        #    deterministic function call, not an LLM decision (log_router.py).
        log_pod_ids = _crashing_pod_ids(data)
        for pod_id in log_pod_ids:
            tasks[f"logs.{pod_id}"] = self._fetch(
                "get_logs",
                {"namespace": namespace, "pod": pod_id, "previous": True},
            )

        # 6. Semantic memory: similar past incidents (P8).
        #    Retrieves the top-3 most relevant prior diagnoses so Claude can
        #    pattern-match against historical root causes. Non-blocking — fails
        #    silently when no embeddings exist yet (fresh deployment).
        if service_name:
            tasks["similar_incidents"] = self._fetch(
                "get_similar_incidents",
                {
                    "namespace":   namespace,
                    "service":     service_name,
                    "description": f"service health issues in {namespace}/{service_name}",
                    "limit":       3,
                },
            )

        # 7. Service-dependency graph — the already-mined edges for this
        #    namespace (service_topology.py), so Claude can consider upstream/
        #    downstream services, not just the one being asked about.
        tasks["service_dependencies"] = fetch_service_dependencies(namespace, self.backend_url)

        # 7b. Fleet-cluster scoping (ROADMAP P16 / ADR 0023, extended to the
        #     ingested-data path by ADR 0024): resolve which cluster(s) run
        #     this service via the Hub's cluster_services registry
        #     (populated by agentify-discovery's inventory push), then:
        #       - prefetch a LIVE snapshot from each match through the
        #         persistent-connection relay (ROADMAP P18 use case #9), and
        #       - ALSO prefetch a cluster-scoped get_service_health (ADR
        #         0024's ingested-data cluster scoping) — the ingested store
        #         may already have per-cluster data pushed by that cluster's
        #         own agentify-discovery watch stream (ADR 0027), and it's
        #         cheap to check.
        #     Both go through the same self._fetch()/process_tool_call path
        #     every other tool here uses — cluster_id in the args is all
        #     _dispatch_live_diagnostic / HandleAgentFetch need to route
        #     either remotely or to the right shard. correlation.md:
        #     diagnostic intent fans out across every matching signal and
        #     lets Tier-2 synthesize/surface disagreement, so EVERY resolved
        #     cluster gets tasks, not just one. For deployments with no
        #     registered fleet clusters (the common case today), resolution
        #     returns [] and this whole step is a no-op — the unscoped
        #     "service_health" task above already covers that case unchanged.
        if service_name:
            for cluster_id in await resolve_service_clusters(namespace, service_name, self.backend_url):
                tasks[f"live_pods.{cluster_id}"] = self._fetch(
                    "live_list_pods", {"namespace": namespace, "cluster_id": cluster_id},
                )
                tasks[f"service_health.{cluster_id}"] = self._fetch(
                    "get_service_health",
                    {"service_name": service_name, "namespace": namespace, "cluster_id": cluster_id},
                )

        # 8. Topology-mining log fetch — a few GENERAL (not crash-only) pods'
        #    logs, since routine operation logs mention routine downstream
        #    calls more than crash traces do. Mined for service-dependency
        #    evidence below, after the gather resolves.
        topology_pod_ids = _all_pod_ids(data)[:_MAX_TOPOLOGY_PODS]
        for pod_id in topology_pod_ids:
            tasks[f"topology_logs.{pod_id}"] = self._fetch(
                "get_logs",
                {"namespace": namespace, "pod": pod_id},
            )

        if not tasks:
            return {}

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        prefetched: Dict[str, Any] = {}
        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("diagnose prefetch failed for %s: %s", key, result)
            else:
                prefetched[key] = result

        # Mine the just-fetched topology logs for service-dependency evidence.
        # Best-effort side channel — mine_service_dependencies() never raises,
        # so this never affects the diagnosis itself.
        if service_name:
            for pod_id in topology_pod_ids:
                log_result = prefetched.get(f"topology_logs.{pod_id}")
                log_text = log_result.get("logs", "") if isinstance(log_result, dict) else ""
                if log_text:
                    await mine_service_dependencies(namespace, service_name, log_text, self.backend_url)

        return prefetched


def _all_pod_ids(data: Dict[str, Any]) -> List[str]:
    """Return keys whose values look like pod-state dicts (have phase or ready)."""
    return [
        k for k, v in data.items()
        if isinstance(v, dict) and ("phase" in v or "ready" in v)
    ]


def _crashing_pod_ids(data: Dict[str, Any]) -> List[str]:
    """Return pods worth fetching crash logs for.

    Includes pods that:
    - Have restarted >= threshold (catches CrashLoopBackOff which shows as Running)
    - Are in a terminal failure phase
    - Are explicitly marked as not-ready (degraded pods may have useful logs)

    Excludes completed/old pods — they've already exited cleanly.
    """
    ids = []
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        phase = val.get("phase", "")
        restarts = val.get("restarts", 0)
        ready = val.get("ready", True)
        completed = val.get("completed", False)
        if completed:
            continue
        if (
            restarts >= _CRASH_RESTART_THRESHOLD
            or phase in _CRASH_PHASES
            or (not ready and restarts > 0)
        ):
            ids.append(key)
    return ids
