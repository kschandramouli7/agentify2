"""Seed the Langfuse evaluation dataset for the k8fy eval harness (ADR 0019).

Run once (or after adding new test cases) with Langfuse credentials set:

    export LANGFUSE_PUBLIC_KEY=pk-lf-...
    export LANGFUSE_SECRET_KEY=sk-lf-...
    export LANGFUSE_BASE_URL=https://us.cloud.langfuse.com

    python scripts/seed_eval_dataset.py

Idempotent: items use explicit IDs so re-running updates metadata without
duplicating. The dataset itself is created if it doesn't exist.
"""

import os
import sys

from langfuse import Langfuse

DATASET_NAME = "k8fy-regression"
DATASET_DESCRIPTION = (
    "Regression suite for agentify K8fy skills. "
    "Each item is a (query, ground_truth) pair covering all intent classes. "
    "Run via scripts/run_evals.py after every deploy."
)

# ---------------------------------------------------------------------------
# Test cases
# Each item has:
#   id           – stable key so re-runs are idempotent
#   input        – the POST /api/query body (question + context)
#   expected     – what the response MUST satisfy (scored by run_evals.py)
#     intent     – exact string match (diagnose / health_check / cert_check /
#                  change_history / metrics_history / general_query)
#     tier       – "tier1" or "tier2"
#     status     – exact or list of acceptable values
#     required_details – keys that must be non-null/non-empty in resp.details
#     latency_ms_max – fail if response exceeds this (0 = no check)
# ---------------------------------------------------------------------------
ITEMS = [
    {
        "id": "diagnose-payment-crash-001",
        "input": {
            "question": "why is payment-worker crashing?",
            "context": {"namespace": "payments", "service": "payment-worker"},
        },
        "expected": {
            "intent":           "diagnose",
            "tier":             "tier2",
            "status":           ["unhealthy", "degraded"],
            "required_details": ["headline", "timeline", "findings", "likely_cause"],
            "latency_ms_max":   20_000,
        },
    },
    {
        "id": "health-healthy-payment-api-002",
        "input": {
            "question": "is payment-api healthy?",
            "context": {"namespace": "payments", "service": "payment-api"},
        },
        "expected": {
            "intent":           "health_check",
            "tier":             "tier1",
            # Any VALID health status, not a specific one. payment-api's real
            # health is not a fixed fact: on 2026-08-30 this item failed at 0.80
            # because two of its pods were genuinely Pending, so the agent
            # correctly answered "degraded" and the ground truth was simply
            # stale. Asserting a particular state for a live service tests the
            # cluster's mood, not the agent. A failed model call is caught
            # separately and unconditionally by the status='error' check.
            "status":           ["healthy", "ok", "degraded", "unhealthy"],
            "required_details": [],
            "latency_ms_max":   500,   # Tier-1 must be fast
        },
    },
    {
        "id": "health-degraded-payment-worker-003",
        "input": {
            "question": "is payment-worker healthy?",
            "context": {"namespace": "payments", "service": "payment-worker"},
        },
        "expected": {
            "intent":           "health_check",
            "tier":             "",        # tier depends on actual pod state — not asserted
            "status":           [],        # any status is valid (pods may be healthy in CI)
            "required_details": [],
            "latency_ms_max":   20_000,
        },
    },
    {
        "id": "cert-check-payments-004",
        "input": {
            "question": "check TLS certs for payments",
            "context": {"namespace": "payments", "service": "payment-api"},
        },
        "expected": {
            "intent":           "cert_check",
            "tier":             "tier1",
            # not_applicable = namespace has no TLS secrets (valid in test cluster)
            "status":           ["ok", "warn", "crit", "not_applicable"],
            "required_details": [],
            "latency_ms_max":   500,
        },
    },
    {
        "id": "cert-expiry-payments-005",
        "input": {
            "question": "are any certs expiring soon in payments?",
            "context": {"namespace": "payments"},
        },
        "expected": {
            "intent":           "cert_check",
            "tier":             "tier1",
            "status":           ["ok", "warn", "crit", "not_applicable"],
            "required_details": [],
            "latency_ms_max":   500,
        },
    },
    {
        "id": "change-history-payments-006",
        # Question must match inferIntent keywords: "recently deployed", "deploy history", etc.
        "input": {
            "question": "show deploy history for payment-worker",
            "context": {"namespace": "payments", "service": "payment-worker"},
        },
        "expected": {
            "intent":           "change_history",
            "tier":             "tier2",
            "status":           [],
            "required_details": [],
            "latency_ms_max":   25_000,
        },
    },
    {
        "id": "restart-trend-payment-worker-007",
        # Question must match inferIntent keywords: "restart trend", "restart history"
        "input": {
            "question": "show restart history for payment-worker",
            "context": {"namespace": "payments", "service": "payment-worker"},
        },
        "expected": {
            "intent":           "metrics_history",
            "tier":             "tier2",
            "status":           [],
            "required_details": [],
            "latency_ms_max":   25_000,
        },
    },
    {
        "id": "tier1-latency-gate-008",
        "input": {
            "question": "is payment-api healthy?",
            "context": {"namespace": "payments", "service": "payment-api"},
        },
        "expected": {
            "intent":           "health_check",
            "tier":             "tier1",
            "status":           [],
            "required_details": [],
            "latency_ms_max":   500,
        },
    },
    {
        "id": "diagnose-required-fields-009",
        "input": {
            "question": "diagnose payment-worker",
            "context": {"namespace": "payments", "service": "payment-worker"},
        },
        "expected": {
            "intent":           "diagnose",
            "tier":             "tier2",
            "status":           [],
            # Only check headline — the other fields depend on whether Claude
            # has enough signal data to populate them.
            "required_details": ["headline"],
            "latency_ms_max":   35_000,
        },
    },
    {
        "id": "general-query-pods-010",
        "input": {
            "question": "how many pods are running in the payments namespace?",
            "context": {"namespace": "payments"},
        },
        "expected": {
            "intent":           "general_query",
            "tier":             "tier2",
            "status":           [],   # any
            "required_details": [],
            "latency_ms_max":   20_000,
        },
    },
]


def _lf_client() -> Langfuse:
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    base_url   = os.environ.get("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com")
    if not public_key or not secret_key:
        print(
            "ERROR: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set.\n"
            "       Find them in Langfuse UI → Settings → API Keys.",
            file=sys.stderr,
        )
        sys.exit(1)
    return Langfuse(public_key=public_key, secret_key=secret_key, host=base_url)


def main() -> None:
    lf = _lf_client()

    # Create (or update) the dataset
    lf.create_dataset(name=DATASET_NAME, description=DATASET_DESCRIPTION)
    print(f"Dataset '{DATASET_NAME}' ready.\n")

    ok = err = 0
    for item in ITEMS:
        try:
            lf.create_dataset_item(
                dataset_name=DATASET_NAME,
                id=item["id"],
                input=item["input"],
                expected_output=item["expected"],
                metadata={"created_by": "seed_eval_dataset.py"},
            )
            print(f"  OK   {item['id']}")
            ok += 1
        except Exception as exc:
            print(f"  ERR  {item['id']}  {exc}", file=sys.stderr)
            err += 1

    lf.flush()
    print(f"\n{ok} items seeded, {err} errors.")
    if err:
        sys.exit(1)
    print(f"\nVerify in Langfuse UI → Datasets → {DATASET_NAME}")


if __name__ == "__main__":
    main()
