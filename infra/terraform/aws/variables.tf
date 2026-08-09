variable "aws_region" {
  type    = string
  default = "ap-southeast-2"
}

variable "project" {
  type    = string
  default = "agentify"
}

variable "env" {
  type    = string
  default = "dev"
  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be dev or prod."
  }
}

# EKS
variable "cluster_version" {
  type    = string
  default = "1.33"
}

# Low-cost node group for dev; scale up in prod.
variable "node_instance_type" {
  type    = string
  default = "t3.medium"
}

variable "node_min" {
  type    = number
  default = 1
}

variable "node_max" {
  type    = number
  default = 3
}

variable "node_desired" {
  type    = number
  default = 2
}

# RDS
variable "db_instance_class" {
  type    = string
  default = "db.t3.micro"
}

variable "db_name" {
  type    = string
  default = "agentify"
}

variable "db_username" {
  type    = string
  default = "agentify"
}

# VPC CIDR — single VPC for everything.
variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "anthropic_api_key" {
  type        = string
  sensitive   = true
  description = "Anthropic API key stored in Secrets Manager. Pass via TF_VAR_anthropic_api_key env var — never hardcode."
}

variable "github_token" {
  type        = string
  sensitive   = true
  description = "GitHub PAT with repo+secrets scope. Used to set GitHub Actions secrets from Terraform outputs. Pass via TF_VAR_github_token."
}

variable "github_repo" {
  type        = string
  default     = "agentify"
  description = "GitHub repository name (without owner prefix)."
}

variable "github_owner" {
  type        = string
  default     = "kschandramouli"
  description = "GitHub account/org that owns the repository."
}

variable "ci_role_name" {
  type        = string
  default     = ""
  description = "Name of the CI IAM role to attach Vault secrets read policy to. Leave empty to skip attachment."
}

variable "create_github_oidc_provider" {
  type        = bool
  default     = true
  description = "Whether to create the GitHub Actions OIDC identity provider (token.actions.githubusercontent.com) in this account. Set to false when the account already has one registered by something else — AWS allows only one OIDC provider per issuer URL per account, so applying with this left true against such an account fails with EntityAlreadyExists. When false, the CI role's trust policy is built against the existing provider's well-known ARN pattern instead, without Terraform ever creating, importing, or otherwise touching it."
}

variable "github_oidc_trusted_repos" {
  type = list(string)
  default = [
    "repo:kschandramouli/agentify:*",
    "repo:kschandramouli/agentify2:*",
    "repo:kschandramouli@*/agentify:*",
    "repo:kschandramouli@*/agentify2@*:*",
    "repo:kschandramouli7/agentify2:*",
    "repo:kschandramouli7@*/agentify2@*:*",
  ]
  description = "GitHub repos (StringLike patterns on token.actions.githubusercontent.com:sub) allowed to assume the CI role. Defaults to every repo used across every account this stack has been deployed to so far — override with -var for an account where only a subset should be trusted (e.g. a shared/corporate account, where trusting unrelated personal repos is unnecessary over-permissioning even though harmless: they have no secrets pointing at that account anyway)."
}

variable "tfstate_bucket" {
  type        = string
  default     = "agentify-tfstate-f6e00ef8"
  description = "The S3 bucket infra/terraform/aws/backend.tf's remote state lives in (from infra/terraform/bootstrap's state_bucket output). Defaults to the original account's bucket for backward compatibility — MUST be overridden with -var when applying against a different AWS account, since bucket names are random-suffixed per bootstrap run and this can't be derived automatically. Used by the CI role's Terraform-state IAM grant (the Pause/Resume workflows' targeted `terraform apply`), not by the backend block itself (which can't reference variables and is edited directly per docs/DEPLOYMENT.md)."
}

variable "enable_nat_gateway" {
  type        = bool
  default     = false
  description = "Whether to create a NAT gateway and run the EKS node group in private subnets (with public subnets reserved for the internet-facing ALB only). Defaults to false — the original account's cost-optimized setup runs nodes directly in public subnets with no NAT gateway (saves ~$35/month, see main.tf). Set to true for an account whose security policy denies ec2:RunInstances with AssociatePublicIpAddress=true (a common Service Control Policy in governed/corporate AWS Organizations) — that denial surfaces as an opaque 'not authorized to launch instances with this launch template' error on the EKS node group, decodable via `aws sts decode-authorization-message`."
}

# ── P15 test log-platform (ADR 0021) ─────────────────────────────────────────
# Fargate + Kinesis Firehose + OpenSearch, used to validate the P15 log
# connector against a real, isolated log source. Off by default — the
# OpenSearch domain is the dominant cost item of this whole stack, so it
# should only exist while a test session is actually running.
variable "enable_log_platform_test" {
  type        = bool
  default     = false
  description = "Provisions the Fargate profile + Firehose + OpenSearch test log pipeline (ADR 0021). Keep false except during an active test session."
}

# Single source of truth for every cluster onboarded to the shared log
# pipeline. Onboarding a new cluster = add one entry here; onboarding an
# additional service/namespace on an already-onboarded cluster = add one
# entry to that cluster's `namespaces` list (see ADR 0021 for why the Fargate
# profile scales via for_each but the aws-observability ConfigMap does not).
# Every namespace listed, across every cluster, streams into the SAME shared
# Firehose -> S3 -> Glue destination (one database/table for the whole
# pipeline) — namespace/pod identity is preserved per-row via the
# kubernetes.namespace_name/pod_name columns, not a separate table per service.
variable "clusters" {
  type = map(object({
    cluster_name = string
    subnet_ids   = list(string)
    namespaces   = list(string)
  }))
  default     = {}
  description = "Clusters onboarded to the shared Firehose/Glue log pipeline. Populated in main.tf for the cluster this root module manages; add entries for additional clusters, or additional namespaces on an existing cluster, here."
}

