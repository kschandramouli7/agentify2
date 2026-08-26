# EKS Fargate's built-in log router (Fargate Fluent Bit) reads its config from
# this ConfigMap — no Fluent Bit DaemonSet to deploy or manage; AWS injects
# the router as a managed sidecar per pod once this exists (ADR 0021).
#
# Applied via scripts/onboard_cluster_logging.sh, not Terraform's kubernetes
# provider — see ADR 0021 for why (provider connections are static in HCL,
# can't loop across multiple clusters' API servers the way ordinary
# for_each'd resources can).
#
# Placeholders (${...}) are substituted by the onboarding script from the
# `clusters`/`log_platform_firehose_stream_name` Terraform outputs, plus
# ${cluster_id} — the Hub's Integration.ID for this cluster, supplied by the
# operator (not a Terraform output; minted by the Hub's admin API when the
# cluster is registered, ADR 0022) — nothing here should be hand-edited per
# cluster; edit the template once, re-run the script per cluster.
#
# NOTE: verify this against AWS's current Fargate logging documentation before
# relying on it in a new region/EKS version — the Fargate Fluent Bit image's
# supported OUTPUT plugin options have changed across EKS platform versions.
apiVersion: v1
kind: Namespace
metadata:
  name: aws-observability
  labels:
    aws-observability: "true"
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: aws-logging
  namespace: aws-observability
data:
  filters.conf: |
    [FILTER]
        Name                kubernetes
        Match               kube.*
        Merge_Log           On
        Buffer_Size         0
        Kube_Meta_Cache_TTL 300s
    [FILTER]
        # Stamps this cluster's own Hub Integration.ID directly (never a
        # human-readable cluster name) — sidesteps needing a name-to-ID
        # mapping downstream entirely. ADR 0029 / P18 use case #2 (Glue
        # extension): a centralized miner spanning multiple clusters' logs
        # can only attribute an edge to a specific fleet cluster if the row
        # itself already carries that cluster's real identifier.
        Name                record_modifier
        Match               *
        Record              cluster_id ${cluster_id}
  output.conf: |
    [OUTPUT]
        Name                firehose
        Match               *
        region              ${aws_region}
        delivery_stream     ${firehose_stream_name}
