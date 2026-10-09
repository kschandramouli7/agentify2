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
    mine_external_egress = False
    cross_cluster_ingress_kinds = ["route"]  # ROADMAP P31 phase 1


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
    """A genuinely unreadable log (get_pod_logs returning "" after exhausting
    its own retry — see test_k8s_client.py's OPS-9 coverage for that retry
    itself) is a platform problem and must be distinguishable from "read the
    logs and found no mentions", which is a real observation."""
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


@pytest.mark.asyncio
async def test_scan_namespace_pushes_port_and_last_known_outcome(monkeypatch, captured):
    """ROADMAP P27 phase 2: port is captured from the bare host:port form,
    and when a pod's log tail mentions the same target twice, the LAST known
    outcome wins — a later unclassifiable line must not erase an earlier
    confident one."""
    pushed = []

    async def fake_push_dependency(ns, from_service, to_service, backend_url, token, target_kind="service",
                                    port=None, outcome=None, path="", caller_pod="", match_kind="", source=""):
        pushed.append((from_service, to_service, port, outcome, path, caller_pod, match_kind, source))

    monkeypatch.setattr(discovery_main, "push_dependency", fake_push_dependency)

    services = [
        {"name": "batch", "selector": {"app": "batch"}},
        {"name": "api", "selector": {"app": "api"}},
    ]
    pods = [_pod("batch-1", "batch")]
    logs = "\n".join([
        "called api:8443 ok",
        "called api:8443 unreachable",   # last known outcome for (api, 8443)
        "GET api.payments.svc.cluster.local",  # same service, no port, no outcome
    ])
    await _run(monkeypatch, services, pods, {"batch-1": logs})

    # Neither line has a trailing path, so path stays "" (ROADMAP P27 phase 4)
    # for both — it doesn't further split these two already-distinct (port)
    # dedup keys. caller_pod is the sampled pod's own name (also phase 4,
    # caller cardinality) — same for every push here, since only one pod
    # was sampled. match_kind/source (also phase 4, provenance): the two
    # "api:8443" lines are both the bare host:port form; "live" is this
    # producer's own fixed source identity.
    assert ("batch", "api", 8443, "failure", "", "batch-1", "bare", "live") in pushed
    # The qualified FQDN form ("api.payments.svc.cluster.local") is a
    # separate (port=0) key, so it keeps its own distinct match_kind.
    assert ("batch", "api", 0, None, "", "batch-1", "qualified", "live") in pushed


@pytest.mark.asyncio
async def test_scan_namespace_promotes_a_resolved_external_mention_to_cross_cluster(monkeypatch, captured):
    """ROADMAP P31 phase 1 (ADR 0037): an external-shaped hostname that
    resolves to exactly one cluster via the ingress lookup is promoted to
    target_kind="cross_cluster" with that cluster's id — and this happens
    UNCONDITIONALLY, even with mine_external_egress left at its default
    (False), since a resolved match is no longer an unvalidated guess."""
    pushed = []

    async def fake_push_dependency(ns, from_service, to_service, backend_url, token, target_kind="service",
                                    port=None, outcome=None, path="", caller_pod="", match_kind="", source="",
                                    target_cluster_id=""):
        pushed.append((from_service, to_service, target_kind, target_cluster_id))

    async def fake_resolve(host, backend_url, kinds, cache):
        assert host == "shop.apps.dc2.example.com"
        return ["cluster-b"]

    monkeypatch.setattr(discovery_main, "push_dependency", fake_push_dependency)
    monkeypatch.setattr(discovery_main, "resolve_cross_cluster_target", fake_resolve)

    services = [{"name": "batch", "selector": {"app": "batch"}}]
    pods = [_pod("batch-1", "batch")]
    logs = "calling http://shop.apps.dc2.example.com/charge now"
    await _run(monkeypatch, services, pods, {"batch-1": logs})

    assert ("batch", "shop.apps.dc2.example.com", "cross_cluster", "cluster-b") in pushed


@pytest.mark.asyncio
async def test_scan_namespace_ambiguous_cross_cluster_match_is_promoted_but_unattributed(monkeypatch, captured):
    """A hostname matching more than one cluster's ingress entries (a GSLB
    name briefly fronting both during a migration overlap) is still
    promoted to cross_cluster — but target_cluster_id stays "", never a
    guess at which one."""
    pushed = []

    async def fake_push_dependency(ns, from_service, to_service, backend_url, token, target_kind="service",
                                    port=None, outcome=None, path="", caller_pod="", match_kind="", source="",
                                    target_cluster_id=""):
        pushed.append((from_service, to_service, target_kind, target_cluster_id))

    async def fake_resolve(host, backend_url, kinds, cache):
        return ["cluster-b", "cluster-c"]

    monkeypatch.setattr(discovery_main, "push_dependency", fake_push_dependency)
    monkeypatch.setattr(discovery_main, "resolve_cross_cluster_target", fake_resolve)

    services = [{"name": "batch", "selector": {"app": "batch"}}]
    pods = [_pod("batch-1", "batch")]
    logs = "calling http://shared.example.com/charge now"
    await _run(monkeypatch, services, pods, {"batch-1": logs})

    assert ("batch", "shared.example.com", "cross_cluster", "") in pushed


@pytest.mark.asyncio
async def test_scan_namespace_unresolved_external_mention_still_respects_the_egress_gate(monkeypatch, captured):
    """When the ingress lookup finds NO match anywhere in the fleet, this is
    a genuinely unvalidated external mention — it must fall through to
    today's MINE_EXTERNAL_EGRESS gate, not become unconditionally-on just
    because a lookup was attempted."""
    pushed = []

    async def fake_push_dependency(ns, from_service, to_service, backend_url, token, target_kind="service",
                                    port=None, outcome=None, path="", caller_pod="", match_kind="", source="",
                                    target_cluster_id=""):
        pushed.append((from_service, to_service, target_kind, target_cluster_id))

    async def fake_resolve(host, backend_url, kinds, cache):
        return []

    monkeypatch.setattr(discovery_main, "push_dependency", fake_push_dependency)
    monkeypatch.setattr(discovery_main, "resolve_cross_cluster_target", fake_resolve)

    services = [{"name": "batch", "selector": {"app": "batch"}}]
    pods = [_pod("batch-1", "batch")]
    logs = "calling https://api.anthropic.com/v1/messages now"

    # Default _Cfg (mine_external_egress=False): no match anywhere in the
    # fleet, and the gate is off — nothing pushed, same as before this phase.
    await _run(monkeypatch, services, pods, {"batch-1": logs})
    assert pushed == []

    # With the gate on, the unresolved mention still pushes as plain "external".
    class _CfgEgressOn(_Cfg):
        mine_external_egress = True

    async def fake_list_services(ns):
        return services

    async def fake_list_pods(ns):
        return pods

    async def fake_get_pod_logs(ns, pod, tail_lines=200):
        return logs

    monkeypatch.setattr(discovery_main.k8s_client, "list_services", fake_list_services)
    monkeypatch.setattr(discovery_main.k8s_client, "list_pods", fake_list_pods)
    monkeypatch.setattr(discovery_main.k8s_client, "get_pod_logs", fake_get_pod_logs)
    await discovery_main._scan_namespace("payments", _CfgEgressOn())

    assert ("batch", "api.anthropic.com", "external", "") in pushed
