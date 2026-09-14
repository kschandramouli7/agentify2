"""Deployment security posture (ROADMAP P30 phase 1, ADR 0033).

Pure build_*_findings functions — no live cluster or HTTP mocking needed,
same style as test_service_profile.py: given already-fetched k8s_client
output, assert the findings produced.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from discovery.security_posture import (  # noqa: E402
    build_ingress_tls_findings,
    build_networkpolicy_findings,
    build_pod_security_context_findings,
)


# ── build_networkpolicy_findings ────────────────────────────────────────────

def test_networkpolicy_finding_when_count_is_zero():
    findings = build_networkpolicy_findings("payments", 0)
    assert len(findings) == 1
    f = findings[0]
    assert f["check_id"] == "namespace-has-networkpolicy"
    assert f["resource_kind"] == "Namespace"
    assert f["resource_name"] == "payments"
    assert f["severity"] == "high"


def test_no_networkpolicy_finding_when_count_is_positive():
    assert build_networkpolicy_findings("payments", 1) == []
    assert build_networkpolicy_findings("payments", 5) == []


# ── build_pod_security_context_findings ─────────────────────────────────────

def _secure_container(name="worker"):
    return {
        "name": name,
        "run_as_non_root": True,
        "read_only_root_filesystem": True,
        "allow_privilege_escalation": False,
        "drops_capabilities": True,
    }


def test_fully_secure_container_produces_no_finding():
    pods = [{"name": "payment-api-abc", "containers": [_secure_container()]}]
    assert build_pod_security_context_findings("payments", pods) == []


def test_missing_run_as_non_root_is_a_finding():
    container = _secure_container()
    container["run_as_non_root"] = None  # unset, not explicitly False
    pods = [{"name": "payment-api-abc", "containers": [container]}]
    findings = build_pod_security_context_findings("payments", pods)
    assert len(findings) == 1
    f = findings[0]
    assert f["check_id"] == "pod-security-context"
    assert f["resource_kind"] == "Pod"
    assert f["resource_name"] == "payment-api-abc/worker"
    assert f["severity"] == "medium"
    assert "runAsNonRoot" in f["evidence"]


def test_explicitly_false_is_a_finding_same_as_unset():
    """An explicitly insecure setting is exactly as much a finding as an
    unset one — there is no reason to treat "someone set this wrong" as
    better than "nobody set this"."""
    container = _secure_container()
    container["allow_privilege_escalation"] = True  # explicitly insecure
    pods = [{"name": "p", "containers": [container]}]
    findings = build_pod_security_context_findings("payments", pods)
    assert len(findings) == 1
    assert "allowPrivilegeEscalation" in findings[0]["evidence"]


def test_no_capability_drop_is_a_finding():
    container = _secure_container()
    container["drops_capabilities"] = False
    pods = [{"name": "p", "containers": [container]}]
    findings = build_pod_security_context_findings("payments", pods)
    assert len(findings) == 1
    assert "capabilities are dropped" in findings[0]["evidence"]


def test_multiple_missing_fields_join_into_one_finding_per_container():
    """One finding per container, not one per missing field — the evidence
    string lists every reason, so a caller isn't asked to fix four rows for
    the same container to make it disappear from the panel."""
    pods = [{"name": "p", "containers": [{
        "name": "c", "run_as_non_root": None, "read_only_root_filesystem": None,
        "allow_privilege_escalation": None, "drops_capabilities": False,
    }]}]
    findings = build_pod_security_context_findings("payments", pods)
    assert len(findings) == 1
    evidence = findings[0]["evidence"]
    assert "runAsNonRoot" in evidence
    assert "readOnlyRootFilesystem" in evidence
    assert "allowPrivilegeEscalation" in evidence
    assert "capabilities are dropped" in evidence


def test_multiple_containers_produce_separate_findings():
    pods = [{"name": "p", "containers": [
        _secure_container("good"),
        {**_secure_container("bad"), "run_as_non_root": None},
    ]}]
    findings = build_pod_security_context_findings("payments", pods)
    assert len(findings) == 1
    assert findings[0]["resource_name"] == "p/bad"


def test_no_pods_produces_no_findings():
    assert build_pod_security_context_findings("payments", []) == []


# ── build_ingress_tls_findings ───────────────────────────────────────────────

def test_ingress_with_tls_produces_no_finding():
    ingresses = [{"name": "shop-ingress", "hosts": ["shop.example.com"], "backend_services": ["storefront"], "has_tls": True}]
    assert build_ingress_tls_findings("payments", ingresses) == []


def test_ingress_without_tls_is_a_finding():
    ingresses = [{"name": "shop-ingress", "hosts": ["shop.example.com"], "backend_services": ["storefront"], "has_tls": False}]
    findings = build_ingress_tls_findings("payments", ingresses)
    assert len(findings) == 1
    f = findings[0]
    assert f["check_id"] == "ingress-missing-tls"
    assert f["resource_kind"] == "Ingress"
    assert f["resource_name"] == "shop-ingress"
    assert f["severity"] == "critical"


def test_mixed_ingresses_only_flag_the_insecure_ones():
    ingresses = [
        {"name": "secure-ingress", "hosts": [], "backend_services": [], "has_tls": True},
        {"name": "insecure-ingress", "hosts": [], "backend_services": [], "has_tls": False},
    ]
    findings = build_ingress_tls_findings("payments", ingresses)
    assert len(findings) == 1
    assert findings[0]["resource_name"] == "insecure-ingress"


def test_no_ingresses_produces_no_findings():
    assert build_ingress_tls_findings("payments", []) == []
