"""inventory.py — push this cluster's active-namespace + service inventory
to the Hub (ADR 0022 / ROADMAP P18 use case #1, extended by ROADMAP P16 /
ADR 0023 to also carry per-namespace service names for the service->cluster
resolver, and ADR 0029 to also carry each service's selector for the
Glue-based dependency miner).

"Active" (decided in main.py's _namespace_services) means the namespace
has at least one Service, Deployment, StatefulSet, or DaemonSet — empty
namespaces the ServiceAccount can merely list are excluded, same spirit as
service_topology.py's known-services validation: report only namespaces that
mean something.

push_inventory mirrors service_topology.push_dependency's shape: same bearer
credential, same best-effort log-and-swallow-on-failure discipline — one
dropped scan cycle never blocks the next.
"""

import logging
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)


async def push_inventory(namespace_services: Dict[str, List[Dict[str, Any]]], backend_url: str, collector_token: str) -> None:
    """Push this cluster's full current namespace->services snapshot. The Hub
    overwrites Integration.Namespaces and the cluster_services registry with
    it (not a merge) — this is meant to reflect live cluster truth, so a
    decommissioned namespace or service should disappear on the next push,
    not linger. Each service in `namespace_services` is {"name": ...,
    "selector": {...}} — the Hub's serviceInventoryEntry also accepts a bare
    string for backward compatibility with an older Discovery build, but this
    (current) build always sends the richer shape.
    """
    payload = {
        "namespaces": [
            {"name": namespace, "services": services}
            for namespace, services in namespace_services.items()
        ]
    }
    # Omit the header entirely when unset — httpx raises a local (never-sent)
    # error on a "Bearer " value with an empty token. This endpoint requires
    # a valid CollectorToken (unlike /api/ingest), so an omitted header still
    # correctly gets a clean 401 from the Hub rather than a client-side crash.
    headers = {"Authorization": f"Bearer {collector_token}"} if collector_token else {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{backend_url.rstrip('/')}/api/cluster-inventory",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("push_inventory failed: %s", e)
