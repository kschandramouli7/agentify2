"""Tests for log_platform.py — the Glue/Athena test-harness (ADR 0021) log
source. boto3.client is monkeypatched so no real AWS calls happen.
"""

import pytest

from k8fy import log_platform as lp


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
        header = {"Data": [{"VarCharValue": "pod_name"}, {"VarCharValue": "container_name"}, {"VarCharValue": "log"}]}
        data_rows = [{"Data": [{"VarCharValue": v} for v in row]} for row in self.rows]
        return {"ResultSet": {"Rows": [header] + data_rows}}


@pytest.mark.asyncio
async def test_query_athena_logs_rejects_invalid_namespace():
    result = await lp.query_athena_logs("payments'; DROP TABLE x; --", None, {"workgroup": "wg", "database": "db", "table": "tbl"})
    assert "error" in result
    assert "invalid namespace" in result["error"]


@pytest.mark.asyncio
async def test_query_athena_logs_rejects_invalid_pod():
    result = await lp.query_athena_logs("payments", "pod'; --", {"workgroup": "wg", "database": "db", "table": "tbl"})
    assert "error" in result
    assert "invalid pod" in result["error"]


@pytest.mark.asyncio
async def test_query_athena_logs_missing_config():
    result = await lp.query_athena_logs("payments", None, {})
    assert "error" in result
    assert "not configured" in result["error"]


@pytest.mark.asyncio
async def test_query_athena_logs_success(monkeypatch):
    fake_client = _FakeAthenaClient(rows=[
        ["payment-worker-abc", "worker", "line one"],
        ["payment-worker-abc", "worker", "line two"],
    ])
    monkeypatch.setattr(lp.boto3, "client", lambda service, region_name=None: fake_client)

    result = await lp.query_athena_logs(
        "payments", "payment-worker-abc",
        {"workgroup": "agentify-dev-log-test", "database": "agentify_dev_logs", "table": "pod_logs"},
    )

    assert result["namespace"] == "payments"
    assert result["pod"] == "payment-worker-abc"
    assert "line one" in result["logs"]
    assert "line two" in result["logs"]
    assert fake_client.started_with["workgroup"] == "agentify-dev-log-test"
    assert fake_client.started_with["database"] == "agentify_dev_logs"
    assert "kubernetes.namespace_name = 'payments'" in fake_client.started_with["query"]
    assert "kubernetes.pod_name = 'payment-worker-abc'" in fake_client.started_with["query"]


@pytest.mark.asyncio
async def test_query_athena_logs_empty_results(monkeypatch):
    fake_client = _FakeAthenaClient(rows=[])
    monkeypatch.setattr(lp.boto3, "client", lambda service, region_name=None: fake_client)

    result = await lp.query_athena_logs("payments", None, {"workgroup": "wg", "database": "db", "table": "tbl"})
    assert result == {"namespace": "payments", "pod": None, "logs": ""}


@pytest.mark.asyncio
async def test_query_athena_logs_query_failed(monkeypatch):
    fake_client = _FakeAthenaClient(query_state="FAILED", reason="table not found")
    monkeypatch.setattr(lp.boto3, "client", lambda service, region_name=None: fake_client)

    result = await lp.query_athena_logs("payments", None, {"workgroup": "wg", "database": "db", "table": "tbl"})
    assert "error" in result
    assert "table not found" in result["error"]


@pytest.mark.asyncio
async def test_query_athena_logs_boto3_exception(monkeypatch):
    def raise_error(service, region_name=None):
        raise RuntimeError("no credentials")
    monkeypatch.setattr(lp.boto3, "client", raise_error)

    result = await lp.query_athena_logs("payments", None, {"workgroup": "wg", "database": "db", "table": "tbl"})
    assert "error" in result
    assert "no credentials" in result["error"]


def test_partition_predicate_covers_requested_hours():
    pred = lp._partition_predicate(3)
    assert pred.count(" OR ") == 2  # 3 hours -> 2 ORs
    assert "year=" in pred and "month=" in pred and "day=" in pred and "hour=" in pred


@pytest.mark.parametrize("name", ["payments", "payment-worker-abc123", "a"])
def test_validate_k8s_name_accepts_valid(name):
    assert lp._validate_k8s_name(name, "namespace") is None


@pytest.mark.parametrize("name", ["Payments", "payments_test", "payments;drop", "-payments", "payments-", ""])
def test_validate_k8s_name_rejects_invalid(name):
    assert lp._validate_k8s_name(name, "namespace") is not None
