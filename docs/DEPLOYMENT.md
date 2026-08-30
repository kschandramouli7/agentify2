# Deployment Guide

Architecture: all workloads (backend, agent, discovery) run on a single EKS cluster.
The frontend is a static Vite build served from S3 + CloudFront.
See [ADR 0017](../context-mesh/decisions/0017-all-on-eks-topology.md).

**Deploying this stack into a second/different AWS account?** Read
[NEW_ACCOUNT_MIGRATION.md](NEW_ACCOUNT_MIGRATION.md) first — it documents
every account-portability variable this guide's steps rely on, plus the
full set of gotchas (hardcoded values, apply-ordering races, SCP/NAT
interactions, credential-chain quirks) hit the last time this was done.

## Authentication model

| Who/what needs AWS access | How |
|---|---|
| **GitHub Actions** (ongoing CI/CD) | OIDC — no stored credentials (already in `iam.tf`) |
| **You, running bootstrap locally** | AWS IAM Identity Center SSO (browser login, auto-expiring session) |

There are **no long-lived IAM user keys** in this setup. If you already have
`~/.aws/credentials` with static keys, that works for bootstrap but is not
recommended — follow Step 0 below to replace it with short-lived SSO sessions.

---

## Step 0 — Set up AWS IAM Identity Center (one-time)

> Skip this if you already have a working `aws sso login` profile for your account.

IAM Identity Center gives you browser-based login with auto-expiring credentials.
No long-lived keys, no corporate IdP required. **One manual click** is unavoidable
(Terraform cannot create the IAM Identity Center instance itself); everything after
that is fully Terraformed.

### 0a — Enable IAM Identity Center (one console click)

1. Open the [IAM Identity Center console](https://console.aws.amazon.com/singlesignon)
2. Click **Enable** → confirm region `ap-southeast-2`
3. That's it — the instance is ready. Terraform reads it as a data source.

You need temporary credentials to run the SSO module for the first time. Use your
**root account** (temporarily) or any existing IAM user. After this step you'll
never need them again.

### 0b — Run the SSO Terraform module

```bash
cd infra/terraform/sso
terraform init
terraform apply \
  -var="email=your@email.com" \
  -var="first_name=Your" \
  -var="last_name=Name"
```

This creates:
- A permission set (`AgentifyAdmin` = AdministratorAccess; scope down later)
- Your user in the built-in identity store
- The account assignment binding them together

AWS will send a **"Set your password"** email to the address you provided.
Check it and set your password before continuing.

The `terraform output` block prints the exact `aws configure sso` command to run.

### 0c — Configure the AWS CLI profile

```bash
aws configure sso --profile agentify-dev
```

When prompted, use the values from the Terraform output `sso_start_url`:
- **SSO start URL**: value from `terraform output sso_start_url`
- **SSO region**: `ap-southeast-2`
- **Default region**: `ap-southeast-2`
- **Default output**: `json`

### 0d — Login and verify

```bash
aws sso login --profile agentify-dev
# Opens browser → click Allow → session valid 8h
aws sts get-caller-identity --profile agentify-dev
# Returns your account ID and the AgentifyAdmin role ARN
```

### 0e — Export the profile for all subsequent steps

```bash
export AWS_PROFILE=agentify-dev
```

Add to `~/.zshrc` (or `~/.bash_profile`) so it persists:
```bash
export AWS_PROFILE=agentify-dev
```

---

## Prerequisites

- AWS SSO profile configured (Step 0 above) — **no static keys needed**
- Terraform ≥ 1.6 (download from [releases.hashicorp.com](https://releases.hashicorp.com/terraform/))
- `kubectl` and `helm` installed locally
- Docker (for local builds; CI builds in GitHub Actions)

---

## Step 1 — Bootstrap Terraform remote state (once)

Make sure you're logged in: `aws sso login --profile agentify-dev` (if session expired).

```bash
export AWS_PROFILE=agentify-dev    # or set in your shell profile
cd infra/terraform/bootstrap
terraform init
terraform apply -var="aws_region=ap-southeast-2" -var="project=agentify"
# Note the outputs: state_bucket and lock_table
```

---

## Step 2 — Configure the backend

Edit `infra/terraform/aws/backend.tf` and replace `REPLACE_WITH_BOOTSTRAP_OUTPUT`
with the `state_bucket` value from Step 1.

```bash
cd infra/terraform/aws
terraform init \
  -backend-config="bucket=<state_bucket>" \
  -backend-config="dynamodb_table=agentify-tfstate-lock"
```

---

## Step 3 — Provision AWS infrastructure

```bash
cd infra/terraform/aws
terraform plan -var="env=dev"
terraform apply -var="env=dev"
```

This creates: EKS cluster, VPC, RDS Postgres, DynamoDB (pod registry), ECR repos,
Secrets Manager secrets, IRSA roles, and the ALB controller.

Note the outputs — you'll need them in the next steps:
- `cluster_name` → for kubeconfig and the `EKS_CLUSTER_NAME` GitHub secret
- `ci_role_arn` → for the `AWS_ROLE_ARN` GitHub secret
- `ecr_*_url` → for image pushes
- `anthropic_secret_arn` → to fill the API key

---

## Step 4 — Fill the Anthropic API key

```bash
aws secretsmanager put-secret-value \
  --secret-id agentify/dev/anthropic \
  --region ap-southeast-2 \
  --secret-string '{"api_key":"sk-ant-YOUR-KEY-HERE"}'
```

**Put the RAW key in there, never the secret's existing JSON.** This command
replaces the whole value, so passing the current value as `api_key` wraps it one
level deeper each time you run it. Seen for real on 2026-08-30: the secret ended
up triple-wrapped —

```json
{"api_key":"{\"api_key\":\"{\\\"api_key\\\":\\\"sk-ant-...\\\"}\"}"}
```

— the agent sent that JSON as its `x-api-key`, and **every Tier-2 query 401'd for
hours** while the eval suite still reported mean 0.935. The agent now unwraps
nested layers and refuses to start if the value is still JSON, but the stored
value should be correct rather than rescued.

Verify after writing:

```bash
aws secretsmanager get-secret-value --secret-id agentify/dev/anthropic \
  --region ap-southeast-2 --query SecretString --output text \
  | python3 -c "import sys,json;v=json.load(sys.stdin)['api_key'];\
print('OK' if v.startswith('sk-ant-') else 'STILL WRAPPED -> '+v[:60])"
```

Then push it to the cluster and restart the agent so it re-reads at startup:

```bash
kubectl create secret generic agentify-anthropic-secret -n agentify \
  --from-literal=api_key="$(aws secretsmanager get-secret-value \
     --secret-id agentify/dev/anthropic --region ap-southeast-2 \
     --query SecretString --output text | python3 -c 'import sys,json;print(json.load(sys.stdin)["api_key"])')" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deploy/agentify-agent -n agentify
```

---

## Step 5 — Configure kubeconfig

```bash
aws eks update-kubeconfig --name <cluster_name from Step 3> --region ap-southeast-2
kubectl get nodes   # should list your nodes
```

---

## Step 6 — Patch manifests with real values

Replace the `REPLACE_WITH_TERRAFORM_OUTPUT_*` and `ACCOUNT_ID` placeholders in
`infra/kubernetes/*.yaml` with actual values from Terraform outputs.

```bash
# Example (replace ACCOUNT_ID and role ARNs):
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BACKEND_ROLE=$(cd infra/terraform/aws && terraform output -raw backend_irsa_role_arn)
AGENT_ROLE=$(cd infra/terraform/aws && terraform output -raw agent_irsa_role_arn)

sed -i "s|ACCOUNT_ID|${ACCOUNT_ID}|g" infra/kubernetes/*.yaml
sed -i "s|REPLACE_WITH_TERRAFORM_OUTPUT_backend_irsa_role_arn|${BACKEND_ROLE}|g" infra/kubernetes/backend.yaml
sed -i "s|REPLACE_WITH_TERRAFORM_OUTPUT_agent_irsa_role_arn|${AGENT_ROLE}|g" infra/kubernetes/agent.yaml
# agentify-discovery has no IRSA role (ADR 0027 — in-cluster RBAC only, no
# AWS API access needed), so discovery.yaml needs no role-ARN substitution.
```

---

## Step 7 — Create Kubernetes secrets from Secrets Manager

The manifests reference K8s Secrets by name. Create them once (or install
External Secrets Operator to sync from Secrets Manager automatically):

```bash
# DB credentials
DB_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id agentify/dev/db --query SecretString --output text)
kubectl create secret generic agentify-db-secret -n agentify \
  --from-literal=host=$(echo $DB_SECRET | jq -r .host) \
  --from-literal=port=$(echo $DB_SECRET | jq -r .port) \
  --from-literal=dbname=$(echo $DB_SECRET | jq -r .dbname) \
  --from-literal=username=$(echo $DB_SECRET | jq -r .username) \
  --from-literal=password=$(echo $DB_SECRET | jq -r .password)

# agentify-discovery collector token (ADR 0022/0027) — this Secret must
# exist for the pod to start at all (discovery.yaml's secretKeyRef has no
# `optional: true`), but the token VALUE itself is optional: leave it empty
# for a single-cluster deployment and ingestion still works unscoped,
# exactly like the old k8fy-adapter's default. Only set a real value if this
# cluster is joining a multi-cluster fleet (ROADMAP P16/P18) — mint it via
# POST/PUT /admin/integrations' collector_token field first (it becomes that
# Integration row's credential — see ADR 0022), store it at
# agentify/dev/discovery yourself (no Terraform resource for this one; it's
# minted at runtime, not known at `terraform apply` time), then sync it here:
COLLECTOR_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id agentify/dev/discovery --query SecretString --output text 2>/dev/null | jq -r '.collector_token // ""')
kubectl create secret generic agentify-discovery-secret -n agentify \
  --from-literal=collector_token="$COLLECTOR_TOKEN"

# Anthropic API key
ANTHROPIC_KEY=$(aws secretsmanager get-secret-value \
  --secret-id agentify/dev/anthropic --query SecretString --output text | jq -r .api_key)
kubectl create secret generic agentify-anthropic-secret -n agentify \
  --from-literal=api_key=$ANTHROPIC_KEY
```

---

## Step 8 — First deploy

```bash
kubectl apply -f infra/kubernetes/backend.yaml
kubectl apply -f infra/kubernetes/agent.yaml
kubectl apply -f infra/kubernetes/discovery.yaml
kubectl apply -f infra/kubernetes/ingress.yaml

kubectl rollout status deployment/agentify-backend   -n agentify
kubectl rollout status deployment/agentify-agent     -n agentify
kubectl rollout status deployment/agentify-discovery -n agentify
```

`agentify-discovery` (`discovery.yaml`) is the one per-cluster collector
([ADR 0027](../context-mesh/decisions/0027-merge-k8fy-adapter-into-discovery.md)
merged the old k8fy-adapter's ingestion role into it) — every cluster needs
it running for `current_state`/`events` to populate at all, so it's part of
the base deploy now, not optional. Requires the `agentify-discovery-secret`
created in Step 7 (an empty `collector_token` is fine for a single-cluster
deployment — see below).

Get the ALB address:
```bash
kubectl get ingress -n agentify
```

### Optional — join a multi-cluster fleet (ROADMAP P18/P16)

**Where each piece runs:** the Hub (`backend.yaml`, deployed once in Step 8
above) is the one central process every cluster in the fleet reports into.
Every cluster runs its own `agentify-discovery` Deployment (deployed
unconditionally in Step 8) — one per cluster, not a replacement for or a
second copy of the Hub. What's actually optional here is giving that
Deployment a **real** `COLLECTOR_TOKEN`: with an empty token (Step 7's
default), Discovery still ingests into the Hub, just unscoped
(`DefaultTenantID`), exactly like a single-cluster deployment always has.
Set a real value only if this cluster should report into a shared Hub
alongside others — mint it via POST/PUT `/admin/integrations`'
`collector_token` field, store it and re-run Step 7's `agentify-discovery-secret`
creation with the real value, then restart the Deployment:

```bash
kubectl rollout restart deployment/agentify-discovery -n agentify
kubectl rollout status  deployment/agentify-discovery -n agentify
```

**Security note (ADR 0024):** Discovery's ClusterRole grants
`secrets: list, get` cluster-wide **in this cluster only** — its
`live_get_certificates` capability needs to read `kubernetes.io/tls`
Secrets for expiry, but RBAC itself can't scope by Secret *type*, only by
resource kind. Discovery enforces the type filter client-side
(`fieldSelector=type=kubernetes.io/tls`) and never returns raw cert/key
bytes to the Hub, but a compromised Discovery pod would have read access to
every Secret in *that* cluster (the Hub never holds this credential itself —
it only ever receives Discovery's already-filtered answers). Review before onboarding a
cluster with sensitive Secrets outside the TLS-cert use case.

---

## Step 9 — GitHub Actions secrets (for CI/CD)

In your GitHub repo → Settings → Secrets → Actions, add:

| Secret | Value |
|---|---|
| `AWS_ROLE_ARN` | `ci_role_arn` from Terraform output |
| `AWS_REGION` | `ap-southeast-2` |
| `ECR_REGISTRY` | `ACCOUNT_ID.dkr.ecr.ap-southeast-2.amazonaws.com` |
| `EKS_CLUSTER_NAME` | `cluster_name` from Terraform output |

After that, every push to `main` (touching `src/` or manifests) triggers the deploy workflow automatically.

---

## Cost estimate (dev environment, ap-southeast-2)

NAT gateway removed (nodes run in public subnets for dev); free S3 + DynamoDB
gateway endpoints added. Three cost tiers depending on usage:

### Running (fully active)

| Resource | ~Monthly |
|---|---|
| EKS control plane | $73 |
| 2× t3.medium nodes (on-demand) | ~$60 |
| RDS db.t3.micro single-AZ | ~$15 |
| ~~NAT gateway~~ (removed) | $0 |
| DynamoDB, ECR, Secrets Manager | <$5 |
| **Total** | **~$123/month** |

### Paused (nodes = 0, RDS stopped)

Trigger the **Pause** GitHub Actions workflow when not using for a day or more.

| Resource | ~Monthly |
|---|---|
| EKS control plane | $73 |
| Nodes (scaled to 0) | $0 |
| RDS (stopped, storage only) | ~$0.10 |
| **Total** | **~$73/month** |

Restore with the **Resume** workflow (~5–10 min).

### Fully destroyed (no cluster)

For extended breaks (weeks+), run `terraform destroy -target=module.eks -target=module.vpc -target=aws_db_instance.this -target=... -var="env=dev"` after taking an RDS snapshot. ECR images, DynamoDB data, and secrets survive (`prevent_destroy = true` on ECR + DynamoDB).

| Resource | ~Monthly |
|---|---|
| ECR, DynamoDB, Secrets Manager | ~$5 |
| S3 state bucket | ~$0.10 |
| **Total** | **~$5/month** |

Restore with `terraform apply -var="env=dev"` (~20 min).

---

## Pause / Resume (GitHub Actions)

Go to your repo → **Actions** tab:

- **Pause (scale down)**: manually trigger, type `pause` to confirm.
  Snapshots RDS, scales nodes to 0, stops RDS. Cost drops to ~$73/month.
- **Resume (scale up)**: manually trigger, optionally set node count (default: 2).
  Starts RDS, scales nodes back up. Takes ~5–10 min.
