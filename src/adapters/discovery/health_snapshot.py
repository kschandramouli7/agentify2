"""health_snapshot.py — push this cluster's fleet-wide health/version
snapshot to the Hub (ROADMAP P18 use case #5).

Mirrors inventory.py's push_inventory shape: same bearer credential, same
best-effort log-and-swallow-on-failure discipline — one dropped scan cycle
never blocks the next. Capacity (node count/allocatable CPU-mem) is
deliberately not part of this snapshot — it needs a new `nodes` RBAC grant
Discovery has never had, flagged as a follow-up, not built here.
"""

import logging

import httpx

logger = logging.getLogger(__name__)


async def push_health(k8s_version: str, pods_total: int, pods_ready: int, backend_url: str, collector_token: str) -> None:
    """Push this cluster's current snapshot. The Hub overwrites this
    cluster's single cluster_health_snapshots row in place (not a set of
    rows) — always reflects the most recent scan cycle, not history."""
    payload = {"k8s_version": k8s_version, "pods_total": pods_total, "pods_ready": pods_ready}
    # Omit the header entirely when unset — see push_inventory's identical
    # comment (inventory.py) for why.
    headers = {"Authorization": f"Bearer {collector_token}"} if collector_token else {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{backend_url.rstrip('/')}/api/cluster-health",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("push_health failed: %s", e)
