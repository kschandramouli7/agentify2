"""Tests for splunk_dependency_miner.py (ROADMAP P15, ADR 0042). Mocked at
the same level test_dependency_miner.py mocks its Athena fetch (the whole
_run_query call, not each individual HTTP step — that three-call flow is
already covered by test_log_platform_splunk.py). Cluster-registry/selector/
push helpers are reused from dependency_miner.py via import, so they're
monkeypatched on splunk_dependency_miner's own bound names, not
dependency_miner's — `from X import Y` binds a separate reference.
"""

import pytest

from k8fy import splunk_dependency_miner as sdm

_SPLUNK_CONFIG = {"url": "https://splunk.example.com:8089", "index": "k8s_logs", "token": "fake-token"}


# ── Pure functions ────────────────────────────────────────────────────────────

def test_build_namespace_spl_has_no_pod_filter():
    """Unlike log_platform_splunk.py's single-pod _build_spl, this scans
    every pod in the namespace in one search — the miner's own unit of
    work, matching dependency_miner.py's _mine_namespace."""
    spl = sdm._build_namespace_spl("k8s_logs", "payments", 500)
    assert "index=k8s_logs" in spl
    assert 'kubernetes.namespace_name="payments"' in spl
    assert "kubernetes.pod_name" not in spl  # no pod filter at all
    assert "| head 500" in spl


def test_build_namespace_spl_requests_all_fields():
    # Confirmed live: without "| table *", Splunk's JSON results API
    # silently drops kubernetes.pod_name/kubernetes.labels.*/
    # kubernetes.container_name from every row (they're never named
    # elsewhere in this query), so _mine_namespace's by_pod grouping sees
    # an empty pod_name for every row and pushes nothing — no exception,
    # no log line, just zero edges. See log_platform_splunk.py's sibling
    # test for the same regression on the single-pod query.
    spl = sdm._build_namespace_spl("k8s_logs", "payments", 500)
    assert spl.endswith("| table *")


def test_extract_labels_strips_prefix():
    row = {
        "kubernetes.pod_name": "payment-worker-abc",
        "kubernetes.labels.app": "payment-worker",
        "kubernetes.labels.tier": "backend",
        "_raw": "some log line",
    }
    assert sdm._extract_labels(row) == {"app": "payment-worker", "tier": "backend"}


def test_extract_labels_ignores_non_label_fields():
    row = {"kubernetes.pod_name": "pod-abc", "_raw": "text"}
    assert sdm._extract_labels(row) == {}


# ── _mine_namespace orchestration ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mine_namespace_end_to_end(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {"payment-worker": {"app": "payment-worker"}, "payment-api": {"app": "payment-api"}}

    async def fake_run_query(client, splunk_url, token, spl, hours_back, result_limit=None):
        return {"rows": [
            {"kubernetes.pod_name": "payment-worker-abc", "kubernetes.labels.app": "payment-worker",
             "_raw": "GET payment-api.payments.svc.cluster.local"},
        ]}

    monkeypatch.setattr(sdm, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(sdm, "_run_query", fake_run_query)

    pushed = []

    async def fake_push_edge(backend_url, cluster_id, namespace, from_service, to_service,
                              port=None, outcome=None, path="", caller_pod="", match_kind="", source=""):
        pushed.append((cluster_id, namespace, from_service, to_service, port, outcome, path, caller_pod,
                        match_kind, source))

    monkeypatch.setattr(sdm, "_push_edge", fake_push_edge)

    await sdm._mine_namespace("http://backend", _SPLUNK_CONFIG, "cluster-a", "payments", hours_back=2)

    # Same reading as dependency_miner.py's equivalent test: the FQDN form
    # carries no port, no classifiable outcome, no trailing path — source
    # is this miner's own fixed identity, "splunk", not "glue".
    assert pushed == [
        ("cluster-a", "payments", "payment-worker", "payment-api", 0, None, "", "payment-worker-abc",
         "qualified", "splunk")
    ]


@pytest.mark.asyncio
async def test_mine_namespace_pushes_service_labels_only_when_tracked_label_keys_given(monkeypatch):
    # ROADMAP P32 (ADR 0043): mirrors dependency_miner.py's own equivalent
    # test. _push_service_labels is imported from dependency_miner.py into
    # this module's own namespace, so it's monkeypatched on `sdm`, not
    # `dm` -- same "from X import Y binds a separate reference" discipline
    # this file's own module docstring already names for the other shared
    # helpers.
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {"payment-worker": {"app": "payment-worker"}, "payment-api": {"app": "payment-api"}}

    async def fake_run_query(client, splunk_url, token, spl, hours_back, result_limit=None):
        return {"rows": [
            {"kubernetes.pod_name": "payment-worker-abc", "kubernetes.labels.app": "payment-worker",
             "_raw": "GET payment-api.payments.svc.cluster.local"},
        ]}

    async def fake_push_edge(*args, **kwargs):
        pass

    monkeypatch.setattr(sdm, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(sdm, "_run_query", fake_run_query)
    monkeypatch.setattr(sdm, "_push_edge", fake_push_edge)

    label_calls = []

    async def fake_push_service_labels(backend_url, cluster_id, namespace, service, labels, tracked_keys):
        label_calls.append((cluster_id, namespace, service, labels, tracked_keys))

    monkeypatch.setattr(sdm, "_push_service_labels", fake_push_service_labels)

    await sdm._mine_namespace("http://backend", _SPLUNK_CONFIG, "cluster-a", "payments", hours_back=2)
    assert label_calls == []  # no tracked_label_keys -- must not call it at all

    await sdm._mine_namespace(
        "http://backend", _SPLUNK_CONFIG, "cluster-a", "payments", hours_back=2, tracked_label_keys={"apm-id"},
    )
    assert label_calls == [("cluster-a", "payments", "payment-worker", {"app": "payment-worker"}, {"apm-id"})]


@pytest.mark.asyncio
async def test_mine_namespace_pushes_each_edge_at_most_once_per_cycle(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {"payment-worker": {"app": "payment-worker"}, "payment-api": {"app": "payment-api"}}

    async def fake_run_query(client, splunk_url, token, spl, hours_back, result_limit=None):
        return {"rows": [
            {"kubernetes.pod_name": "payment-worker-abc", "kubernetes.labels.app": "payment-worker",
             "_raw": "GET payment-api.payments.svc.cluster.local"},
            {"kubernetes.pod_name": "payment-worker-abc", "kubernetes.labels.app": "payment-worker",
             "_raw": "GET payment-api.payments.svc.cluster.local"},
        ]}

    monkeypatch.setattr(sdm, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(sdm, "_run_query", fake_run_query)

    pushed = []

    async def fake_push_edge(backend_url, cluster_id, namespace, from_service, to_service, **kwargs):
        pushed.append((from_service, to_service))

    monkeypatch.setattr(sdm, "_push_edge", fake_push_edge)

    await sdm._mine_namespace("http://backend", _SPLUNK_CONFIG, "cluster-a", "payments", hours_back=2)

    assert pushed == [("payment-worker", "payment-api")]


@pytest.mark.asyncio
async def test_mine_namespace_pushes_once_per_distinct_contributing_pod(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {"payment-worker": {"app": "payment-worker"}, "payment-api": {"app": "payment-api"}}

    async def fake_run_query(client, splunk_url, token, spl, hours_back, result_limit=None):
        return {"rows": [
            {"kubernetes.pod_name": "payment-worker-abc", "kubernetes.labels.app": "payment-worker",
             "_raw": "GET payment-api.payments.svc.cluster.local"},
            {"kubernetes.pod_name": "payment-worker-xyz", "kubernetes.labels.app": "payment-worker",
             "_raw": "GET payment-api.payments.svc.cluster.local"},
        ]}

    monkeypatch.setattr(sdm, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(sdm, "_run_query", fake_run_query)

    pushed_pods = []

    async def fake_push_edge(backend_url, cluster_id, namespace, from_service, to_service,
                              port=None, outcome=None, path="", caller_pod="", match_kind="", source=""):
        pushed_pods.append(caller_pod)

    monkeypatch.setattr(sdm, "_push_edge", fake_push_edge)

    await sdm._mine_namespace("http://backend", _SPLUNK_CONFIG, "cluster-a", "payments", hours_back=2)

    assert sorted(pushed_pods) == ["payment-worker-abc", "payment-worker-xyz"]


@pytest.mark.asyncio
async def test_mine_namespace_skips_when_no_selectors(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {}

    called = {"query": False}

    async def fake_run_query(*args, **kwargs):
        called["query"] = True
        raise AssertionError("should not query Splunk with no known services")

    monkeypatch.setattr(sdm, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(sdm, "_run_query", fake_run_query)

    await sdm._mine_namespace("http://backend", _SPLUNK_CONFIG, "cluster-a", "payments", hours_back=2)

    assert called["query"] is False


@pytest.mark.asyncio
async def test_mine_namespace_unmatched_pod_labels_are_skipped(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {"payment-api": {"app": "payment-api"}}

    async def fake_run_query(client, splunk_url, token, spl, hours_back, result_limit=None):
        return {"rows": [
            {"kubernetes.pod_name": "unknown-pod-abc", "kubernetes.labels.app": "some-other-app",
             "_raw": "GET payment-api.payments.svc.cluster.local"},
        ]}

    monkeypatch.setattr(sdm, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(sdm, "_run_query", fake_run_query)

    pushed = []

    async def fake_push_edge(*args, **kwargs):
        pushed.append(args)

    monkeypatch.setattr(sdm, "_push_edge", fake_push_edge)

    await sdm._mine_namespace("http://backend", _SPLUNK_CONFIG, "cluster-a", "payments", hours_back=2)

    assert pushed == []


@pytest.mark.asyncio
async def test_mine_namespace_query_error_does_not_raise(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {"payment-api": {"app": "payment-api"}}

    async def fake_run_query(client, splunk_url, token, spl, hours_back, result_limit=None):
        return {"error": "Splunk search job job-1 failed"}

    monkeypatch.setattr(sdm, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(sdm, "_run_query", fake_run_query)

    # Must not raise.
    await sdm._mine_namespace("http://backend", _SPLUNK_CONFIG, "cluster-a", "payments", hours_back=2)


@pytest.mark.asyncio
async def test_mine_namespace_rows_missing_pod_name_are_skipped(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {"payment-api": {"app": "payment-api"}}

    async def fake_run_query(client, splunk_url, token, spl, hours_back, result_limit=None):
        return {"rows": [{"kubernetes.labels.app": "payment-api", "_raw": "no pod name on this row"}]}

    monkeypatch.setattr(sdm, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(sdm, "_run_query", fake_run_query)

    pushed = []

    async def fake_push_edge(*args, **kwargs):
        pushed.append(args)

    monkeypatch.setattr(sdm, "_push_edge", fake_push_edge)

    await sdm._mine_namespace("http://backend", _SPLUNK_CONFIG, "cluster-a", "payments", hours_back=2)

    assert pushed == []


# ── run_once orchestration ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_once_skips_entirely_when_splunk_unconfigured(monkeypatch):
    called = {"clusters": False}

    async def fake_fetch_clusters(backend_url):
        called["clusters"] = True
        return []

    monkeypatch.setattr(sdm, "_fetch_registered_clusters", fake_fetch_clusters)

    await sdm.run_once("http://backend", {"url": "", "index": "", "token": ""})

    assert called["clusters"] is False


@pytest.mark.asyncio
async def test_run_once_mines_every_cluster_namespace_pair(monkeypatch):
    async def fake_fetch_clusters(backend_url):
        return [
            {"id": "cluster-a", "namespaces": ["payments", "checkout"]},
            {"id": "cluster-b", "namespaces": ["payments"]},
        ]

    mined = []

    async def fake_mine_namespace(backend_url, splunk_config, cluster_id, namespace, hours_back, tracked_label_keys=None):
        mined.append((cluster_id, namespace))

    monkeypatch.setattr(sdm, "_fetch_registered_clusters", fake_fetch_clusters)
    monkeypatch.setattr(sdm, "_mine_namespace", fake_mine_namespace)

    await sdm.run_once("http://backend", _SPLUNK_CONFIG)

    assert set(mined) == {("cluster-a", "payments"), ("cluster-a", "checkout"), ("cluster-b", "payments")}


@pytest.mark.asyncio
async def test_run_once_one_namespace_failure_does_not_block_the_others(monkeypatch):
    async def fake_fetch_clusters(backend_url):
        return [{"id": "cluster-a", "namespaces": ["broken", "payments"]}]

    mined = []

    async def fake_mine_namespace(backend_url, splunk_config, cluster_id, namespace, hours_back, tracked_label_keys=None):
        if namespace == "broken":
            raise RuntimeError("boom")
        mined.append(namespace)

    monkeypatch.setattr(sdm, "_fetch_registered_clusters", fake_fetch_clusters)
    monkeypatch.setattr(sdm, "_mine_namespace", fake_mine_namespace)

    await sdm.run_once("http://backend", _SPLUNK_CONFIG)  # must not raise

    assert mined == ["payments"]


@pytest.mark.asyncio
async def test_run_once_skips_clusters_with_no_id(monkeypatch):
    async def fake_fetch_clusters(backend_url):
        return [{"id": "", "namespaces": ["payments"]}]

    called = {"mine": False}

    async def fake_mine_namespace(*args, **kwargs):
        called["mine"] = True

    monkeypatch.setattr(sdm, "_fetch_registered_clusters", fake_fetch_clusters)
    monkeypatch.setattr(sdm, "_mine_namespace", fake_mine_namespace)

    await sdm.run_once("http://backend", _SPLUNK_CONFIG)

    assert called["mine"] is False
