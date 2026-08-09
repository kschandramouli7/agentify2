# Remote state backend. Fill in bucket + dynamodb_table from bootstrap outputs
# before running `terraform init` in this directory.
#
# After running bootstrap:
#   terraform init \
#     -backend-config="bucket=<state_bucket from bootstrap output>" \
#     -backend-config="dynamodb_table=agentify-tfstate-lock"

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.27"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.13"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    # github provider (integrations/github ~> 6.0) intentionally excluded —
    # binary download is blocked by corporate proxy. GitHub secrets are set
    # via scripts/set_github_secrets.py instead.
  }

  backend "s3" {
    bucket         = "agentify-tfstate-e906dcc8"
    key            = "agentify/dev/terraform.tfstate"
    region         = "ap-southeast-2"
    dynamodb_table = "agentify-tfstate-lock"
    encrypt        = true
  }
}
