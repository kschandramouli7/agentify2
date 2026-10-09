"""Tests for config.py's ROADMAP P31 phase 1 (cross-cluster call capture,
ADR 0037) addition — cross_cluster_ingress_kinds. The rest of Config has no
existing test coverage; this file is scoped to the new knob only."""

from discovery.config import load_from_env


def test_cross_cluster_ingress_kinds_defaults_to_route_ingress_httproute(monkeypatch):
    """Widened 2026-10-09 (ADR 0037 amendment): Route is OpenShift-only, so
    a Route-only default cannot capture any cross-cluster edge touching a
    non-OpenShift platform — exactly the case a mixed OpenShift+EKS+GKE
    fleet is."""
    monkeypatch.delenv("CROSS_CLUSTER_INGRESS_KINDS", raising=False)
    cfg = load_from_env()
    assert cfg.cross_cluster_ingress_kinds == ["route", "ingress", "httproute"]


def test_cross_cluster_ingress_kinds_parses_a_comma_separated_list(monkeypatch):
    monkeypatch.setenv("CROSS_CLUSTER_INGRESS_KINDS", "route,ingress,httproute")
    cfg = load_from_env()
    assert cfg.cross_cluster_ingress_kinds == ["route", "ingress", "httproute"]


def test_cross_cluster_ingress_kinds_strips_whitespace_and_drops_empties(monkeypatch):
    monkeypatch.setenv("CROSS_CLUSTER_INGRESS_KINDS", " route , , ingress ")
    cfg = load_from_env()
    assert cfg.cross_cluster_ingress_kinds == ["route", "ingress"]
