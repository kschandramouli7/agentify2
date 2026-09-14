#!/usr/bin/env bash
# vault-setup.sh — Initialise and configure Vault for the agentify dev/test environment.
#
# Prerequisites:
#   • Vault deployed via helm (vault-values.yaml)
#   • kubectl context pointing at your EKS cluster
#   • vault CLI in PATH  (brew install vault  /  apt install vault)
#
# Usage:
#   bash scripts/vault-setup.sh
#
# What this does:
#   1. Initialises Vault (1-of-1 unseal for dev simplicity)
#   2. Unseals and logs in
#   3. Enables Kubernetes auth (pods authenticate with their ServiceAccount JWT)
#   4. Enables PKI secrets engine — Vault becomes the cert CA for payment services
#   5. Enables KV v2 for general secrets
#   6. Creates policy + role for the payments namespace
#   7. Stores the initial self-signed TLS cert for the payment-service in Vault KV

set -euo pipefail

VAULT_NS="vault"
VAULT_POD="vault-0"
PAYMENTS_NS="payments"

echo "=== Waiting for vault pod to be ready ==="
kubectl wait pod/"$VAULT_POD" -n "$VAULT_NS" \
  --for=condition=Ready --timeout=120s

# Port-forward so we can talk to Vault from this shell
kubectl port-forward -n "$VAULT_NS" svc/vault 8200:8200 &
PF_PID=$!
trap 'kill $PF_PID 2>/dev/null || true' EXIT
sleep 3

export VAULT_ADDR="http://127.0.0.1:8200"

# ── 1. Init (1-of-1 for dev simplicity) ──────────────────────────────────────
echo "=== Initialising Vault ==="
INIT_OUTPUT=$(vault operator init -key-shares=1 -key-threshold=1 -format=json)
UNSEAL_KEY=$(echo "$INIT_OUTPUT" | jq -r '.unseal_keys_b64[0]')
ROOT_TOKEN=$(echo "$INIT_OUTPUT"  | jq -r '.root_token')

echo "Unseal key: $UNSEAL_KEY"
echo "Root token: $ROOT_TOKEN"

# Save to a local file — treat like a password; don't commit to git.
cat > .vault-dev-creds <<EOF
VAULT_UNSEAL_KEY=$UNSEAL_KEY
VAULT_ROOT_TOKEN=$ROOT_TOKEN
EOF
chmod 600 .vault-dev-creds
echo "Credentials saved to .vault-dev-creds (gitignored)"

# ── 2. Unseal + login ─────────────────────────────────────────────────────────
vault operator unseal "$UNSEAL_KEY"
export VAULT_TOKEN="$ROOT_TOKEN"
vault login "$ROOT_TOKEN"

# ── 3. Kubernetes auth ────────────────────────────────────────────────────────
echo "=== Enabling Kubernetes auth ==="
vault auth enable kubernetes

# Vault needs the K8s API host + the cluster CA to verify pod JWTs.
K8S_HOST=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
K8S_CA=$(kubectl get secret \
  -n "$VAULT_NS" \
  "$(kubectl get serviceaccount vault -n "$VAULT_NS" \
     -o jsonpath='{.secrets[0].name}' 2>/dev/null || echo '')" \
  -o jsonpath='{.data.ca\.crt}' 2>/dev/null \
  | base64 --decode || \
  kubectl config view --raw --minify --flatten \
    -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' \
  | base64 --decode)

vault write auth/kubernetes/config \
  kubernetes_host="$K8S_HOST" \
  kubernetes_ca_cert="$K8S_CA"

# ── 4. PKI secrets engine (Vault as intermediate CA) ─────────────────────────
echo "=== Enabling PKI secrets engine ==="
vault secrets enable pki
vault secrets tune -max-lease-ttl=8760h pki   # 1 year max

# Generate the CA cert for the payments domain.
vault write -format=json pki/root/generate/internal \
  common_name="payments.svc.cluster.local Root CA" \
  ttl=8760h \
  | jq -r '.data.certificate' > .vault-payments-ca.pem
echo "CA cert written to .vault-payments-ca.pem"

# Configure CRL / issuing endpoints (optional for dev but good practice).
vault write pki/config/urls \
  issuing_certificates="http://vault.vault.svc.cluster.local:8200/v1/pki/ca" \
  crl_distribution_points="http://vault.vault.svc.cluster.local:8200/v1/pki/crl"

# Role for issuing certs to payment services.
vault write pki/roles/payment-service \
  allowed_domains="payments.svc.cluster.local,payment.payments.svc.cluster.local" \
  allow_subdomains=true \
  allow_localhost=true \
  max_ttl=720h     # 30 days — triggers rotation well before CA expiry
  generate_lease=true

# ── 5. KV v2 for general secrets ─────────────────────────────────────────────
echo "=== Enabling KV v2 ==="
vault secrets enable -path=secret kv-v2

# Store a placeholder TLS cert for the payment-service (first-time seed).
# Real cert will be issued by PKI role; this is just for bootstrapping.
CERT_DATA=$(vault write -format=json pki/issue/payment-service \
  common_name="payment.payments.svc.cluster.local" \
  ttl=720h)

echo "$CERT_DATA" | jq -r '.data.certificate' > .vault-payment-cert.pem
echo "$CERT_DATA" | jq -r '.data.private_key'  > .vault-payment-key.pem
echo "Initial cert written to .vault-payment-cert.pem"

vault kv put secret/payments/tls \
  certificate="$(cat .vault-payment-cert.pem)" \
  private_key="$(cat .vault-payment-key.pem)"

# ── 6. Policy + Kubernetes role ───────────────────────────────────────────────
echo "=== Creating policy and Kubernetes role ==="
vault policy write payment-service-policy - <<'HCL'
# Read and issue certs from the PKI engine.
path "pki/issue/payment-service" {
  capabilities = ["create", "update"]
}
path "pki/cert/*" {
  capabilities = ["read"]
}
# Read TLS material from KV.
path "secret/data/payments/*" {
  capabilities = ["read"]
}
# Allow token renewal.
path "auth/token/renew-self" {
  capabilities = ["update"]
}
HCL

# Rotation agent gets the same policy plus PKI revoke.
vault policy write cert-rotator-policy - <<'HCL'
path "pki/issue/payment-service" {
  capabilities = ["create", "update"]
}
path "pki/revoke" {
  capabilities = ["create", "update"]
}
path "pki/cert/*" {
  capabilities = ["read"]
}
path "secret/data/payments/*" {
  capabilities = ["create", "update", "read"]
}
path "auth/token/renew-self" {
  capabilities = ["update"]
}
HCL

# Kubernetes role: payment-service ServiceAccount → payment-service-policy.
vault write auth/kubernetes/role/payment-service \
  bound_service_account_names=payment-service \
  bound_service_account_namespaces="$PAYMENTS_NS" \
  policies=payment-service-policy \
  ttl=1h

# Kubernetes role: cert-rotator ServiceAccount → cert-rotator-policy.
vault write auth/kubernetes/role/cert-rotator \
  bound_service_account_names=cert-rotator \
  bound_service_account_namespaces="$PAYMENTS_NS" \
  policies=cert-rotator-policy \
  ttl=1h

echo ""
echo "✅ Vault setup complete."
echo ""
echo "Next steps:"
echo "  1. kubectl apply -f infra/kubernetes/payments-test/"
echo "  2. kubectl apply -f infra/kubernetes/vault/cert-rotator-cronjob.yaml"
echo ""
echo "  To reach the UI (no public Ingress — ROADMAP OPS-14, 2026-09-14: a"
echo "  dev-mode Vault with a published root token must never sit on the"
echo "  public ALB):"
echo "    kubectl port-forward -n vault svc/vault 8200:8200 &"
echo "    open http://localhost:8200/ui/"
echo "    login with root token: ${ROOT_TOKEN}"
