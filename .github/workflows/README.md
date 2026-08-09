# Workflows

GitHub sorts both this folder's file listing and the Actions tab's sidebar
alphabetically — there's no separate "custom order" setting for either. The
`NN-` filename prefix and matching `NN ·` name prefix are how this repo
forces a deliberate order instead of an alphabetical accident. When adding a
new workflow, pick the next free number in the lifecycle stage it belongs to
(leave gaps, e.g. `06`/`07`/`12`, rather than renumbering existing files) and
give it a matching `name: "NN · Title"`.

**If you rename a file or its `name:` field, check for two things that break
silently otherwise:** any `workflow_run: workflows: [...]` trigger elsewhere
that matches by `name:` (not filename), and any `on.push.paths` entry that
references the workflow's own filename.

| # | Workflow | Trigger | Purpose |
|---|----------|---------|---------|
| 01 | [CI](01-ci.yml) | push/PR to `main` | Fast tests for backend (Go), agent (Python), discovery (Python), frontend (TS). No Docker build, no AWS calls. |
| 02 | [Deploy](02-deploy.yml) | `workflow_dispatch`, or push to `main` touching `src/**` / `infra/kubernetes/**` | Build images, push to ECR, roll out to EKS. The core deploy pipeline. |
| 03 | [Vault Bootstrap](03-vault-bootstrap.yml) | `workflow_dispatch`, or automatically after a successful Deploy | Creates namespace-scoped PKI engines, roles, KV paths, and Vault policies; applies the `payments-test` manifests so pods pick up Vault-injected certs. |
| 04 | [Bootstrap Langfuse Secret](04-bootstrap-langfuse-secret.yml) | `workflow_dispatch` | One-time (or post-rotation) setup: applies the IAM policy for the agent to read Langfuse credentials, then populates the secret in Secrets Manager. |
| 05 | [Payment Test (Full P1-P4c)](05-payment-test.yml) | `workflow_dispatch` | End-to-end integration test — drives a synthetic payment service through every signal shape the platform handles (health, deploy event, restart trend, expiring cert) and validates each query response. |
| 06 | [Onboard Cluster Logging](06-onboard-cluster-logging.yml) | `workflow_dispatch` | Applies the Fargate native log router ConfigMap for one cluster onboarded to the P15 test log pipeline (ADR 0021) — the one piece of that pipeline Terraform's `kubernetes` provider can't drive across multiple clusters. |
| 07 | [Mirror Base Images to ECR](07-mirror-base-images.yml) | `workflow_dispatch` | Mirrors the Docker Hub base images (`alpine`, `nginx`) the `payments-test` manifests need into ECR — the `payments` namespace's Fargate profile has no NAT gateway, so Docker Hub pulls from those pods time out (ADR 0021). |
| 08 | [Check Ingress / ALB Status](08-check-ingress.yml) | `workflow_dispatch` | One-shot diagnostic: ALB controller health, ingress status, pod health across namespaces, backend/agent reachability, and an end-to-end query smoke test. Run when the ALB address is missing after a deploy. |
| 09 | [Validate Langfuse Secrets](09-validate-langfuse.yml) | `workflow_dispatch` | Checks that the `LANGFUSE_*` GitHub secrets are valid and that the `k8fy-regression` eval dataset is seeded. |
| 10 | [Pause (Scale Down)](10-pause.yml) | `workflow_dispatch` (requires typing `pause` to confirm) | Cost control: snapshots RDS, scales the EKS managed node group to 0, stops RDS. Note: does **not** stop Fargate-scheduled pods (any namespace onboarded to the log pipeline per ADR 0021 — `payments` today) — those bill independently of the node group. |
| 11 | [Resume (Scale Up)](11-resume.yml) | `workflow_dispatch` | Reverses Pause: starts RDS, scales nodes back up, re-syncs the namespace cache, optionally waits for payment test pods to reschedule. |
