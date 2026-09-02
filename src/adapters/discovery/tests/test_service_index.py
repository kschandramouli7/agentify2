"""Pod -> Service attribution at push time.

The gap this closes: the Hub stores per-pod state and every Service's label
selector, but the pod event payload carried no labels, so nothing Hub-side
could join them. Per-service health and version were blocked on data that was
already being collected (ROADMAP P22's data audit).

These tests pin the three things that can silently regress:
  1. the selector semantics (all keys must match; an empty selector matches
     nothing) — this is K8s' own rule and getting it wrong misattributes pods;
  2. the index staying correct as Services are added, modified and deleted;
  3. the coverage counters, because a `service` field that is quietly absent on
     most events would be trusted by everything downstream.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from discovery.service_index import ServiceIndex, service_for_labels  # noqa: E402

SERVICES = [
    {"name": "payment-api", "selector": {"app": "payment-api"}},
    {"name": "payment", "selector": {"app": "payment", "tier": "backend"}},
    {"name": "manual-endpoints", "selector": {}},
]


# ── selector semantics ───────────────────────────────────────────────────────


def test_matches_when_every_selector_key_matches():
    labels = {"app": "payment-api", "pod-template-hash": "abc123"}
    assert service_for_labels(labels, SERVICES) == "payment-api"


def test_extra_pod_labels_are_ignored():
    """A pod always carries labels the Service does not select on
    (pod-template-hash, and anything a mesh or operator injects)."""
    labels = {"app": "payment-api", "pod-template-hash": "x", "istio.io/rev": "default"}
    assert service_for_labels(labels, SERVICES) == "payment-api"


def test_a_multi_key_selector_needs_all_keys():
    assert service_for_labels({"app": "payment", "tier": "backend"}, SERVICES) == "payment"
    # Missing the second key must NOT match — this is the misattribution bug.
    assert service_for_labels({"app": "payment"}, SERVICES) is None


def test_an_empty_selector_never_matches():
    """A Service with no selector has manually-managed Endpoints. Treating {}
    as "matches everything" would attribute every pod in the namespace to it."""
    assert service_for_labels({"anything": "at-all"}, [{"name": "m", "selector": {}}]) is None


def test_no_labels_resolves_to_none():
    assert service_for_labels(None, SERVICES) is None
    assert service_for_labels({}, SERVICES) is None


def test_missing_selector_key_is_tolerated():
    """A Service dict without a `selector` key at all must not raise."""
    assert service_for_labels({"app": "x"}, [{"name": "s"}]) is None


# ── the index ────────────────────────────────────────────────────────────────


def _svc(name, namespace, selector):
    return {"metadata": {"name": name, "namespace": namespace}, "spec": {"selector": selector}}


def test_index_resolves_after_a_service_added_event():
    idx = ServiceIndex()
    idx.apply_service_event("ADDED", _svc("payment-api", "payments", {"app": "payment-api"}))
    assert idx.resolve("payments", {"app": "payment-api"}) == "payment-api"


def test_index_reflects_a_changed_selector():
    """MODIFIED must replace the selector, not merge with the old one — a pod
    matching only the OLD selector must stop resolving."""
    idx = ServiceIndex()
    idx.apply_service_event("ADDED", _svc("api", "payments", {"app": "old"}))
    idx.apply_service_event("MODIFIED", _svc("api", "payments", {"app": "new"}))
    assert idx.resolve("payments", {"app": "new"}) == "api"
    assert idx.resolve("payments", {"app": "old"}) is None


def test_deleted_removes_the_service():
    idx = ServiceIndex()
    idx.apply_service_event("ADDED", _svc("api", "payments", {"app": "api"}))
    idx.apply_service_event("DELETED", _svc("api", "payments", {"app": "api"}))
    assert idx.resolve("payments", {"app": "api"}) is None


def test_namespaces_are_isolated():
    """The same service name in two namespaces must not cross-attribute."""
    idx = ServiceIndex()
    idx.apply_service_event("ADDED", _svc("api", "payments", {"app": "api"}))
    assert idx.resolve("other", {"app": "api"}) is None


def test_seed_replaces_wholesale():
    """The scan loop seeds from an authoritative list, so a Service that has
    disappeared must not survive in the index."""
    idx = ServiceIndex()
    idx.seed("payments", [{"name": "a", "selector": {"app": "a"}}, {"name": "b", "selector": {"app": "b"}}])
    idx.seed("payments", [{"name": "a", "selector": {"app": "a"}}])
    assert idx.resolve("payments", {"app": "a"}) == "a"
    assert idx.resolve("payments", {"app": "b"}) is None


def test_malformed_service_events_are_ignored_not_fatal():
    idx = ServiceIndex()
    idx.apply_service_event("ADDED", {})
    idx.apply_service_event("ADDED", {"metadata": {"name": "no-namespace"}})
    idx.apply_service_event("ADDED", {"metadata": {"namespace": "no-name"}})
    assert idx.coverage()["namespaces_indexed"] == 0


def test_a_service_with_a_null_selector_is_stored_as_empty():
    """`spec.selector` absent or null must become {} — and therefore match
    nothing — rather than crashing the resolve path."""
    idx = ServiceIndex()
    idx.apply_service_event("ADDED", {"metadata": {"name": "s", "namespace": "n"}, "spec": {}})
    assert idx.resolve("n", {"app": "x"}) is None


# ── coverage counters ────────────────────────────────────────────────────────


def test_coverage_distinguishes_unindexed_from_unmatched():
    """The distinction that makes the counter worth having: "we have never seen
    this namespace" (a startup-window or RBAC problem) is not the same as "no
    Service selects this pod" (normal for a bare Job)."""
    idx = ServiceIndex()
    idx.seed("payments", [{"name": "api", "selector": {"app": "api"}}])

    idx.resolve("payments", {"app": "api"})      # resolved
    idx.resolve("payments", {"app": "orphan"})   # indexed, but nothing selects it
    idx.resolve("never-seen", {"app": "api"})    # namespace not indexed at all

    cov = idx.coverage()
    assert cov["attempts"] == 3
    assert cov["resolved"] == 1
    assert cov["unindexed_namespace"] == 1
    assert cov["rate"] == pytest.approx(1 / 3)


def test_coverage_rate_is_none_before_any_attempt():
    """None, not 0.0 — "no data yet" must never be logged as "0% resolved"."""
    assert ServiceIndex().coverage()["rate"] is None


# ── wiring: does the field actually reach the payload? ───────────────────────
#
# The unit tests above prove the matcher and the index. These prove they are
# CONNECTED — the failure mode this codebase keeps hitting is a correct
# component that nothing calls.


def test_pod_payload_carries_the_resolved_service():
    from discovery import normalize

    pod = {
        "metadata": {"name": "payment-api-abc", "namespace": "payments", "labels": {"app": "payment-api"}},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
    }
    event = normalize.normalize_pod_event(pod, "MODIFIED", "payment-api")
    assert event["payload"]["service"] == "payment-api"


def test_pod_payload_omits_service_when_unresolved():
    """Absent, not null: a consumer must not be able to read "attributed to
    nothing" as an attribution."""
    from discovery import normalize

    pod = {"metadata": {"name": "p", "namespace": "n"}, "status": {"phase": "Running"}}
    assert "service" not in normalize.normalize_pod_event(pod, "ADDED")["payload"]
    assert "service" not in normalize.normalize_pod_event(pod, "ADDED", None)["payload"]


def test_deploy_payload_carries_the_resolved_service():
    from discovery import normalize

    dep = {
        "metadata": {"name": "payment-api", "namespace": "payments"},
        "spec": {"replicas": 2, "template": {"spec": {"containers": [{"image": "payment-api:1.4"}]}}},
    }
    event = normalize.normalize_deploy_event(dep, "7", "payment-api")
    assert event["payload"]["service"] == "payment-api"
    assert event["payload"]["images"] == ["payment-api:1.4"]
    assert event["payload"]["replicas_desired"] == 2


@pytest.mark.asyncio
async def test_watch_resolves_a_pod_through_the_shared_index(monkeypatch):
    """End to end: a Services event indexes the selector, then a pod event on
    the other stream is attributed with no extra API call."""
    from discovery import watch
    from discovery.service_index import INDEX

    pushed = []

    async def fake_push(event, backend_url, token):
        pushed.append(event)

    monkeypatch.setattr(watch.normalize, "push_event", fake_push)
    monkeypatch.setattr(INDEX, "_by_namespace", {}, raising=False)

    cfg = type("C", (), {"backend_url": "http://b", "collector_token": ""})()

    await watch._handle_service_event(
        "ADDED",
        {"metadata": {"name": "payment-api", "namespace": "payments"}, "spec": {"selector": {"app": "payment-api"}}},
        cfg,
    )
    await watch._handle_pod_event(
        "MODIFIED",
        {
            "metadata": {"name": "payment-api-1", "namespace": "payments", "labels": {"app": "payment-api"}},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
        },
        cfg,
    )

    pod_event = next(e for e in pushed if e["type"].startswith("pod_"))
    assert pod_event["payload"]["service"] == "payment-api"


@pytest.mark.asyncio
async def test_watch_resolves_a_deployment_from_its_POD_TEMPLATE_labels(monkeypatch):
    """A Service selects the pods, so the Deployment's own metadata labels are
    the wrong ones to match. Using them would silently fail wherever the two
    label sets differ — which is common."""
    from discovery import watch
    from discovery.service_index import INDEX

    pushed = []

    async def fake_push(event, backend_url, token):
        pushed.append(event)

    monkeypatch.setattr(watch.normalize, "push_event", fake_push)
    monkeypatch.setattr(INDEX, "_by_namespace", {}, raising=False)
    INDEX.seed("payments", [{"name": "payment-api", "selector": {"app": "payment-api"}}])

    cfg = type("C", (), {"backend_url": "http://b", "collector_token": ""})()
    handler = watch._make_deployment_handler()

    await handler(
        "MODIFIED",
        {
            "metadata": {
                "name": "payment-api",
                "namespace": "payments",
                # Deliberately NOT the selector labels.
                "labels": {"managed-by": "helm"},
                "annotations": {"deployment.kubernetes.io/revision": "9"},
            },
            "spec": {
                "replicas": 2,
                "template": {
                    "metadata": {"labels": {"app": "payment-api"}},
                    "spec": {"containers": [{"image": "payment-api:1.4"}]},
                },
            },
        },
        cfg,
    )

    assert pushed, "no deploy event was pushed"
    assert pushed[0]["payload"]["service"] == "payment-api"
