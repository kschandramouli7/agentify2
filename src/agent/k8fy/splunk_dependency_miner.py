"""splunk_dependency_miner.py — mine service-to-service dependency edges
from a customer's Splunk instance, continuously (ROADMAP P15, ADR 0042).

Complements, not replaces, Discovery's live per-cluster mining and the
Glue/Athena miner (dependency_miner.py) — this is a THIRD producer of the
same service_dependencies evidence, tagged source="splunk". Reuses
extract_service_calls (the actual "does this log mention service X, on
what port, with what outcome" logic) verbatim, and the same cluster-
registry/selector-fetch/push helpers dependency_miner.py already built —
this miner only supplies a different DATA SOURCE, not new extraction or
push logic (ADR 0029's "the logic doesn't get rebuilt, just re-hosted"
principle, extended to a third host).

Runs as a periodic background task (app.py's startup event), on its own
interval (SPLUNK_MINING_INTERVAL_SECONDS) — independent of the Glue
miner's, since Splunk has no Firehose-buffering-interval equivalent floor
(see ADR 0042's interval discussion).

Schema assumption, unverified against a real instance (ADR 0042, stacking
on ADR 0041's own pod/container-name assumption): pod labels arrive as
flat dotted fields, kubernetes.labels.<key> — needed here because label-
to-Service-selector matching is how from_service gets resolved at all.
Check this first against real data before trusting any edge this produces.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from k8fy.dependency_miner import _fetch_registered_clusters, _fetch_selectors, _push_edge, _service_for_labels
from k8fy.log_platform_splunk import _run_query
from k8fy.service_topology import extract_service_calls

logger = logging.getLogger(__name__)

_MAX_ROWS = 500  # matches dependency_miner.py's own cap, not log_platform_splunk.py's 200 (a different use case)
_LABEL_PREFIX = "kubernetes.labels."


def _build_namespace_spl(index: str, namespace: str, limit: int) -> str:
    """One broad search per namespace per cycle, not one per pod — matches
    dependency_miner.py's own _mine_namespace unit of work (query once,
    group by pod_name after fetch), not log_platform_splunk.py's single-pod
    on-demand read shape. No pod filter: this fetches every pod's events in
    the namespace in one search."""
    return f'search index={index} kubernetes.namespace_name="{namespace}" | head {limit}'


def _extract_labels(row: Dict[str, Any]) -> Dict[str, str]:
    """Pod labels from a Splunk result row's dotted kubernetes.labels.<key>
    fields — the schema assumption named in this module's own docstring."""
    return {
        k[len(_LABEL_PREFIX):]: v
        for k, v in row.items()
        if k.startswith(_LABEL_PREFIX) and isinstance(v, str)
    }


async def _mine_namespace(
    backend_url: str, splunk_config: Dict[str, Any], cluster_id: str, namespace: str, hours_back: int,
) -> None:
    """Mine one (cluster, namespace)'s recent Splunk events for service-
    dependency edges. Same per-pod dedup/push shape dependency_miner.py's
    own _mine_namespace already uses (see that function's docstring for the
    full reasoning on dedup keys and outcome/match_kind merge rules) —
    only the fetch layer differs."""
    selectors = await _fetch_selectors(backend_url, cluster_id, namespace)
    if not selectors:
        return  # no known services (or the fetch failed) — nothing to attribute logs to

    known_services: Set[str] = set(selectors.keys())
    splunk_url = splunk_config.get("url", "")
    index = splunk_config.get("index", "")
    token = splunk_config.get("token", "")
    verify_tls = splunk_config.get("verify_tls", True)
    spl = _build_namespace_spl(index, namespace, _MAX_ROWS)

    try:
        async with httpx.AsyncClient(verify=verify_tls, timeout=15.0) as client:
            result = await _run_query(client, splunk_url, token, spl, hours_back, result_limit=_MAX_ROWS)
    except httpx.HTTPError as e:
        logger.warning("splunk_dependency_miner: query failed for cluster=%s namespace=%s: %s", cluster_id, namespace, e)
        return
    if "error" in result:
        logger.warning("splunk_dependency_miner: %s (cluster=%s namespace=%s)", result["error"], cluster_id, namespace)
        return

    # Group each pod's events together so extract_service_calls sees that
    # pod's full recently-landed text at once — matches dependency_miner.py's
    # own by_pod/pod_labels grouping exactly.
    by_pod: Dict[str, List[str]] = {}
    pod_labels: Dict[str, Dict[str, str]] = {}
    for row in result.get("rows", []):
        pod_name = row.get("kubernetes.pod_name") or row.get("pod_name") or ""
        if not pod_name:
            continue
        pod_labels.setdefault(pod_name, _extract_labels(row))
        raw = row.get("_raw") or row.get("log") or ""
        by_pod.setdefault(pod_name, []).append(raw)

    pushed: Set[Tuple[str, str, int, str, str]] = set()
    for pod_name, lines in by_pod.items():
        from_service = _service_for_labels(pod_labels.get(pod_name, {}), selectors)
        if not from_service:
            continue
        log_text = "\n".join(lines)

        # Same "last known outcome wins, qualified match sticks" merge rules
        # as dependency_miner.py's own _mine_namespace — see that function
        # for the full reasoning, not duplicated here.
        last_state: Dict[Tuple[str, int, str], Tuple[Optional[str], str]] = {}
        for obs in extract_service_calls(log_text, namespace, known_services):
            if obs.service == from_service:
                continue
            key = (obs.service, obs.port or 0, obs.path)
            prev_outcome, prev_match_kind = last_state.get(key, (None, ""))
            outcome = obs.outcome if obs.outcome is not None else prev_outcome
            match_kind = "qualified" if "qualified" in (prev_match_kind, obs.match_kind) else obs.match_kind
            last_state[key] = (outcome, match_kind)

        for (to_service, port, path), (outcome, match_kind) in last_state.items():
            edge = (from_service, to_service, port, path, pod_name)
            if edge in pushed:
                continue
            pushed.add(edge)
            await _push_edge(
                backend_url, cluster_id, namespace, from_service, to_service,
                port=port, outcome=outcome, path=path, caller_pod=pod_name,
                match_kind=match_kind, source="splunk",
            )


async def run_once(backend_url: str, splunk_config: Dict[str, Any], hours_back: int = 2) -> None:
    """One full mining cycle across every registered fleet cluster's
    namespaces. Never raises — a failure mining one (cluster, namespace)
    never blocks the others (same discipline as dependency_miner.py's own
    run_once / agentify-discovery's _scan_once)."""
    if not (splunk_config.get("url") and splunk_config.get("index") and splunk_config.get("token")):
        logger.info("splunk_dependency_miner: Splunk not configured, skipping this cycle")
        return

    clusters = await _fetch_registered_clusters(backend_url)
    for cluster in clusters:
        cluster_id = cluster.get("id", "")
        if not cluster_id:
            continue
        for namespace in cluster.get("namespaces") or []:
            try:
                await _mine_namespace(backend_url, splunk_config, cluster_id, namespace, hours_back)
            except Exception:
                logger.exception("splunk_dependency_miner: mining failed for cluster=%s namespace=%s", cluster_id, namespace)


async def run_forever(
    backend_url: str, splunk_config: Dict[str, Any], interval_seconds: int, shutdown: asyncio.Event,
) -> None:
    """Periodic background task — same shape as dependency_miner.py's own
    run_forever (and ultimately agentify-discovery's main.py::_run): run
    once, then wait up to interval_seconds for shutdown before the next
    cycle. Never raises out of the loop; a failed cycle just tries again
    next time."""
    while not shutdown.is_set():
        try:
            await run_once(backend_url, splunk_config)
        except Exception:
            logger.exception("splunk_dependency_miner: mining cycle failed")
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass  # normal: next cycle starts
