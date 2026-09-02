"""watch.py — continuous K8s watch streams for pods/services/deployments
(ADR 0027, merged from the retired src/adapters/k8fy adapter's watcher.py).

Runs as a third background task alongside main.py's scan-cycle loop and
live_relay.py's persistent connection (`asyncio.gather` in `_run_all`) — a
drop in one never affects the others. Each of the three watches reconnects
independently with the same capped-exponential-backoff discipline
live_relay.py established; never raises out of run_forever except
asyncio.CancelledError.

Cluster-wide only (no "pin to one namespace" option, unlike the retired
adapter) — matches every other scan in this package, which is already
cluster-wide with an exclude-list; running two different namespace-scoping
models in one merged component would be confusing, not simplifying.
Excluded namespaces are filtered per-event via cfg.namespace_exclude.
"""

import asyncio
import logging
from typing import Any, Dict

from . import k8s_client, normalize
from .service_index import INDEX
from .config import Config

logger = logging.getLogger("agentify.discovery.watch")

_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 30.0
_REVISION_ANNOTATION = "deployment.kubernetes.io/revision"


async def _watch_loop(name: str, path: str, cfg: Config, shutdown: asyncio.Event, handle_event) -> None:
    """Shared reconnect/backoff shell for one watch stream. `handle_event`
    is called with each `{"type", "object"}` frame; exceptions it raises
    propagate out to the same backoff handling as a connection failure,
    since a bad event is as good a reason to reconnect (fresh LIST) as a
    dropped socket."""
    backoff = _INITIAL_BACKOFF_SECONDS
    while not shutdown.is_set():
        try:
            async for event in k8s_client.watch_resource(path):
                obj = event.get("object", {})
                namespace = obj.get("metadata", {}).get("namespace", "")
                if namespace in cfg.namespace_exclude:
                    continue
                await handle_event(event.get("type", ""), obj, cfg)
                backoff = _INITIAL_BACKOFF_SECONDS
                if shutdown.is_set():
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("%s watch dropped (%s) — retrying in %.1fs", name, e, backoff)

        if shutdown.is_set():
            break
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass  # normal: reconnect now
        backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)


async def _handle_pod_event(event_type: str, pod: Dict[str, Any], cfg: Config) -> None:
    metadata = pod.get("metadata", {})
    service = INDEX.resolve(metadata.get("namespace", ""), metadata.get("labels") or {})
    await normalize.push_event(
        normalize.normalize_pod_event(pod, event_type, service), cfg.backend_url, cfg.collector_token
    )


async def _handle_service_event(event_type: str, svc: Dict[str, Any], cfg: Config) -> None:
    # Index BEFORE pushing: a Service's own ADDED event is the earliest moment
    # its selector is known, and pods in that namespace may already be queued
    # behind it on the other stream.
    INDEX.apply_service_event(event_type, svc)
    await normalize.push_event(normalize.normalize_service_event(svc, event_type), cfg.backend_url, cfg.collector_token)


def _make_deployment_handler():
    """Closure holding the revision-dedup state (so only genuine rollouts —
    a changed `deployment.kubernetes.io/revision` annotation — emit a
    change event, spec 007). State persists across this watch's own
    reconnects for the process lifetime, same as the retired adapter's
    per-instance dict."""
    deploy_revisions: Dict[str, str] = {}

    async def handle(event_type: str, dep: Dict[str, Any], cfg: Config) -> None:
        metadata = dep.get("metadata", {})
        key = f"{metadata.get('namespace', '')}/{metadata.get('name', '')}"
        if event_type == "DELETED":
            deploy_revisions.pop(key, None)
            return
        revision = (metadata.get("annotations") or {}).get(_REVISION_ANNOTATION)
        if not revision or deploy_revisions.get(key) == revision:
            return
        deploy_revisions[key] = revision
        # The pod TEMPLATE labels are what a Service selects on; a Deployment's
        # own metadata labels frequently differ.
        template_labels = (
            ((dep.get("spec", {}) or {}).get("template", {}) or {}).get("metadata", {}) or {}
        ).get("labels") or {}
        service = INDEX.resolve(metadata.get("namespace", ""), template_labels)
        await normalize.push_event(
            normalize.normalize_deploy_event(dep, revision, service), cfg.backend_url, cfg.collector_token
        )

    return handle


async def run_forever(cfg: Config, shutdown: asyncio.Event) -> None:
    """Runs the three watch streams concurrently until `shutdown` is set."""
    await asyncio.gather(
        _watch_loop("pods", "/api/v1/pods", cfg, shutdown, _handle_pod_event),
        _watch_loop("services", "/api/v1/services", cfg, shutdown, _handle_service_event),
        _watch_loop("deployments", "/apis/apps/v1/deployments", cfg, shutdown, _make_deployment_handler()),
    )
