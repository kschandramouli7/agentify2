"""Set GitHub Actions secrets from Terraform outputs via the GitHub API.

Usage:
    GITHUB_TOKEN=ghp_... python3 scripts/set_github_secrets.py

The token needs repo scope (Settings → Developer settings → Personal access tokens).
"""

import base64
import json
import os
import sys
import urllib.request
import urllib.error

from nacl import encoding, public


OWNER = "kschandramouli7"
REPO  = "agentify2"

SECRETS = {
    "AWS_ROLE_ARN":      "arn:aws:iam::637423369012:role/agentify-dev-ci",
    "AWS_REGION":        "ap-southeast-2",
    "ECR_REGISTRY":      "637423369012.dkr.ecr.ap-southeast-2.amazonaws.com",
    "EKS_CLUSTER_NAME":  "agentify-dev",
}


def _api(path: str, method: str = "GET", body: dict = None, token: str = "") -> dict:
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def encrypt(public_key_b64: str, secret_value: str) -> str:
    """Encrypt a secret using the repo's public key (libsodium sealed box)."""
    pk = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder)
    box = public.SealedBox(pk)
    encrypted = box.encrypt(secret_value.encode())
    return base64.b64encode(encrypted).decode()


def main():
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("ERROR: GITHUB_TOKEN env var not set.")
        print("Create a token at: https://github.com/settings/tokens")
        print("Required scopes: repo (includes secrets)")
        sys.exit(1)

    print(f"Fetching repo public key for {OWNER}/{REPO}...")
    key_data = _api(f"/repos/{OWNER}/{REPO}/actions/secrets/public-key", token=token)
    key_id    = key_data["key_id"]
    key_b64   = key_data["key"]

    for name, value in SECRETS.items():
        encrypted = encrypt(key_b64, value)
        _api(
            f"/repos/{OWNER}/{REPO}/actions/secrets/{name}",
            method="PUT",
            body={"encrypted_value": encrypted, "key_id": key_id},
            token=token,
        )
        print(f"  ✓ {name}")

    print(f"\nAll {len(SECRETS)} secrets set on {OWNER}/{REPO}.")
    print("GitHub Actions deploy workflow is ready to trigger.")


if __name__ == "__main__":
    main()
