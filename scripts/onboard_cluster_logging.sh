#!/usr/bin/env bash
# onboard_cluster_logging.sh — apply the Fargate-logging ConfigMap to one
# cluster onboarded to the shared P15 test log pipeline (ADR 0021).
#
# Why this exists instead of a Terraform kubernetes_config_map resource:
# Terraform's kubernetes/helm provider *connections* are static in HCL — there
# is no supported way to loop a provider connection across multiple clusters'
# API servers the way ordinary resources loop via for_each. Every other piece
# of this pipeline (Fargate profile, IAM, Firehose, S3/Athena) IS driven by
# for_each over the `clusters` Terraform variable; this script is the one
# per-cluster step, reading that same registry via `terraform output -json`
# so cluster config lives in exactly one place.
#
# Prerequisites:
#   • terraform apply -var enable_log_platform_test=true already run
#   • aws CLI + kubectl in PATH, authenticated (aws sso login, or CloudShell)
#   • this cluster already registered as an Integration via the Hub's admin
#     API (ADR 0022) — you need its Integration.ID for <cluster-id> below
#
# Usage:
#   scripts/onboard_cluster_logging.sh <cluster-key> <cluster-id>
#
# <cluster-key> is a key in the `clusters` Terraform variable/output
# (e.g. "agentify_dev" — the default entry for the cluster this root module
# manages; add more entries to `variable "clusters"` for additional clusters).
#
# <cluster-id> is that cluster's Hub Integration.ID (NOT the Terraform
# cluster-key and NOT the K8s cluster's own name) — stamped onto every log
# record so a centralized Glue-based dependency miner spanning multiple
# clusters can tell which cluster each row came from (ADR 0029). Deliberately
# not auto-looked-up from the Hub's admin API here: Integration.Name is a
# free-text label with no guaranteed correspondence to this Terraform
# cluster-key, so guessing it would be more fragile than requiring the
# operator to supply the ID they already used to mint this cluster's
# CollectorToken.

set -euo pipefail

CLUSTER_KEY="${1:?Usage: $0 <cluster-key> <cluster-id> (e.g. agentify_dev 3f9c2e1a-...)}"
CLUSTER_ID="${2:?Usage: $0 <cluster-key> <cluster-id> (e.g. agentify_dev 3f9c2e1a-...)}"
TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../infra/terraform/aws" && pwd)"
TEMPLATE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../infra/kubernetes/fargate-logging" && pwd)/aws-observability-configmap.yaml.tpl"

CLUSTERS_JSON=$(terraform -chdir="$TF_DIR" output -json clusters)
CLUSTER_NAME=$(echo "$CLUSTERS_JSON" | jq -r ".${CLUSTER_KEY}.cluster_name // empty")
if [ -z "$CLUSTER_NAME" ]; then
  echo "ERROR: no cluster '${CLUSTER_KEY}' in the clusters Terraform output." >&2
  echo "Known clusters: $(echo "$CLUSTERS_JSON" | jq -r 'keys | join(", ")')" >&2
  exit 1
fi

FIREHOSE_STREAM_NAME=$(terraform -chdir="$TF_DIR" output -raw log_platform_firehose_stream_name)
if [ -z "$FIREHOSE_STREAM_NAME" ] || [ "$FIREHOSE_STREAM_NAME" = "null" ]; then
  echo "ERROR: log_platform_firehose_stream_name output is empty — is enable_log_platform_test=true applied?" >&2
  exit 1
fi
AWS_REGION="${AWS_REGION:-$(aws configure get region)}"
if [ -z "$AWS_REGION" ]; then
  echo "ERROR: could not determine AWS region — set AWS_REGION or run 'aws configure'." >&2
  exit 1
fi

echo "Onboarding cluster '${CLUSTER_KEY}' (${CLUSTER_NAME}) to Firehose stream '${FIREHOSE_STREAM_NAME}'..."
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION"

# sed, not envsubst — only a few known placeholders, not worth a gettext
# dependency (envsubst isn't installed by default on macOS, and Homebrew
# isn't always reachable from every network).
sed -e "s|\${aws_region}|${AWS_REGION}|g" -e "s|\${firehose_stream_name}|${FIREHOSE_STREAM_NAME}|g" -e "s|\${cluster_id}|${CLUSTER_ID}|g" "$TEMPLATE" | kubectl apply -f -

echo "Done. Fargate pods scheduled after this point in the target namespace(s)"
echo "will get the log router sidecar injected automatically — restart any"
echo "already-running pods (kubectl rollout restart) to pick it up retroactively."
