"""security_posture.py — deployment security posture checks (ROADMAP P30
phase 1, ADR 0033).

Three checks, chosen because two need zero new RBAC and one needs the
smallest possible new grant — proving the whole pattern (K8s read -> check
-> finding -> panel) without asking for broad new cluster access in the same
feature that flags other ClusterRoles as too broad:

- `namespace-has-networkpolicy` — needs the new `networkpolicies` list/get
  grant (`discovery.yaml`).
- `pod-security-context` — reuses the existing `pods` read.
- `ingress-missing-tls` — reuses the existing `ingresses` read.

Findings are config-state, not accumulated evidence — the Hub
(UpsertSecurityFindings) treats a push as this cycle's *complete* set for the
namespace and resolves anything missing from it, so a build_* function below
must return every currently-true finding every time, never a diff.

Mirrors ingress.py's shape: pure `build_*_findings` functions (easy to unit
test without a live cluster) plus one `push_*` function using the same
httpx/bearer-token/best-effort-log-and-swallow pattern every other push_*
function in this package already uses.
"""

import logging
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)


def build_networkpolicy_findings(namespace: str, network_policy_count: int) -> List[Dict[str, str]]:
    """One finding if `namespace` has zero NetworkPolicy objects, else none.
    Severity is "high", not "critical" — a namespace with no NetworkPolicy is
    the *default* K8s posture, not a misconfiguration introduced by this
    workload; still worth flagging, but a step down from an actively
    insecure setting like a missing TLS listener."""
    if network_policy_count > 0:
        return []
    return [{
        "check_id": "namespace-has-networkpolicy",
        "resource_kind": "Namespace",
        "resource_name": namespace,
        "severity": "high",
        "evidence": "0 NetworkPolicy objects found in this namespace — every pod can reach every other pod.",
    }]


# Fields checked per container, each field's SECURE value, and why it
# matters — kept as one table so the evidence string and the check logic
# can't drift apart. The secure value is not uniformly True:
# allowPrivilegeEscalation is secure when False, the other two when True —
# collapsing this into a single "must be True" check was a real bug caught
# by this file's own tests (a container with the correct, secure
# allowPrivilegeEscalation: false was flagged as a finding).
_POD_SECURITY_CONTEXT_FIELDS = (
    ("run_as_non_root", True, "runAsNonRoot is not set to true (container may run as root)"),
    ("read_only_root_filesystem", True, "readOnlyRootFilesystem is not set to true"),
    ("allow_privilege_escalation", False, "allowPrivilegeEscalation is not set to false"),
)


def build_pod_security_context_findings(namespace: str, pods: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """One finding per container missing any of runAsNonRoot,
    readOnlyRootFilesystem, allowPrivilegeEscalation:false, or a capability
    drop list. `pods` is `list_pod_security_contexts`' output shape.
    Booleans of `None` (unset) and the explicitly-insecure value are both
    findings — a security posture check has no reason to treat "nobody set
    this" as better than "someone set this wrong."""
    findings = []
    for pod in pods:
        pod_name = pod.get("name", "")
        for container in pod.get("containers", []):
            reasons = [
                msg for field, secure_value, msg in _POD_SECURITY_CONTEXT_FIELDS
                if container.get(field) is not secure_value
            ]
            if not container.get("drops_capabilities"):
                reasons.append("no capabilities are dropped")
            if not reasons:
                continue
            findings.append({
                "check_id": "pod-security-context",
                "resource_kind": "Pod",
                "resource_name": f"{pod_name}/{container.get('name', '')}",
                "severity": "medium",
                "evidence": "; ".join(reasons),
            })
    return findings


def build_ingress_tls_findings(namespace: str, ingresses: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """One finding per Ingress with no `tls:` block. Severity is "critical"
    — unlike a missing NetworkPolicy (the K8s default), an Ingress is
    something this workload deliberately created to accept traffic, so
    serving it over plaintext HTTP is an active choice, not an absence."""
    findings = []
    for ing in ingresses:
        if ing.get("has_tls"):
            continue
        findings.append({
            "check_id": "ingress-missing-tls",
            "resource_kind": "Ingress",
            "resource_name": ing.get("name", ""),
            "severity": "critical",
            "evidence": "No tls: block on this Ingress — traffic is served over plaintext HTTP.",
        })
    return findings


async def push_security_findings(
    namespace: str, findings: List[Dict[str, str]], backend_url: str, collector_token: str,
) -> None:
    """Push this namespace's complete current finding set. Same full-
    replace-per-push semantics as push_inventory/push_ingress: the Hub
    resolves any prior finding not present in this push, so an empty
    `findings` list is meaningful (namespace passed every check this cycle)
    and must still be sent, not skipped."""
    payload = {"namespace": namespace, "findings": findings}
    # Omit the header entirely when unset — see push_inventory's identical
    # comment (inventory.py) for why.
    headers = {"Authorization": f"Bearer {collector_token}"} if collector_token else {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{backend_url.rstrip('/')}/api/security-findings",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("push_security_findings failed for namespace=%s: %s", namespace, e)
