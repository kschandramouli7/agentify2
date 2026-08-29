"""Tests for log_router.py — the ONE place that decides whether a pod's logs
come from the live cluster or the Glue/Athena log-platform test harness
(ADR 0021). No registry: when the log platform is configured, it's tried
first for every namespace; falls back to the live cluster on empty results,
errors, or when unconfigured. The load-bearing property: this decision is
never exposed to the model — get_logs() is a single function whichever way
it's called (Claude tool or DiagnoseSkill's plain prefetch).
"""

from types import SimpleNamespace

import pytest

from k8fy import log_router as lr


def _settings(workgroup="wg", database="db", table="tbl", region="ap-southeast-2"):
    return SimpleNamespace(
        athena_workgroup=workgroup, athena_database=database, athena_table=table, aws_region=region,
    )


@pytest.mark.asyncio
async def test_get_logs_uses_athena_when_configured_and_has_data(monkeypatch):
    monkeypatch.setattr(lr, "get_settings", lambda: _settings())

    captured = {}
    async def fake_query_athena_logs(namespace, pod, athena_config, **kwargs):
        captured["namespace"] = namespace
        captured["pod"] = pod
        captured["athena_config"] = athena_config
        return {"namespace": namespace, "pod": pod, "logs": "athena logs here"}

    async def fake_live_get_pod_logs(*args, **kwargs):
        raise AssertionError("should not call the live cluster when Athena has data")

    monkeypatch.setattr(lr, "query_athena_logs", fake_query_athena_logs)
    monkeypatch.setattr(lr, "live_get_pod_logs", fake_live_get_pod_logs)

    result = await lr.get_logs("payments", "payment-worker-abc")

    assert result["logs"] == "athena logs here"
    assert captured["namespace"] == "payments"
    assert captured["pod"] == "payment-worker-abc"
    assert captured["athena_config"] == {
        "workgroup": "wg", "database": "db", "table": "tbl", "region": "ap-southeast-2",
    }


@pytest.mark.asyncio
async def test_get_logs_falls_back_to_cluster_when_athena_empty(monkeypatch):
    monkeypatch.setattr(lr, "get_settings", lambda: _settings())

    async def fake_query_athena_logs(namespace, pod, athena_config, **kwargs):
        return {"namespace": namespace, "pod": pod, "logs": ""}

    captured = {}
    async def fake_live_get_pod_logs(namespace, pod, container=None, tail_lines=200, previous=False):
        captured.update(namespace=namespace, pod=pod, previous=previous)
        return {"namespace": namespace, "pod": pod, "logs": "live cluster logs"}

    monkeypatch.setattr(lr, "query_athena_logs", fake_query_athena_logs)
    monkeypatch.setattr(lr, "live_get_pod_logs", fake_live_get_pod_logs)

    result = await lr.get_logs("payments", "payment-worker-abc", previous=True)

    assert result["logs"] == "live cluster logs"
    assert captured["previous"] is True


@pytest.mark.asyncio
async def test_get_logs_falls_back_to_cluster_when_athena_errors(monkeypatch):
    monkeypatch.setattr(lr, "get_settings", lambda: _settings())

    async def fake_query_athena_logs(*args, **kwargs):
        return {"error": "Athena query failed: timeout"}

    async def fake_live_get_pod_logs(namespace, pod, **kwargs):
        return {"namespace": namespace, "pod": pod, "logs": "fallback logs"}

    monkeypatch.setattr(lr, "query_athena_logs", fake_query_athena_logs)
    monkeypatch.setattr(lr, "live_get_pod_logs", fake_live_get_pod_logs)

    result = await lr.get_logs("payments", "pod-x")
    assert result["logs"] == "fallback logs"


@pytest.mark.asyncio
async def test_get_logs_skips_athena_entirely_when_unconfigured(monkeypatch):
    monkeypatch.setattr(lr, "get_settings", lambda: _settings(workgroup="", database="", table=""))

    async def fake_query_athena_logs(*args, **kwargs):
        raise AssertionError("should not call Athena at all when unconfigured")

    async def fake_live_get_pod_logs(namespace, pod, **kwargs):
        return {"namespace": namespace, "pod": pod, "logs": "cluster logs"}

    monkeypatch.setattr(lr, "query_athena_logs", fake_query_athena_logs)
    monkeypatch.setattr(lr, "live_get_pod_logs", fake_live_get_pod_logs)

    result = await lr.get_logs("payments", "pod-x")
    assert result["logs"] == "cluster logs"
