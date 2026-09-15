"""Tests for trace_search.py (ROADMAP P29) — the on-demand, cross-cluster
Athena search by trace ID or "METHOD /path". Same _FakeAthenaClient/httpx
mocking convention as test_dependency_miner.py, adapted for this module's
5-column row shape (cluster_id, namespace, pod_name, labels, log).
"""

import pytest

from k8fy import trace_search as ts


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
        header = {"Data": [{"VarCharValue": v} for v in ("cluster_id", "namespace_name", "pod_name", "labels", "log")]}
        data_rows = [{"Data": [{"VarCharValue": v} for v in row]} for row in self.rows]
        return {"ResultSet": {"Rows": [header] + data_rows}}


class _RaisingAthenaClient:
    """Proves invalid input never reaches Athena at all."""
    def start_query_execution(self, **kwargs):
        raise AssertionError("Athena must not be queried for invalid input")


_ATHENA_CFG = {"workgroup": "wg", "database": "db", "table": "tbl", "region": "us-east-1"}


# ── _classify_input ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "a1b2c3d4",
    "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",  # 32-hex, Jaeger/Zipkin-shaped
    "550e8400-e29b-41d4-a716-446655440000",  # UUID
])
def test_classify_input_recognizes_trace_ids(text):
    kind, value, err = ts._classify_input(text)
    assert kind == "trace_id" and err is None
    assert value == {"trace_id": text}


@pytest.mark.parametrize("text,method,path", [
    ("POST /charge", "POST", "/charge"),
    ("get /health", "GET", "/health"),
    ("/charge", None, "/charge"),
])
def test_classify_input_recognizes_url_paths(text, method, path):
    kind, value, err = ts._classify_input(text)
    assert kind == "url_path" and err is None
    assert value == {"method": method, "path": path}


@pytest.mark.parametrize("text", ["payments", "hello world", "", "   ", "not-a-trace-or-path"])
def test_classify_input_rejects_neither_shape(text):
    kind, value, err = ts._classify_input(text)
    assert kind is None and value is None
    assert err is not None


# ── _parse_cri_line ───────────────────────────────────────────────────────────

def test_parse_cri_line_keeps_and_parses_timestamp():
    line = "2026-07-24T22:22:26Z stdout F POST /charge 200"
    ts_, message = ts._parse_cri_line(line)
    assert ts_ is not None
    assert ts_.year == 2026 and ts_.month == 7 and ts_.day == 24
    assert message == "POST /charge 200"


def test_parse_cri_line_malformed_returns_none_timestamp():
    ts_, message = ts._parse_cri_line("not a cri line")
    assert ts_ is None
    assert message == "not a cri line"


# ── _line_matches_request_context ────────────────────────────────────────────

def test_request_context_accepts_method_prefixed_line():
    assert ts._line_matches_request_context("POST /charge 200 OK", "POST", "/charge") is True


def test_request_context_accepts_field_shaped_line():
    assert ts._line_matches_request_context('handling request path="/charge" user=42', None, "/charge") is True


def test_request_context_rejects_referer_header():
    """The exact bug class service_topology.py's docstring names: a Referer
    header mentioning the path is NOT evidence a call happened."""
    assert ts._line_matches_request_context("Referer: http://frontend.svc/charge", None, "/charge") is False


def test_request_context_rejects_quoted_error_body():
    line = 'error: could not process request, body was "visit /charge to retry"'
    assert ts._line_matches_request_context(line, None, "/charge") is False


def test_request_context_rejects_disagreeing_method():
    assert ts._line_matches_request_context("GET /charge 200 OK", "POST", "/charge") is False


# ── Query builders ────────────────────────────────────────────────────────────

def test_build_trace_id_query_has_no_namespace_or_cluster_filter():
    query = ts._build_trace_id_query("db", "tbl", "a1b2c3d4", 24, 500)
    assert "cluster_id = " not in query
    assert "namespace_name = " not in query
    assert "log LIKE '%a1b2c3d4%'" in query


def test_build_url_path_query_has_no_namespace_or_cluster_filter():
    query = ts._build_url_path_query("db", "tbl", "/charge", 24, 500)
    assert "cluster_id = " not in query
    assert "namespace_name = " not in query
    assert "log LIKE '%/charge%'" in query


# ── search_trace end to end ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_trace_rejects_invalid_input_before_any_athena_call(monkeypatch):
    monkeypatch.setattr(ts.boto3, "client", lambda service, region_name=None: _RaisingAthenaClient())
    result = await ts.search_trace("not valid", "http://backend", _ATHENA_CFG)
    assert result["error_kind"] == "invalid_input"


@pytest.mark.asyncio
async def test_search_trace_matches_by_trace_id(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {"payment-api": {"app": "payment-api"}}

    fake_client = _FakeAthenaClient(rows=[
        ["cluster-a", "payments", "payment-api-abc", "{app=payment-api}",
         "2026-07-24T22:22:26Z stdout F handling trace=a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"],
    ])
    monkeypatch.setattr(ts, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(ts.boto3, "client", lambda service, region_name=None: fake_client)

    result = await ts.search_trace("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", "http://backend", _ATHENA_CFG)

    assert result["kind"] == "trace_id"
    assert len(result["hops"]) == 1
    assert result["hops"][0]["service"] == "payment-api"
    assert result["hops"][0]["seq"] == 1


@pytest.mark.asyncio
async def test_search_trace_url_path_excludes_non_call_context_decoy(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {"payment-api": {"app": "payment-api"}}

    fake_client = _FakeAthenaClient(rows=[
        # Real call.
        ["cluster-a", "payments", "payment-api-abc", "{app=payment-api}",
         "2026-07-24T22:22:26Z stdout F POST /charge 200"],
        # Decoy: mentions the path in a Referer header, not a call.
        ["cluster-a", "payments", "payment-api-abc", "{app=payment-api}",
         "2026-07-24T22:22:27Z stdout F Referer: http://frontend.svc/charge"],
    ])
    monkeypatch.setattr(ts, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(ts.boto3, "client", lambda service, region_name=None: fake_client)

    result = await ts.search_trace("POST /charge", "http://backend", _ATHENA_CFG)

    assert len(result["hops"]) == 1
    assert "200" in result["hops"][0]["log_excerpt"]


@pytest.mark.asyncio
async def test_search_trace_orders_chronologically_across_clusters(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        if cluster_id == "cluster-a":
            return {"frontend": {"app": "frontend"}}
        return {"payment-api": {"app": "payment-api"}}

    # Athena returns cluster-b's (later) row BEFORE cluster-a's (earlier) row.
    fake_client = _FakeAthenaClient(rows=[
        ["cluster-b", "payments", "payment-api-abc", "{app=payment-api}",
         "2026-07-24T22:22:30Z stdout F POST /charge 200"],
        ["cluster-a", "payments", "frontend-abc", "{app=frontend}",
         "2026-07-24T22:22:26Z stdout F POST /charge 200"],
    ])
    monkeypatch.setattr(ts, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(ts.boto3, "client", lambda service, region_name=None: fake_client)

    result = await ts.search_trace("POST /charge", "http://backend", _ATHENA_CFG)

    assert [h["service"] for h in result["hops"]] == ["frontend", "payment-api"]
    assert [h["seq"] for h in result["hops"]] == [1, 2]


@pytest.mark.asyncio
async def test_search_trace_redacts_log_excerpts(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {}

    fake_client = _FakeAthenaClient(rows=[
        ["cluster-a", "payments", "payment-api-abc", "{app=payment-api}",
         "2026-07-24T22:22:26Z stdout F POST /charge password=hunter2secret"],
    ])
    monkeypatch.setattr(ts, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(ts.boto3, "client", lambda service, region_name=None: fake_client)

    result = await ts.search_trace("POST /charge", "http://backend", _ATHENA_CFG)

    assert "hunter2secret" not in result["hops"][0]["log_excerpt"]
    assert result["hops"][0]["service"] is None
    assert result["unattributed_count"] == 1


@pytest.mark.asyncio
async def test_search_trace_athena_error_is_reported(monkeypatch):
    fake_client = _FakeAthenaClient(query_state="FAILED", reason="table not found")
    monkeypatch.setattr(ts.boto3, "client", lambda service, region_name=None: fake_client)

    result = await ts.search_trace("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", "http://backend", _ATHENA_CFG)

    assert result["error_kind"] == "query_failed"


@pytest.mark.asyncio
async def test_search_trace_empty_result_is_not_an_error(monkeypatch):
    fake_client = _FakeAthenaClient(rows=[])
    monkeypatch.setattr(ts.boto3, "client", lambda service, region_name=None: fake_client)

    result = await ts.search_trace("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", "http://backend", _ATHENA_CFG)

    assert "error" not in result
    assert result["hops"] == []


@pytest.mark.asyncio
async def test_search_trace_fetches_selectors_only_for_pairs_that_actually_matched(monkeypatch):
    """3 registered clusters exist in principle, but the Athena rows only
    touch 1 (cluster, namespace) pair — selectors must be fetched exactly
    once, not once per registered cluster."""
    call_count = {"n": 0}

    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        call_count["n"] += 1
        return {"payment-api": {"app": "payment-api"}}

    fake_client = _FakeAthenaClient(rows=[
        ["cluster-a", "payments", "payment-api-abc", "{app=payment-api}",
         "2026-07-24T22:22:26Z stdout F POST /charge 200"],
        ["cluster-a", "payments", "payment-api-def", "{app=payment-api}",
         "2026-07-24T22:22:27Z stdout F POST /charge 200"],
    ])
    monkeypatch.setattr(ts, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(ts.boto3, "client", lambda service, region_name=None: fake_client)

    await ts.search_trace("POST /charge", "http://backend", _ATHENA_CFG)

    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_search_trace_drops_rows_with_unparseable_timestamp(monkeypatch):
    async def fake_fetch_selectors(backend_url, cluster_id, namespace):
        return {"payment-api": {"app": "payment-api"}}

    fake_client = _FakeAthenaClient(rows=[
        ["cluster-a", "payments", "payment-api-abc", "{app=payment-api}", "not a cri line at all"],
    ])
    monkeypatch.setattr(ts, "_fetch_selectors", fake_fetch_selectors)
    monkeypatch.setattr(ts.boto3, "client", lambda service, region_name=None: fake_client)

    result = await ts.search_trace("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", "http://backend", _ATHENA_CFG)

    assert result["hops"] == []
    assert result["dropped_unparseable_timestamp_count"] == 1


@pytest.mark.asyncio
async def test_search_trace_reports_unconfigured_athena():
    result = await ts.search_trace(
        "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", "http://backend",
        {"workgroup": "", "database": "", "table": ""},
    )
    assert result["error_kind"] == "query_failed"
