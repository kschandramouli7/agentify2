"""Tool definitions for the K8fy agent (Claude can call these)."""

import asyncio
import logging
import os
from typing import Any, Dict, List

import httpx

from k8fy.live_diagnostics import LIVE_DIAGNOSTIC_TOOLS
from k8fy import live_diagnostics
from k8fy.log_router import get_logs as _get_logs
from k8fy.service_topology import fetch_service_dependencies as _fetch_service_dependencies

logger = logging.getLogger(__name__)

# Vault connectivity (injected via env vars in the agent deployment).
# If VAULT_ADDR is not set the Vault tools return a graceful "not configured" message.
_VAULT_ADDR = os.environ.get("VAULT_ADDR", "")
_VAULT_TOKEN = os.environ.get("VAULT_TOKEN", "")

# Shared input_schema property for the four live_* tools (ROADMAP P18 use
# case #9): omit for the agent's own cluster (today's unchanged behavior);
# set to target another of the tenant's fleet clusters, relayed through that
# cluster's agentify-discovery collector. This tool does NOT resolve "which
# cluster is service X in" — that's P16 (multi-cluster connector), not
# built — the caller must already know the id, listed via GET /admin/integrations.
CLUSTER_ID_PROPERTY = {
    "type": "string",
    "description": (
        "Optional: target a specific fleet cluster by its Integration id "
        "(see GET /admin/integrations) instead of the agent's own cluster. "
        "Omit for the local cluster."
    ),
}

# Tool schema for Claude to understand what it can call
TOOLS = [
    {
        "name": "query_pod",
        "description": "Query details about a specific pod: phase, ready status, restart count, recent events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pod_id": {"type": "string", "description": "Pod identifier"},
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
            },
            "required": ["pod_id", "namespace"],
        },
    },
    {
        "name": "get_service_health",
        "description": "Get health status of a service: endpoints, ready ratio, pod statuses.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service name"},
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
            },
            "required": ["service_name", "namespace"],
        },
    },
    {
        "name": "get_certificates",
        "description": "Get certificate status: list of certs, expiry dates, renewal needs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace (optional, all if not provided)",
                },
            },
        },
    },
    {
        "name": "get_pod_events",
        "description": "Get recent events for a pod: restarts, crashes, warnings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pod_id": {"type": "string", "description": "Pod identifier"},
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "limit": {"type": "integer", "description": "Number of recent events to return"},
            },
            "required": ["pod_id", "namespace"],
        },
    },
    {
        "name": "get_change_history",
        "description": (
            "Get recent deployment/change events (rollouts) for a service over a "
            "time window. Use this during diagnosis to see WHAT CHANGED before a "
            "symptom began — a rollout shortly before restarts started is a likely "
            "trigger to investigate (correlation, not proof)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "deployment": {"type": "string", "description": "Deployment/service name to filter to."},
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "since": {"type": "string", "description": "RFC3339 start of the window (optional)."},
                "until": {"type": "string", "description": "RFC3339 end of the window (optional)."},
            },
            "required": ["namespace"],
        },
    },
    {
        "name": "get_logs",
        "description": (
            "PREFERRED way to fetch logs for a pod to find the CRASH REASON "
            "(OOMKilled, panic/stack trace, connection refused, failing probe). "
            "Automatically tries the log platform (Glue/Athena) first when configured "
            "and falls back to the live cluster — you never need to decide which; call "
            "this instead of live_get_pod_logs unless you specifically need a live "
            "snapshot. Set previous=true to read the last crashed container instance "
            "when the live cluster answers (ignored when the log platform answers, "
            "which retains history itself). Logs are best-effort redacted and not "
            "stored."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "pod": {"type": "string", "description": "Pod name"},
                "container": {"type": "string", "description": "Container name (optional; live-cluster path only)."},
                "previous": {"type": "boolean", "description": "Read the previous (crashed) container instance (live-cluster path only)."},
                "tail_lines": {"type": "integer", "description": "Lines from the end (default 200, capped server-side)."},
            },
            "required": ["namespace", "pod"],
        },
    },
    {
        "name": "get_metrics_history",
        "description": (
            "Get the restart-count time-series for a pod over a time window — to "
            "see WHEN restarts started climbing (the temporal trend), not just the "
            "current count. Use this when diagnosing to find when a problem began. "
            "Samples are cumulative restart counts; a rising series means restarts "
            "happened in that window."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod_id": {"type": "string", "description": "Pod identifier to filter the series to."},
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "since": {"type": "string", "description": "RFC3339 start of the window (optional)."},
                "until": {"type": "string", "description": "RFC3339 end of the window (optional)."},
                "order": {"type": "string", "enum": ["asc", "desc"], "description": "Chronological (asc) or recent-first (desc). Use asc to read a trend."},
                "limit": {"type": "integer", "description": "Max samples (default 100)."},
            },
            "required": ["pod_id", "namespace"],
        },
    },
    # ── Vault tools (requires VAULT_ADDR + VAULT_TOKEN env vars) ─────────────
    {
        "name": "get_vault_cert_status",
        "description": (
            "Check the TLS certificate managed by HashiCorp Vault PKI for a given role. "
            "Returns expiry date, days remaining, serial number, and whether rotation is recommended. "
            "Use when the user asks about Vault-managed certs, SSL expiry from Vault, or cert health."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pki_role": {
                    "type": "string",
                    "description": "Vault PKI role name (e.g. 'payment-service').",
                },
                "kv_path": {
                    "type": "string",
                    "description": "Vault KV path where the cert is stored (e.g. 'secret/data/payments/tls'). Optional.",
                },
            },
            "required": ["pki_role"],
        },
    },
    {
        "name": "rotate_vault_cert",
        "description": (
            "Request a new TLS certificate from HashiCorp Vault PKI, store in Vault KV, "
            "and update the Kubernetes TLS Secret so it takes effect immediately. "
            "Only call this when expiry is imminent or when explicitly requested by the operator."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pki_mount": {
                    "type": "string",
                    "description": "Vault PKI mount (e.g. 'pki-payments'). Defaults to 'pki-payments'.",
                    "default": "pki-payments",
                },
                "pki_role": {
                    "type": "string",
                    "description": "Vault PKI role name to issue from (e.g. 'payment-api').",
                },
                "common_name": {
                    "type": "string",
                    "description": "Common name for the new cert (e.g. 'payment.payments.svc.cluster.local').",
                },
                "ttl": {
                    "type": "string",
                    "description": "Desired cert TTL (e.g. '24h'). Defaults to 24h.",
                    "default": "24h",
                },
                "k8s_secret_name": {
                    "type": "string",
                    "description": "K8s TLS Secret to update with the new cert (e.g. 'payment-tls').",
                },
                "k8s_namespace": {
                    "type": "string",
                    "description": "Namespace of the K8s Secret (e.g. 'payments').",
                },
            },
            "required": ["pki_role", "common_name"],
        },
    },
    # ── Live diagnostics tools (read-only, calls the live K8s API — not the
    #    ingested/cached store the tools above read from) ──────────────────
    {
        "name": "live_list_pods",
        "description": (
            "List pods in a namespace with a LIVE status snapshot (phase, ready, "
            "restart count, node) fetched from the Kubernetes API right now — use "
            "when cached data might be stale and you need the current state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "cluster_id": CLUSTER_ID_PROPERTY,
            },
            "required": ["namespace"],
        },
    },
    {
        "name": "live_get_pod_logs",
        "description": (
            "Fetch a LIVE, bounded, redacted tail of a pod's current logs directly "
            "from the Kubernetes API. Set previous=true for the last crashed "
            "container instance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "pod": {"type": "string", "description": "Pod name"},
                "container": {"type": "string", "description": "Container name (optional)."},
                "tail_lines": {"type": "integer", "description": "Lines from the end (default 200, capped at 1000)."},
                "previous": {"type": "boolean", "description": "Read the previous (crashed) container instance."},
                "cluster_id": CLUSTER_ID_PROPERTY,
            },
            "required": ["namespace", "pod"],
        },
    },
    {
        "name": "live_get_events",
        "description": "List recent LIVE Kubernetes events in a namespace, optionally filtered to one pod.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "pod": {"type": "string", "description": "Pod name to filter events to (optional)."},
                "cluster_id": CLUSTER_ID_PROPERTY,
            },
            "required": ["namespace"],
        },
    },
    {
        "name": "live_describe_pod",
        "description": "LIVE equivalent of `kubectl describe pod` — spec/status summary plus recent events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "pod": {"type": "string", "description": "Pod name"},
                "cluster_id": CLUSTER_ID_PROPERTY,
            },
            "required": ["namespace", "pod"],
        },
    },
    {
        "name": "live_get_certificates",
        "description": (
            "LIVE TLS certificate expiry check for a namespace — reads kubernetes.io/tls "
            "Secrets directly from the cluster and returns parsed expiry metadata only "
            "(never raw cert/key material). Unlike get_certificates (the ingested-store "
            "snapshot), this ALWAYS requires cluster_id — there is no local/current-agent "
            "implementation, only the fleet-relay path (ROADMAP P18 use case #9)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "cluster_id": CLUSTER_ID_PROPERTY,
            },
            "required": ["namespace", "cluster_id"],
        },
    },
    # ── Semantic memory tool (P8) ─────────────────────────────────────────────
    {
        "name": "get_similar_incidents",
        "description": (
            "Retrieve past incidents that are semantically similar to the current one. "
            "Returns a list of prior diagnoses with their headlines, likely causes, and "
            "resolution notes. Use at the start of a diagnose task to surface patterns "
            "from historical incidents and provide higher-confidence root-cause analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace to scope the search (e.g. 'payments').",
                },
                "service": {
                    "type": "string",
                    "description": "Service name to scope the search (e.g. 'payment-worker').",
                },
                "description": {
                    "type": "string",
                    "description": "Short description of the current incident used as the similarity query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of similar incidents to return (default 3).",
                },
            },
            "required": ["namespace", "service", "description"],
        },
    },
    # ── Service topology (mined from logs, see service_topology.py) ──────────
    {
        "name": "get_service_dependencies",
        "description": (
            "Get the known service-call graph for a namespace — which services "
            "have been observed (via log text) calling which other services. "
            "Use this when a service's own signals don't fully explain a symptom: "
            "check for upstream services that might be causing it, or downstream "
            "services that might be the actual root cause. Best-effort and often "
            "sparse — an empty result means no evidence has been mined yet, not "
            "that the service has no dependencies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace (e.g. 'payments')."},
            },
            "required": ["namespace"],
        },
    },
]

# ── Vault tool implementations ────────────────────────────────────────────────

async def _vault_get_cert_status(pki_role: str, kv_path: str = "") -> Dict[str, Any]:
    """Read cert metadata from Vault KV and compute expiry."""
    if not _VAULT_ADDR:
        return {"error": "VAULT_ADDR not configured — Vault tools are unavailable in this environment."}

    headers = {"X-Vault-Token": _VAULT_TOKEN} if _VAULT_TOKEN else {}
    result: Dict[str, Any] = {"pki_role": pki_role, "vault_addr": _VAULT_ADDR}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            # Try KV path first if provided.
            if kv_path:
                resp = await client.get(f"{_VAULT_ADDR}/v1/{kv_path}", headers=headers)
                if resp.status_code == 200:
                    data = resp.json().get("data", {}).get("data", {})
                    cert_pem = data.get("certificate", "")
                    result["kv_path"] = kv_path
                    result["renewed_at"] = data.get("renewed_at")
                    result["serial"] = data.get("serial")
                    if cert_pem:
                        # Parse expiry via openssl-compatible approach using stdlib.
                        import ssl, datetime
                        try:
                            import subprocess, tempfile
                            with tempfile.NamedTemporaryFile(suffix=".pem", mode="w", delete=False) as f:
                                f.write(cert_pem)
                                tmp = f.name
                            out = subprocess.run(
                                ["openssl", "x509", "-enddate", "-noout", "-in", tmp],
                                capture_output=True, text=True,
                            ).stdout.strip()
                            date_str = out.split("=", 1)[1]
                            expiry = datetime.datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z")
                            days = (expiry - datetime.datetime.utcnow()).days
                            result["expiry"] = expiry.strftime("%-d %b %Y, %H:%M UTC")
                            result["days_remaining"] = days
                            result["rotation_recommended"] = days < 30
                            result["status"] = "critical" if days < 7 else "warning" if days < 30 else "healthy"
                        except Exception:
                            result["cert_parse_error"] = "openssl not available — install in agent image"

            # Always check Vault PKI CA expiry.
            ca_resp = await client.get(f"{_VAULT_ADDR}/v1/pki/cert/ca", headers=headers)
            if ca_resp.status_code == 200:
                result["pki_ca_serial"] = ca_resp.json().get("data", {}).get("serial_number")

    except httpx.HTTPError as e:
        result["error"] = f"Vault unreachable: {e}"

    return result


async def _vault_rotate_cert(
    pki_role: str,
    common_name: str,
    ttl: str = "24h",
    pki_mount: str = "pki-payments",
    k8s_secret_name: str = "",
    k8s_namespace: str = "",
) -> Dict[str, Any]:
    """Issue a new cert from Vault PKI and update the K8s TLS Secret in-place."""
    if not _VAULT_ADDR:
        return {"error": "VAULT_ADDR not configured — Vault tools are unavailable."}

    headers = {"X-Vault-Token": _VAULT_TOKEN, "Content-Type": "application/json"} if _VAULT_TOKEN else {}
    import base64, datetime

    try:
        async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
            # 1. Issue cert from Vault PKI.
            resp = await client.post(
                f"{_VAULT_ADDR}/v1/{pki_mount}/issue/{pki_role}",
                headers=headers,
                json={"common_name": common_name, "ttl": ttl},
            )
            if resp.status_code != 200:
                return {"error": f"Vault PKI issue failed ({resp.status_code}): {resp.text}"}
            data = resp.json().get("data", {})
            serial = data.get("serial_number", "")
            cert_pem = data.get("certificate", "") + "\n" + data.get("issuing_ca", "")
            key_pem  = data.get("private_key", "")

            # Parse expiry and DNS names from the newly issued cert so callers
            # can update their data stores immediately without waiting for the
            # next adapter scrape cycle (default 5 minutes).
            expires_at_iso: str = ""
            days_until_expiry: int = 0
            dns_names: list = []
            try:
                from cryptography import x509 as _x509
                from cryptography.x509.oid import ExtensionOID as _EXT
                _cert_obj = _x509.load_pem_x509_certificate(cert_pem.encode())
                _exp = getattr(_cert_obj, "not_valid_after_utc", None) or \
                       _cert_obj.not_valid_after.replace(tzinfo=datetime.timezone.utc)
                expires_at_iso = _exp.strftime("%-d %b %Y, %H:%M UTC")
                days_until_expiry = (_exp - datetime.datetime.now(datetime.timezone.utc)).days
                try:
                    _san = _cert_obj.extensions.get_extension_for_oid(_EXT.SUBJECT_ALTERNATIVE_NAME)
                    dns_names = [v.value for v in _san.value if isinstance(v, _x509.DNSName)]
                except _x509.ExtensionNotFound:
                    _cn = _cert_obj.subject.get_attributes_for_oid(_x509.NameOID.COMMON_NAME)
                    dns_names = [_cn[0].value] if _cn else []
            except Exception:
                pass

            result: Dict[str, Any] = {
                "status": "rotated",
                "pki_mount": pki_mount,
                "pki_role": pki_role,
                "common_name": common_name,
                "serial": serial,
                "ttl": ttl,
                "expires_at": expires_at_iso,
                "days_until_expiry": days_until_expiry,
                "dns_names": dns_names,
            }

            # 2. Update K8s TLS Secret via in-cluster API.
            if k8s_secret_name and k8s_namespace:
                try:
                    sa_token = open("/var/run/secrets/kubernetes.io/serviceaccount/token").read()
                    k8s_headers = {
                        "Authorization": f"Bearer {sa_token}",
                        "Content-Type": "application/strategic-merge-patch+json",
                    }
                    patch = {"data": {
                        "tls.crt": base64.b64encode(cert_pem.encode()).decode(),
                        "tls.key": base64.b64encode(key_pem.encode()).decode(),
                    }}
                    k8s_resp = await client.patch(
                        f"https://kubernetes.default.svc/api/v1/namespaces/{k8s_namespace}/secrets/{k8s_secret_name}",
                        headers=k8s_headers,
                        json=patch,
                    )
                    result["k8s_secret_updated"] = k8s_resp.status_code in (200, 201)
                    result["k8s_secret"] = f"{k8s_namespace}/{k8s_secret_name}"
                    if k8s_resp.status_code not in (200, 201):
                        result["k8s_error"] = f"K8s PATCH returned {k8s_resp.status_code}: {k8s_resp.text[:200]}"
                except Exception as ke:
                    result["k8s_error"] = str(ke)

            # 3. Store audit record in Vault KV.
            await client.post(
                f"{_VAULT_ADDR}/v1/secret/data/payments/tls",
                headers=headers,
                json={"data": {"serial": serial, "common_name": common_name,
                               "renewed_at": datetime.datetime.utcnow().isoformat()}},
            )

            result["message"] = (
                f"Cert renewed from Vault (serial {serial}, TTL {ttl}). "
                + (f"K8s Secret {k8s_namespace}/{k8s_secret_name} updated." if k8s_secret_name else "")
            )
            return result

    except httpx.HTTPError as e:
        return {"error": f"Vault rotation failed: {e}"}


async def _get_similar_incidents(
    backend_url: str,
    namespace: str,
    service: str,
    description: str,
    limit: int = 3,
) -> Dict[str, Any]:
    """Query the backend for past incidents similar to the current description.

    If Voyage AI is configured the backend will have populated vector embeddings
    and returns cosine-similarity results. Otherwise falls back to recent incidents
    in the same namespace/service.
    """
    from config.settings import get_settings
    import urllib.parse

    settings = get_settings()
    params: Dict[str, Any] = {"namespace": namespace, "service": service, "limit": limit}

    # Optionally embed the description to enable vector search.
    if settings.voyage_api_key and description:
        try:
            import voyageai
            client = voyageai.Client(api_key=settings.voyage_api_key)
            result = client.embed([description], model=settings.voyage_model)
            vec = result.embeddings[0]
            params["vec"] = ",".join(f"{v:.6f}" for v in vec)
        except Exception as exc:
            logger.debug("embed for similarity query failed: %s", exc)

    qs = urllib.parse.urlencode(params)
    url = f"{backend_url.rstrip('/')}/api/incidents/similar?{qs}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                incidents = resp.json()
                if not incidents:
                    return {"similar_incidents": [], "note": "No past incidents found for this service."}
                return {"similar_incidents": incidents}
            return {"similar_incidents": [], "note": f"Backend returned {resp.status_code}"}
    except Exception as exc:
        return {"similar_incidents": [], "error": str(exc)}


async def _remote_live_fetch(backend_url: str, cluster_id: str, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Relay a live_* tool call to a specific fleet cluster's collector via
    the Hub's on-demand relay (ADR 0022 Decision #7 / ROADMAP P18 use case
    #9), instead of calling this agent pod's own in-cluster K8s API. `args`
    is passed through as-is minus `cluster_id` itself (the collector's
    live_tools.dispatch doesn't need to see its own routing key).
    """
    args = {k: v for k, v in arguments.items() if k != "cluster_id"}
    url = f"{backend_url.rstrip('/')}/api/live-fetch"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json={"cluster_id": cluster_id, "tool": tool_name, "args": args})
        if resp.status_code != 200:
            return {"error": f"live-fetch to cluster {cluster_id} failed ({resp.status_code}): {resp.text[:300]}"}
        return resp.json()
    except httpx.HTTPError as e:
        return {"error": f"live-fetch to cluster {cluster_id} failed: {e}"}


# Tool -> the field in its result dict holding the list to merge across
# clusters (ADR 0028). Only tools that can run at namespace/service scope
# without a specific pod appear here — live_get_pod_logs/live_describe_pod
# always require `pod`, which is already cluster-specific, so callers never
# attach `cluster_ids` (plural) for those (see agent.py's _structure_chat_answer).
_FANOUT_LIST_FIELD = {
    "live_list_pods": "pods",
    "live_get_events": "events",
    "live_get_certificates": "certificates",
}


def _merge_fanout_results(
    tool_name: str, cluster_ids: List[str], results: List[Any]
) -> Dict[str, Any]:
    """Deterministically merge one live-diagnostic tool's per-cluster results
    into a single tagged, concatenated response — no LLM involved (Diagram 7/8
    are deliberately no-LLM; see ADR 0028 vs. correlation.md's LLM-synthesis
    precedent for signals *within* one cluster). Each item in the merged list
    gets a `cluster_id` field so the operator can tell which cluster it came
    from. A cluster that errored or timed out is surfaced in `clusters_failed`
    rather than silently dropped (correlation.md's "surface, don't silently
    resolve" principle, extended to partial fan-out failure).
    """
    list_field = _FANOUT_LIST_FIELD.get(tool_name, "items")
    merged: List[Any] = []
    clusters_queried: List[str] = []
    clusters_failed: List[Dict[str, str]] = []

    for cluster_id, result in zip(cluster_ids, results):
        if isinstance(result, BaseException):
            clusters_failed.append({"cluster_id": cluster_id, "error": str(result)})
            continue
        if not isinstance(result, dict):
            clusters_failed.append({"cluster_id": cluster_id, "error": "unexpected result shape"})
            continue
        if result.get("error"):
            clusters_failed.append({"cluster_id": cluster_id, "error": result["error"]})
            continue
        clusters_queried.append(cluster_id)
        for item in result.get(list_field, []):
            merged.append({**item, "cluster_id": cluster_id})

    return {
        list_field: merged,
        "clusters_queried": clusters_queried,
        "clusters_failed": clusters_failed,
    }


async def _dispatch_live_diagnostic(tool_name: str, arguments: Dict[str, Any], backend_url: str) -> Dict[str, Any]:
    """Dispatch to the live_diagnostics function matching a LIVE_DIAGNOSTIC_TOOLS
    name — locally (this agent pod's own cluster) unless `cluster_id`/
    `cluster_ids` is present, in which case it's relayed to that fleet
    cluster's collector (singular) or fanned out and merged across several
    (plural, ADR 0028)."""
    cluster_ids = arguments.get("cluster_ids")
    if cluster_ids:
        fanout_args = {k: v for k, v in arguments.items() if k not in ("cluster_id", "cluster_ids")}
        results = await asyncio.gather(
            *[_remote_live_fetch(backend_url, cid, tool_name, {**fanout_args, "cluster_id": cid}) for cid in cluster_ids],
            return_exceptions=True,
        )
        return _merge_fanout_results(tool_name, cluster_ids, list(results))
    cluster_id = arguments.get("cluster_id")
    if cluster_id:
        return await _remote_live_fetch(backend_url, cluster_id, tool_name, arguments)
    if tool_name == "live_list_pods":
        return await live_diagnostics.live_list_pods(namespace=arguments.get("namespace", ""))
    if tool_name == "live_get_pod_logs":
        return await live_diagnostics.live_get_pod_logs(
            namespace=arguments.get("namespace", ""),
            pod=arguments.get("pod", ""),
            container=arguments.get("container"),
            tail_lines=int(arguments.get("tail_lines", 200)),
            previous=bool(arguments.get("previous", False)),
        )
    if tool_name == "live_get_events":
        return await live_diagnostics.live_get_events(
            namespace=arguments.get("namespace", ""),
            pod=arguments.get("pod"),
        )
    if tool_name == "live_describe_pod":
        return await live_diagnostics.live_describe_pod(
            namespace=arguments.get("namespace", ""),
            pod=arguments.get("pod", ""),
        )
    if tool_name == "live_get_certificates":
        # No local (this-agent's-own-cluster) implementation — remote-only,
        # always requires an explicit cluster_id (handled above, before this
        # local dispatch chain is ever reached). Reaching here means the
        # caller omitted cluster_id, which is always a caller bug for this
        # specific tool, not a degrade-gracefully case.
        return {"error": "live_get_certificates requires an explicit cluster_id — no local in-cluster implementation"}
    return {"error": f"Unknown live diagnostic tool: {tool_name}"}


async def process_tool_call(
    tool_name: str, arguments: Dict[str, Any], backend_url: str, timeout: float = 10.0
) -> Dict[str, Any]:
    """Execute a tool call by fetching live data from the backend.

    Each tool maps to a pod query on the backend (see backend HandleAgentFetch).
    Returns the fetched data, or an error dict the model can reason about — a
    failed tool call must not crash the agent's loop.
    """
    known = {t["name"] for t in TOOLS}
    if tool_name not in known:
        return {"error": f"Unknown tool: {tool_name}"}

    # Vault tools are handled locally (call Vault HTTP API directly).
    if tool_name == "get_vault_cert_status":
        return await _vault_get_cert_status(
            pki_role=arguments.get("pki_role", ""),
            kv_path=arguments.get("kv_path", ""),
        )
    if tool_name == "rotate_vault_cert":
        return await _vault_rotate_cert(
            pki_role=arguments.get("pki_role", ""),
            common_name=arguments.get("common_name", ""),
            ttl=arguments.get("ttl", "24h"),
            pki_mount=arguments.get("pki_mount", "pki-payments"),
            k8s_secret_name=arguments.get("k8s_secret_name", ""),
            k8s_namespace=arguments.get("k8s_namespace", ""),
        )

    # Semantic memory: similar past incidents (P8).
    if tool_name == "get_similar_incidents":
        return await _get_similar_incidents(
            backend_url=backend_url,
            namespace=arguments.get("namespace", ""),
            service=arguments.get("service", ""),
            description=arguments.get("description", ""),
            limit=int(arguments.get("limit", 3)),
        )

    # Live diagnostics: calls the live K8s API directly (or, with cluster_id,
    # relays to that fleet cluster's collector) — never the backend store.
    if tool_name in LIVE_DIAGNOSTIC_TOOLS:
        return await _dispatch_live_diagnostic(tool_name, arguments, backend_url)

    # get_logs: tries the log platform (Glue/Athena) first when configured,
    # falls back to the live cluster — see log_router.py.
    if tool_name == "get_logs":
        return await _get_logs(
            namespace=arguments.get("namespace", ""),
            pod=arguments.get("pod", ""),
            container=arguments.get("container"),
            tail_lines=int(arguments.get("tail_lines", 200)),
            previous=bool(arguments.get("previous", False)),
        )

    # get_service_dependencies: read-only — the graph mined from logs, see
    # service_topology.py.
    if tool_name == "get_service_dependencies":
        deps = await _fetch_service_dependencies(arguments.get("namespace", ""), backend_url)
        return {"namespace": arguments.get("namespace", ""), "dependencies": deps}

    url = f"{backend_url.rstrip('/')}/api/agent/fetch"
    payload = {"tool": tool_name, "args": arguments}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPError as exc:
        logger.error("tool fetch failed", extra={"tool": tool_name, "error": str(exc)})
        return {"error": f"backend fetch failed for {tool_name}: {exc}"}

    # body is {"tool": ..., "data": {pod_id: [rows], ...}}
    return body.get("data", {})
