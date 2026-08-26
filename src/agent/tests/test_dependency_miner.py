"""Tests for dependency_miner.py (ADR 0029) — the Glue-based dependency
miner that runs centrally in the Agent process. boto3 is monkeypatched
(same _FakeAthenaClient convention as test_log_platform.py) so no real AWS
calls happen; Hub calls use httpx.MockTransport (same convention as
test_remote_live_fetch.py / test_inventory.py).
"""

import json

import httpx
import pytest

from k8fy import dependency_miner as dm

_RealAsyncClient = httpx.AsyncClient


def _client_factory(transport: httpx.MockTransport):
    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = transport
        return _RealAsyncClient(**kwargs)
    return factory


class _FakeAthenaClient:
    def __init__(self, query_state="SUCCEEDED", rows=None, reason=""):
        self.query_state = query_state
        self.rows = rows if rows is not None else []
        self.reason = reason
        self.started_with = None

    def start_query_execution(self, QueryString, QueryExecutionContext, WorkGroup):
        self.started_with = {"query": QueryString, "database": QueryExecutionContext["Database"], "workgroup": WorkGroup}
        return {"QueryExecutionId": "fake-query-id"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": self.query_state, "StateChangeReason": self.reason}}}

    def get_query_results(self, QueryExecutionId, MaxResults):
        header = {"Data": [{"VarCharValue": "pod_name"}, {"VarCharValue": "labels"}, {"VarCharValue": "log"}]}
        data_rows = [{"Data": [{"VarCharValue": v} for v in row]} for row in self.rows]
        return {"ResultSet": {"Rows": [header] + data_rows}}


# ── Pure functions ────────────────────────────────────────────────────────────

def test_parse_cri_message_strips_timestamp_stream_tag():
    line = "2026-07-24T22:22:26Z stdout F GET payment-api.payments.svc.cluster.local"
    assert dm._parse_cri_message(line) == "GET payment-api.payments.svc.cluster.local"


def test_parse_cri_message_returns_unchanged_when_malformed():
    assert dm._parse_cri_message("not a cri line") == "not a cri line"


@pytest.mark.parametrize("raw,expected", [
    ("{app=payment-api, tier=backend}", {"app": "payment-api", "tier": "backend"}),
    ("{app=payment-api}", {"app": "payment-api"}),
    ("{}", {}),
    ("", {}),
    ("NULL", {}),
])
def test_parse_athena_map(raw, expected):
    assert dm._parse_athena_map(raw) == expected


def test_service_for_labels_matches_full_selector():
    selectors = {"payment-backend": {"app": "payment-backend", "tier": "backend"}}
    assert dm._service_for_labels({"app": "payment-backend", "tier": "backend"}, selectors) == "payment-backend"


def test_service_for_labels_requires_full_selector_satisfaction():
    selectors = {"payment-backend": {"app": "payment-backend", "tier": "backend"}}
    # Missing "tier" -> not a match, even though "app" matches.
    assert dm._service_for_labels({"app": "payment-backend"}, selectors) is None


def test_service_for_labels_empty_selector_never_matches():
    selectors = {"manually-managed": {}}
    assert dm._service_for_labels({"app": "anything"}, selectors) is None


def test_service_for_labels_no_match_returns_none():
    selectors = {"payment-backend": {"app": "payment-backend"}}
    assert dm._service_for_labels({"app": "payment-ui"}, selectors) is None


def test_partition_predicate_covers_requested_hours():
    pred = dm._partition_predicate(3)
    assert pred.count(" OR ") == 2
    assert "year=" in pred and "month=" in pred and "day=" in pred and "hour=" in pred


def test_build_query_includes_cluster_id_and_namespace_filters():
    query = dm._build_query("db", "tbl", "cluster-a", "payments", 2, 500)
    assert "cluster_id = 'cluster-a'" in query
    assert "kubernetes.namespace_name = 'payments'" in query
    assert "kubernetes.labels AS labels" in query


# ── Hub-facing helpers (httpx) ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_registered_clusters_parses_response(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://backend/admin/integrations"
        return httpx.Response(200, json=[{"id": "cluster-a", "namespaces": ["payments"]}])

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    clusters = await dm._fetch_registered_clusters("http://backend")
    assert clusters == [{"id": "cluster-a", "namespaces": ["payments"]}]


@pytest.mark.asyncio
async def test_fetch_registered_clusters_degrades_to_empty_on_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    assert await dm._fetch_registered_clusters("http://backend") == []


@pytest.mark.asyncio
async def test_fetch_selectors_scopes_by_cluster_and_namespace(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"selectors": {"payment-api": {"app": "payment-api"}}})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    selectors = await dm._fetch_selectors("http://backend", "cluster-a", "payments")
    assert selectors == {"payment-api": {"app": "payment-api"}}
    assert "cluster_id=cluster-a" in captured["url"]
    assert "namespace=payments" in captured["url"]


@pytest.mark.asyncio
async def test_push_edge_sends_no_bearer_token_and_includes_cluster_id(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    await dm._push_edge("http://backend", "cluster-a", "payments", "payment-worker", "payment-api")

    assert captured["auth"] is None
    assert captured["body"] == {
        "namespace": "payments", "from_service": "payment-worker",
        "to_service": "payment-api", "cluster_id": "cluster-a",
    }


@pytest.mark.asyncio
async def test_push_edge_degrades_silently_on_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))

    await dm._push_edge("http://backend", "cluster-a", "payments", "payment-worker", "payment-api")  # must not raise


# ── _mine_namespace orchestration ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mine_namespace_end_to_end(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {
            "payment-worker": {"app": "payment-worker"},
            "payment-api": {"app": "payment-api"},
        }

    fake_client = _FakeAthenaClient(rows=[
        ["payment-worker-abc", "{app=payment-worker}", "2026-07-24T22:22:26Z stdout F GET payment-api.payments.svc.cluster.local"],
    ])
    monkeypatch.setattr(dm, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(dm.boto3, "client", lambda service: fake_client)

    pushed = []

    async def fake_push_edge(backend_url, cluster_id, namespace, from_service, to_service):
        pushed.append((cluster_id, namespace, from_service, to_service))

    monkeypatch.setattr(dm, "_push_edge", fake_push_edge)

    await dm._mine_namespace(
        "http://backend", {"workgroup": "wg", "database": "db", "table": "tbl"}, "cluster-a", "payments", hours_back=2,
    )

    assert pushed == [("cluster-a", "payments", "payment-worker", "payment-api")]
    assert "cluster_id = 'cluster-a'" in fake_client.started_with["query"]


@pytest.mark.asyncio
async def test_mine_namespace_pushes_each_edge_at_most_once_per_cycle(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {"payment-worker": {"app": "payment-worker"}, "payment-api": {"app": "payment-api"}}

    # Two log lines from the same pod both mention payment-api — should
    # only push the edge once (evidence_count accumulates Hub-side already).
    fake_client = _FakeAthenaClient(rows=[
        ["payment-worker-abc", "{app=payment-worker}", "2026-07-24T22:22:26Z stdout F GET payment-api.payments.svc.cluster.local"],
        ["payment-worker-abc", "{app=payment-worker}", "2026-07-24T22:22:27Z stdout F GET payment-api.payments.svc.cluster.local"],
    ])
    monkeypatch.setattr(dm, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(dm.boto3, "client", lambda service: fake_client)

    pushed = []

    async def fake_push_edge(backend_url, cluster_id, namespace, from_service, to_service):
        pushed.append((from_service, to_service))

    monkeypatch.setattr(dm, "_push_edge", fake_push_edge)

    await dm._mine_namespace("http://backend", {"workgroup": "wg", "database": "db", "table": "tbl"}, "cluster-a", "payments", hours_back=2)

    assert pushed == [("payment-worker", "payment-api")]


@pytest.mark.asyncio
async def test_mine_namespace_skips_when_no_selectors(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {}

    called = {"athena": False}

    def fail_if_called(service):
        called["athena"] = True
        raise AssertionError("should not query Athena with no known services")

    monkeypatch.setattr(dm, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(dm.boto3, "client", fail_if_called)

    await dm._mine_namespace("http://backend", {"workgroup": "wg", "database": "db", "table": "tbl"}, "cluster-a", "payments", hours_back=2)

    assert called["athena"] is False


@pytest.mark.asyncio
async def test_mine_namespace_unmatched_pod_labels_are_skipped(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {"payment-api": {"app": "payment-api"}}

    # Pod's labels don't match any known selector -> from_service unresolved.
    fake_client = _FakeAthenaClient(rows=[
        ["unknown-pod-abc", "{app=some-other-app}", "2026-07-24T22:22:26Z stdout F GET payment-api.payments.svc.cluster.local"],
    ])
    monkeypatch.setattr(dm, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(dm.boto3, "client", lambda service: fake_client)

    pushed = []

    async def fake_push_edge(*args):
        pushed.append(args)

    monkeypatch.setattr(dm, "_push_edge", fake_push_edge)

    await dm._mine_namespace("http://backend", {"workgroup": "wg", "database": "db", "table": "tbl"}, "cluster-a", "payments", hours_back=2)

    assert pushed == []


@pytest.mark.asyncio
async def test_mine_namespace_athena_query_failure_does_not_raise(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {"payment-api": {"app": "payment-api"}}

    fake_client = _FakeAthenaClient(query_state="FAILED", reason="table not found")
    monkeypatch.setattr(dm, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(dm.boto3, "client", lambda service: fake_client)

    # Must not raise.
    await dm._mine_namespace("http://backend", {"workgroup": "wg", "database": "db", "table": "tbl"}, "cluster-a", "payments", hours_back=2)


# ── run_once orchestration ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_once_skips_entirely_when_athena_unconfigured(monkeypatch):
    called = {"clusters": False}

    async def fake_fetch_clusters(backend_url):
        called["clusters"] = True
        return []

    monkeypatch.setattr(dm, "_fetch_registered_clusters", fake_fetch_clusters)

    await dm.run_once("http://backend", {"workgroup": "", "database": "", "table": ""})

    assert called["clusters"] is False


@pytest.mark.asyncio
async def test_run_once_mines_every_cluster_namespace_pair(monkeypatch):
    async def fake_fetch_clusters(backend_url):
        return [
            {"id": "cluster-a", "namespaces": ["payments", "checkout"]},
            {"id": "cluster-b", "namespaces": ["payments"]},
        ]

    mined = []

    async def fake_mine_namespace(backend_url, athena_config, cluster_id, namespace, hours_back):
        mined.append((cluster_id, namespace))

    monkeypatch.setattr(dm, "_fetch_registered_clusters", fake_fetch_clusters)
    monkeypatch.setattr(dm, "_mine_namespace", fake_mine_namespace)

    await dm.run_once("http://backend", {"workgroup": "wg", "database": "db", "table": "tbl"})

    assert set(mined) == {("cluster-a", "payments"), ("cluster-a", "checkout"), ("cluster-b", "payments")}


@pytest.mark.asyncio
async def test_run_once_one_namespace_failure_does_not_block_the_others(monkeypatch):
    async def fake_fetch_clusters(backend_url):
        return [{"id": "cluster-a", "namespaces": ["broken", "payments"]}]

    mined = []

    async def fake_mine_namespace(backend_url, athena_config, cluster_id, namespace, hours_back):
        if namespace == "broken":
            raise RuntimeError("boom")
        mined.append(namespace)

    monkeypatch.setattr(dm, "_fetch_registered_clusters", fake_fetch_clusters)
    monkeypatch.setattr(dm, "_mine_namespace", fake_mine_namespace)

    await dm.run_once("http://backend", {"workgroup": "wg", "database": "db", "table": "tbl"})  # must not raise

    assert mined == ["payments"]


@pytest.mark.asyncio
async def test_run_once_skips_clusters_with_no_id(monkeypatch):
    async def fake_fetch_clusters(backend_url):
        return [{"id": "", "namespaces": ["payments"]}]

    called = {"mine": False}

    async def fake_mine_namespace(*args, **kwargs):
        called["mine"] = True

    monkeypatch.setattr(dm, "_fetch_registered_clusters", fake_fetch_clusters)
    monkeypatch.setattr(dm, "_mine_namespace", fake_mine_namespace)

    await dm.run_once("http://backend", {"workgroup": "wg", "database": "db", "table": "tbl"})

    assert called["mine"] is False
