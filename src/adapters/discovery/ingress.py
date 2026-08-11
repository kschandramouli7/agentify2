"""ingress.py — push this cluster's entry-point mapping (Ingress, Gateway
API's Gateway+HTTPRoute, OpenShift Route) to the Hub (ROADMAP P18 use case
#3, ADR 0022's "Ingress/Gateway-API/Route-agnostic entry-point mapping"
constraint).

Mirrors inventory.py's push_inventory shape: same bearer credential, same
best-effort log-and-swallow-on-failure discipline — one dropped scan cycle
never blocks the next.

Every build_*_entries function flattens a K8s object's host(s)/backend(s)
into one row per (host, backend_service) pair — a deliberate simplification
for Ingress specifically: an Ingress's per-rule host->backend pairing isn't
preserved past k8s_client.list_ingresses' dedup, so a multi-rule Ingress
with N hosts and M distinct backends produces the cross product (N*M rows),
not the exact per-rule mapping. Acceptable for an entry-point *map*
(which hosts/services participate) rather than a precise routing table.
"""

import logging
from typing import Any, Dict, List, Tuple

import httpx

logger = logging.getLogger(__name__)


def build_ingress_entries(namespace: str, ingresses: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Flatten list_ingresses' output into one entry per (host, backend)
    pair. Neither side being empty still produces a recorded entry (empty
    string), same "still useful without a fully-resolved mapping" principle
    the Gateway/HTTPRoute correlation below uses."""
    entries = []
    for ing in ingresses:
        hosts = ing["hosts"] or [""]
        backends = ing["backend_services"] or [""]
        for host in hosts:
            for backend in backends:
                entries.append({
                    "namespace": namespace, "kind": "ingress", "name": ing["name"],
                    "host": host, "backend_service": backend,
                })
    return entries


def build_route_entries(namespace: str, routes: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Flatten list_routes' output (already one host/backend per Route) into
    the same entry shape as the other two build_*/correlate_* functions."""
    return [
        {"namespace": namespace, "kind": "route", "name": r["name"], "host": r["host"], "backend_service": r["backend_service"]}
        for r in routes
    ]


def correlate_gateway_routes(
    namespace: str,
    httproutes: List[Dict[str, Any]],
    gateways_by_key: Dict[Tuple[str, str], Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Join this namespace's HTTPRoutes to their parent Gateway(s)'
    listener hostnames, gateways_by_key keyed by (namespace, name) so a
    cross-namespace parentRef still resolves (build gateways_by_key from
    every scanned namespace's list_gateways up front, not just this one).

    A parentRef with no matching Gateway (not found in gateways_by_key —
    dangling ref, or that Gateway simply wasn't listed) is silently skipped
    for that ref only; the route's own hostnames still contribute.
    sectionName filters to that one listener when set; otherwise every
    listener on the matched Gateway contributes its hostname.
    """
    entries = []
    for route in httproutes:
        listener_hostnames: List[str] = []
        for ref in route["parent_refs"]:
            gw = gateways_by_key.get((ref["namespace"], ref["name"]))
            if gw is None:
                continue
            for listener in gw["listeners"]:
                if ref["section_name"] and listener["name"] != ref["section_name"]:
                    continue
                hostname = listener.get("hostname", "")
                if hostname and hostname not in listener_hostnames:
                    listener_hostnames.append(hostname)

        hosts: List[str] = []
        for h in listener_hostnames + route["hostnames"]:
            if h not in hosts:
                hosts.append(h)
        if not hosts:
            hosts = [""]

        backends = route["backend_services"] or [""]
        for host in hosts:
            for backend in backends:
                entries.append({
                    "namespace": namespace, "kind": "httproute", "name": route["name"],
                    "host": host, "backend_service": backend,
                })
    return entries


async def push_ingress(entries: List[Dict[str, str]], backend_url: str, collector_token: str) -> None:
    """Push this cluster's full current entry-point snapshot. Same
    full-replace-per-push semantics as push_inventory: the Hub overwrites
    cluster_ingress_endpoints for this (tenant, cluster) entirely, so a
    removed Ingress/Route disappears on the next push, not linger."""
    payload = {"entries": entries}
    # Omit the header entirely when unset — see push_inventory's identical
    # comment (inventory.py) for why.
    headers = {"Authorization": f"Bearer {collector_token}"} if collector_token else {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{backend_url.rstrip('/')}/api/cluster-ingress",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("push_ingress failed: %s", e)
