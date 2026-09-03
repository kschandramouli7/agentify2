"""The scan denominator (ROADMAP P27 phase 1).

`service_dependencies.evidence_count` is a numerator with no denominator, so
"seen 51 times" cannot distinguish three very different problems: the service
is called rarely, its logs are unreadable, or its pods are never among the
MAX_PODS_PER_NAMESPACE sampled. That ambiguity is what made payment-worker's
decline uninterpretable on 2026-09-03.

The counters only earn their keep if the arithmetic is right, so these tests
pin it — especially the two ways it could flatter itself:
  - counting pods_seen from the TRUNCATED list, which would report full
    coverage of a 5-pod sample and hide the sampling entirely;
  - failing to advance scan_cycles for a service that was scanned but never
    sampled, which is the case the denominator exists to reveal.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from discovery import main as discovery_main  # noqa: E402


def _pod(name, app):
    return {"name": name, "labels": {"app": app}}


class _Cfg:
    backend_url = "http://backend"
    collector_token = "tok"
    max_pods_per_namespace = 2      # deliberately smaller than the pod count
    log_tail_lines = 200


@pytest.fixture
def captured(monkeypatch):
    """Run _scan_namespace against fakes and return the coverage report."""
    reports = {}

    async def fake_push_coverage(ns, stats, backend_url, token):
        reports[ns] = stats

    async def fake_push_dependency(*a, **k):
        return None

    monkeypatch.setattr(discovery_main, "push_scan_coverage", fake_push_coverage)
    monkeypatch.setattr(discovery_main, "push_dependency", fake_push_dependency)
    return reports


async def _run(monkeypatch, services, pods, logs_by_pod):
    async def fake_list_services(ns):
        return services

    async def fake_list_pods(ns):
        return pods

    async def fake_get_pod_logs(ns, pod, tail_lines=200):
        return logs_by_pod.get(pod, "")

    monkeypatch.setattr(discovery_main.k8s_client, "list_services", fake_list_services)
    monkeypatch.setattr(discovery_main.k8s_client, "list_pods", fake_list_pods)
    monkeypatch.setattr(discovery_main.k8s_client, "get_pod_logs", fake_get_pod_logs)
    await discovery_main._scan_namespace("payments", _Cfg())


@pytest.mark.asyncio
async def test_pods_seen_counts_the_full_list_not_the_sample(monkeypatch, captured):
    """The self-flattery this guards against: with max_pods=2 and 4 pods, a
    naive implementation reports 2 of 2 sampled = full coverage, hiding that
    half the pods were never looked at."""
    services = [{"name": "api", "selector": {"app": "api"}}]
    pods = [_pod(f"api-{i}", "api") for i in range(4)]
    await _run(monkeypatch, services, pods, {})

    cov = captured["payments"]["api"]
    assert cov["pods_seen"] == 4, "pods_seen must count every pod that exists"
    assert cov["pods_sampled"] == 2, "pods_sampled must respect max_pods_per_namespace"


@pytest.mark.asyncio
async def test_a_service_scanned_but_never_sampled_still_advances_its_denominator(monkeypatch, captured):
    """The case the whole item exists for. `worker`'s pod sorts after the
    sample cap, so it is never read — but its scan_cycles must still advance,
    or its coverage would look like 0/0 (unknown) instead of 0/N (invisible)."""
    services = [
        {"name": "api", "selector": {"app": "api"}},
        {"name": "worker", "selector": {"app": "worker"}},
    ]
    pods = [_pod("api-1", "api"), _pod("api-2", "api"), _pod("worker-1", "worker")]
    await _run(monkeypatch, services, pods, {})

    worker = captured["payments"]["worker"]
    assert worker["scan_cycles"] == 1
    assert worker["pods_seen"] == 1, "the pod exists and must be counted"
    assert worker["pods_sampled"] == 0, "but it was never read"


@pytest.mark.asyncio
async def test_a_service_with_no_pods_at_all_is_reported(monkeypatch, captured):
    """A Service backed by nothing is a real finding, not an absence of data."""
    services = [{"name": "ghost", "selector": {"app": "ghost"}}]
    await _run(monkeypatch, services, [], {})

    ghost = captured["payments"]["ghost"]
    assert ghost == {"scan_cycles": 1, "pods_seen": 0, "pods_sampled": 0,
                     "logs_readable": 0, "log_lines": 0}


@pytest.mark.asyncio
async def test_unreadable_logs_count_as_sampled_but_not_readable(monkeypatch, captured):
    """OPS-9 makes get_pod_logs return "" for every multi-container pod. That
    is a platform problem and must be distinguishable from "read the logs and
    found no mentions", which is a real observation."""
    services = [{"name": "api", "selector": {"app": "api"}}]
    pods = [_pod("api-1", "api"), _pod("api-2", "api")]
    await _run(monkeypatch, services, pods, {"api-1": "some log line\n"})  # api-2 returns ""

    cov = captured["payments"]["api"]
    assert cov["pods_sampled"] == 2
    assert cov["logs_readable"] == 1


@pytest.mark.asyncio
async def test_log_lines_are_counted(monkeypatch, captured):
    services = [{"name": "api", "selector": {"app": "api"}}]
    pods = [_pod("api-1", "api")]
    await _run(monkeypatch, services, pods, {"api-1": "one\ntwo\nthree"})

    assert captured["payments"]["api"]["log_lines"] == 3


@pytest.mark.asyncio
async def test_unattributable_pods_do_not_inflate_any_service(monkeypatch, captured):
    """A bare Job matched by no Service must not be counted against a service
    that happens to be in the namespace."""
    services = [{"name": "api", "selector": {"app": "api"}}]
    pods = [_pod("orphan-1", "not-selected-by-anything")]
    await _run(monkeypatch, services, pods, {"orphan-1": "log\n"})

    cov = captured["payments"]["api"]
    assert cov["pods_seen"] == 0
    assert cov["pods_sampled"] == 0


@pytest.mark.asyncio
async def test_no_services_reports_nothing_rather_than_an_empty_denominator(monkeypatch, captured):
    """With no Services there is nothing to attribute to, so no report — as
    opposed to a report of zeroes, which would imply we looked and found none."""
    await _run(monkeypatch, [], [_pod("p", "x")], {})
    assert "payments" not in captured


@pytest.mark.asyncio
async def test_coverage_fraction_reproduces_the_payment_worker_case(monkeypatch, captured):
    """End to end on the shape of the real incident: two services, one whose
    pod is sampled every cycle and one whose never is. After the fact, coverage
    for the second is 0/N rather than an unexplained low evidence_count."""
    services = [
        {"name": "batch", "selector": {"app": "batch"}},
        {"name": "worker", "selector": {"app": "worker"}},
    ]
    pods = [_pod("batch-1", "batch"), _pod("batch-2", "batch"), _pod("worker-1", "worker")]
    await _run(monkeypatch, services, pods, {"batch-1": "x\n", "batch-2": "y\n"})

    rep = captured["payments"]
    assert rep["batch"]["logs_readable"] == 2 and rep["batch"]["pods_sampled"] == 2
    assert rep["worker"]["pods_seen"] == 1 and rep["worker"]["pods_sampled"] == 0
    # The interpretation the denominator makes possible:
    assert rep["worker"]["pods_sampled"] / rep["worker"]["pods_seen"] == 0.0
