# AWS CloudShell Runbook

Operating agentify from AWS CloudShell. Every command here has been run against
the live `agentify-dev` cluster.

> **Why CloudShell.** It already holds your federated AWS credentials, so it
> works when local SSO does not — and it sits in a place that can reach the
> cluster. Notably RDS is **inside the VPC**, so CloudShell cannot reach the
> database directly; the psql recipe below runs the query from a pod instead.

## 0. Cluster access — once per session

```bash
aws eks update-kubeconfig --name agentify-dev --region ap-southeast-2
kubectl get pods -n agentify
```

Expect `Added new context arn:aws:eks:ap-southeast-2:637423369012:cluster/agentify-dev`.

No `aws sso login` needed — CloudShell inherits the console session. That is the
whole reason this path exists: locally you need a working SSO profile *and* a
kubeconfig pointed at the right account, and a stale context silently falls back
to `localhost:8080` (`connection refused`).

## 1. Logs

```bash
# a specific failure
kubectl logs -n agentify -l app=agentify-agent --tail=400 | grep -A5 "pattern-a reasoning failed"

# anything wrong, recent — this is the one that found the 401
kubectl logs -n agentify -l app=agentify-agent --tail=200 \
  | grep -iE "error|401|insufficient|JSON-wrapped|tracing failed|embedding store failed"

# follow live while you run a query from another tab
kubectl logs -n agentify -l app=agentify-agent -f --tail=20

# the backend rather than the agent
kubectl logs -n agentify -l app=agentify-backend --tail=200 | grep -i error
```

Log lines worth recognising:

| Line | Meaning |
|---|---|
| `401 ... invalid x-api-key` | Anthropic key wrong — see `DEPLOYMENT.md` Step 4 |
| `insufficient credits` | Key valid, account unfunded |
| `API key from ... was JSON-wrapped N level(s) deep` | Secret is malformed but was recovered; fix the stored value |
| `Langfuse resolve('...') failed` | Prompt fetch failed; local fallback in use. Note the 60s negative cache means one line per prompt per minute, not per request |
| `Langfuse tracing failed for '...'` | Tracing only — the query itself was unaffected |
| `incident embedding store failed` | Semantic-memory write failed — see `SEMANTIC_MEMORY.md` |

## 2. Query the database

RDS is in the VPC, so run psql from a throwaway pod. Credentials come from the
existing secret via `secretKeyRef`, so no password ever appears on a command line
or in shell history.

```bash
kubectl run pgcheck -n agentify --rm -i --restart=Never --image=postgres:16-alpine \
  --overrides='{"spec":{"containers":[{"name":"pgcheck","image":"postgres:16-alpine",
    "command":["sh","-c","psql \"postgresql://$DB_USER:$DB_PASSWORD@$DB_HOST:$DB_PORT/$DB_NAME\" -c \"SELECT count(*) AS total, count(embedding) AS with_vector FROM incident_embeddings;\""],
    "env":[
      {"name":"DB_HOST","valueFrom":{"secretKeyRef":{"name":"agentify-db-secret","key":"host"}}},
      {"name":"DB_PORT","valueFrom":{"secretKeyRef":{"name":"agentify-db-secret","key":"port"}}},
      {"name":"DB_NAME","valueFrom":{"secretKeyRef":{"name":"agentify-db-secret","key":"dbname"}}},
      {"name":"DB_USER","valueFrom":{"secretKeyRef":{"name":"agentify-db-secret","key":"username"}}},
      {"name":"DB_PASSWORD","valueFrom":{"secretKeyRef":{"name":"agentify-db-secret","key":"password"}}}
    ]}]}}'
```

Swap the SQL by editing the `-c "..."` arguments; add more `-c` for more queries.
**Avoid single quotes in the SQL** — the nesting gets unmanageable. Where a
string literal is unavoidable, `'"'"'` produces one single quote, e.g.
`WHERE extname='"'"'vector'"'"'`.

Useful queries:

```sql
-- semantic memory (see SEMANTIC_MEMORY.md for how to read this)
SELECT count(*) AS total, count(embedding) AS with_vector FROM incident_embeddings;
SELECT namespace, service, left(summary,60) FROM incident_embeddings ORDER BY created_at DESC LIMIT 5;

-- prompt provenance (see PROMPT_LIFECYCLE.md)
SELECT intent, tier, prompt_name, prompt_version, is_eval, created_at
  FROM traces ORDER BY created_at DESC LIMIT 10;

-- conversation grouping
SELECT session_id, count(*) FROM traces WHERE session_id <> '' GROUP BY 1;

-- is pgvector present at all
SELECT extname FROM pg_extension;
```

## 3. Hit the backend API

`kubectl port-forward … &` returns immediately but the tunnel takes a second or
two, so a curl on the next line fails with `Could not connect` while
`Forwarding from 127.0.0.1:18080` prints afterwards. Wait for readiness:

```bash
kubectl port-forward -n agentify svc/agentify-backend 18080:8080 >/dev/null 2>&1 &
for i in $(seq 1 10); do
  curl -sf localhost:18080/health >/dev/null 2>&1 && { echo "port-forward ready"; break; }
  sleep 2
done

curl -sS localhost:18080/health

curl -sS -X POST localhost:18080/api/query -H 'Content-Type: application/json' \
  -d '{"question":"why is payment-worker crashing?","context":{"namespace":"payments","service":"payment-worker"}}'

curl -sS localhost:18080/admin/traces | python3 -m json.tool | head -40
```

A `diagnose` question is the right probe: a healthy service takes the Tier-1
fast path, makes no LLM call, and correctly records no prompt version
([ADR 0006](../context-mesh/decisions/0006-two-tier-query-path.md)) — which
looks like a failure but isn't.

**The port-forward dies with the CloudShell session**, which idles out after
about 20 minutes.

## 4. Secrets

```bash
# read the k8s secret the pod is actually using
for k in host port dbname username password; do
  printf "%s=" "$k"; kubectl get secret agentify-db-secret -n agentify -o jsonpath="{.data.$k}" | base64 -d; echo
done

# check the Anthropic key is not JSON-wrapped (see DEPLOYMENT.md Step 4)
aws secretsmanager get-secret-value --secret-id agentify/dev/anthropic \
  --region ap-southeast-2 --query SecretString --output text \
  | python3 -c "import sys,json;v=json.load(sys.stdin)['api_key'];print('OK' if v.startswith('sk-ant-') else 'STILL WRAPPED -> '+v[:60])"
```

**Updating Secrets Manager does not reach the pod.** The agent reads
`ANTHROPIC_API_KEY` from env, sourced from the Kubernetes secret, which only
`02-deploy.yml` recreates. Either run that workflow, or:

```bash
kubectl create secret generic agentify-anthropic-secret -n agentify \
  --from-literal=api_key="$(aws secretsmanager get-secret-value \
     --secret-id agentify/dev/anthropic --region ap-southeast-2 \
     --query SecretString --output text \
     | python3 -c 'import sys,json;print(json.load(sys.stdin)["api_key"])')" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deploy/agentify-agent -n agentify
```

## 5. Restarts and rollout

```bash
kubectl rollout restart deploy/agentify-agent   -n agentify
kubectl rollout restart deploy/agentify-backend -n agentify
kubectl rollout status  deploy/agentify-agent   -n agentify
kubectl describe pod -n agentify -l app=agentify-agent | tail -30   # why a pod is Pending
```

Prompts do **not** need a restart — they resolve per request
([PROMPT_LIFECYCLE.md](PROMPT_LIFECYCLE.md)). Restart only for a changed secret
or env var.

## 6. Promote a prompt from CloudShell

The Langfuse UI is the usual route. Scripted alternative:

```bash
pip install -q langfuse
export LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... \
       LANGFUSE_BASE_URL=https://us.cloud.langfuse.com

python3 - <<'EOF'
import os
from langfuse import Langfuse
lf = Langfuse(public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
              secret_key=os.environ["LANGFUSE_SECRET_KEY"],
              base_url=os.environ["LANGFUSE_BASE_URL"])
lf.update_prompt(name="k8fy/diagnose", version=6, new_labels=["production"])
print("production now points at version 6")
EOF
```

See [PROMPT_LIFECYCLE.md](PROMPT_LIFECYCLE.md) for the gate-before-promote
workflow this sits at the end of.

## Gotchas

- **Sessions idle out (~20 min)** and take background port-forwards with them.
- **`kubectl run -i` records the pod's stdout to container logs.** Passing
  credentials via `secretKeyRef` rather than on the command line keeps them out
  of both the log and your history.
- **CloudShell has no cluster context until you run step 0** in each new session.
- **Never pipe pod logs into CI.** This repository is public and Actions logs are
  world-readable; use CloudShell, where the output stays in your session.

## References

- [DEPLOYMENT.md](DEPLOYMENT.md) — first-time AWS setup
- [PROMPT_LIFECYCLE.md](PROMPT_LIFECYCLE.md) — changing, gating, promoting prompts
- [SEMANTIC_MEMORY.md](SEMANTIC_MEMORY.md) — embeddings, and how to verify them
