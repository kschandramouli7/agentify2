# TEMPLATE — DO NOT `kubectl apply -f` the manifest that deploys this image
# directly (see infra/kubernetes/security-verifier.yaml's header).
#
# Docker image for agentify-security-verifier (ROADMAP P30 phases 2-4, ADR
# 0033) — the network-isolated executor for approved active-verification
# engagements. No third-party dependencies (see requirements.txt).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the package, preserving the package path so relative imports work —
# same convention as agentify-discovery's Dockerfile.
COPY . ./security_verifier/

ENV PORT=8400

CMD ["python", "-m", "security_verifier.main"]
