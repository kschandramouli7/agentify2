"""Service profiles — what each Service IS (ROADMAP P22).

Every field comes from requests the collector already made and discarded, so
these tests are mostly about ATTRIBUTION: matching a workload to the Service
that fronts it, using the pod TEMPLATE labels rather than the workload's own.
Getting that backwards would attach the wrong kind and replica count to a box
on the architecture diagram, which is worse than showing none.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from discovery import main as discovery_main  # noqa: E402


async def _profiles(monkeypatch, services, workloads):
    async def fake_list_workloads(ns):
        return workloads

    monkeypatch.setattr(discovery_main.k8s_client, "list_workloads", fake_list_workloads)
    rows = await discovery_main._service_profiles("payments", services)
    return {r["service"]: r for r in rows}


@pytest.mark.asyncio
async def test_workload_is_matched_by_pod_template_labels(monkeypatch):
    """A Service selects the PODS, so the template labels are what match. The
    workload's own metadata labels commonly differ — using them would silently
    attribute nothing."""
    out = await _profiles(
        monkeypatch,
        [{"name": "payment-api", "selector": {"app": "payment-api"}, "type": "ClusterIP",
          "ports": [{"name": "https", "port": 8443, "protocol": "TCP"}]}],
        [{"kind": "Deployment", "name": "payment-api",
          # deliberately NOT the selector labels
          "template_labels": {"app": "payment-api", "pod-template-hash": "x"},
          "replicas_desired": 2, "replicas_ready": 0, "images": ["nginx:1.25"]}],
    )
    p = out["payment-api"]
    assert p["workload_kind"] == "Deployment"
    assert p["replicas_desired"] == 2
    assert p["replicas_ready"] == 0     # the OPS-5 shape, and it must survive as 0
    assert p["image"] == "nginx:1.25"
    assert p["ports"] == [{"name": "https", "port": 8443, "protocol": "TCP"}]


@pytest.mark.asyncio
async def test_zero_ready_is_kept_not_coalesced(monkeypatch):
    """0/2 is an urgent finding; None means "not reported". They must not be
    stored or rendered the same way."""
    out = await _profiles(
        monkeypatch,
        [{"name": "a", "selector": {"app": "a"}}],
        [{"kind": "Deployment", "name": "a", "template_labels": {"app": "a"},
          "replicas_desired": 2, "replicas_ready": 0, "images": []}],
    )
    assert out["a"]["replicas_ready"] == 0
    assert out["a"]["replicas_ready"] is not None


@pytest.mark.asyncio
async def test_a_service_with_no_matching_workload_still_gets_a_profile(monkeypatch):
    """Informative in itself: something fronts pods that nothing in this
    namespace declares. Must not be dropped."""
    out = await _profiles(
        monkeypatch,
        [{"name": "orphan", "selector": {"app": "nothing-declares-this"}, "type": "ClusterIP"}],
        [{"kind": "Deployment", "name": "other", "template_labels": {"app": "other"},
          "replicas_desired": 1, "replicas_ready": 1, "images": ["x"]}],
    )
    assert "orphan" in out
    assert out["orphan"]["workload_kind"] == ""
    assert out["orphan"]["replicas_ready"] is None


@pytest.mark.asyncio
async def test_a_selectorless_service_matches_nothing(monkeypatch):
    """An empty selector means manually-managed Endpoints. Treating {} as
    "matches everything" would attach the first workload in the namespace to
    it — the same bug service_index guards against."""
    out = await _profiles(
        monkeypatch,
        [{"name": "manual", "selector": {}}],
        [{"kind": "Deployment", "name": "d", "template_labels": {"app": "d"},
          "replicas_desired": 1, "replicas_ready": 1, "images": ["x"]}],
    )
    assert out["manual"]["workload_kind"] == ""


@pytest.mark.asyncio
async def test_headless_is_reported_as_its_own_exposure(monkeypatch):
    """clusterIP: None is how a StatefulSet does peer discovery — a different
    architectural fact from a normal ClusterIP."""
    out = await _profiles(
        monkeypatch,
        [{"name": "store", "selector": {"app": "store"}, "type": "ClusterIP", "headless": True}],
        [{"kind": "StatefulSet", "name": "store", "template_labels": {"app": "store"},
          "replicas_desired": 3, "replicas_ready": 3, "images": ["postgres:16"]}],
    )
    assert out["store"]["service_type"] == "Headless"
    assert out["store"]["workload_kind"] == "StatefulSet"


@pytest.mark.asyncio
async def test_cronjob_carries_its_schedule(monkeypatch):
    out = await _profiles(
        monkeypatch,
        [{"name": "batch", "selector": {"app": "batch"}}],
        [{"kind": "CronJob", "name": "batch", "template_labels": {"app": "batch"},
          "replicas_desired": None, "replicas_ready": None,
          "images": ["alpine:3.19"], "schedule": "*/30 * * * *"}],
    )
    assert out["batch"]["workload_kind"] == "CronJob"
    assert out["batch"]["schedule"] == "*/30 * * * *"


@pytest.mark.asyncio
async def test_a_workload_listing_failure_does_not_lose_the_services(monkeypatch):
    """Profiles are an enrichment. Losing them must not lose the inventory,
    which is what the diagram needs to draw a box at all."""
    async def boom(ns):
        raise RuntimeError("k8s down")

    monkeypatch.setattr(discovery_main.k8s_client, "list_workloads", boom)
    with pytest.raises(RuntimeError):
        await discovery_main._service_profiles("payments", [{"name": "a", "selector": {}}])
    # _scan_inventory is the layer that swallows this — asserted there, not here.
