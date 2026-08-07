# Direct Azure AD SAML federation — an ALTERNATIVE to ../main.tf (AWS IAM
# Identity Center), not something that runs alongside it. Use this instead
# of ../main.tf when you already have an Azure AD (Entra ID) Enterprise
# Application acting as a SAML IdP and want to reuse it for a new AWS
# account, rather than provisioning a brand-new Identity Center user just
# for this project.
#
# Mechanism is genuinely different from ../main.tf:
#   ../main.tf     : AWS's own Identity Center service (aws_ssoadmin_*) —
#                     login via `aws sso login`.
#   this directory : a raw aws_iam_saml_provider + an IAM role trusting it —
#                     login via `sts:AssumeRoleWithSAML`, reached through the
#                     Azure AD "My Apps" portal or a CLI tool like
#                     saml2aws/aws-adfs. `aws sso login` does NOT work here.
#
# Prerequisite: azure-ad-metadata.xml in this directory is your Azure AD
# Enterprise Application's SAML federation metadata (public — the signing
# cert, not a private key, so safe to commit). Re-export a fresh copy from
# Azure AD if it ever changes (cert rotation).
#
# IMPORTANT — a step Terraform cannot do for you: after `apply`, your Azure
# AD Enterprise Application must be updated (in the Entra admin portal, not
# here) to add this AWS account's role/provider ARN pair to its `Role`
# claim / "Users and groups" role assignment. Without that, Azure AD will
# never issue an assertion permitting access to this account, no matter
# what's configured on the AWS side. The exact string to add is printed in
# the `role_claim_value` output below.
#
# Usage:
#   cd infra/terraform/sso/saml
#   terraform init
#   terraform apply -var="aws_region=ap-southeast-2"

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  # Intentionally local state, same reasoning as ../main.tf: this module
  # creates the auth infrastructure itself, so it cannot depend on the
  # remote-state bucket (which doesn't exist yet at this point in the setup
  # sequence).
}

variable "aws_region" {
  type    = string
  default = "ap-southeast-2"
}

variable "saml_provider_name" {
  type        = string
  default     = "AzureADAgentify"
  description = "Name for the IAM SAML provider resource."
}

variable "role_name" {
  type        = string
  default     = "AgentifySAMLAdmin"
  description = "Name for the IAM role Azure AD-authenticated users assume."
}

variable "max_session_duration" {
  type        = number
  default     = 28800 # 8h, matches ../main.tf's session_duration default
  description = "Max SAML session duration in seconds (AWS limit: 43200 / 12h)."
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "this" {}

# ── SAML identity provider ────────────────────────────────────────────────────
# Registers the Azure AD Enterprise App's metadata with this AWS account so
# it can validate assertions Azure AD signs.

resource "aws_iam_saml_provider" "azure_ad" {
  name                   = var.saml_provider_name
  saml_metadata_document = file("${path.module}/azure-ad-metadata.xml")
}

# ── IAM role Azure AD-authenticated users assume ──────────────────────────────
# AdministratorAccess for now (you control this account) — same posture as
# ../main.tf's AgentifyAdmin permission set; scope down later.
# `SAML:aud` is required on every AssumeRoleWithSAML trust policy — it's the
# fixed endpoint AWS's own SAML consumer service expects, not specific to
# this Azure AD tenant.

resource "aws_iam_role" "saml_admin" {
  name                 = var.role_name
  max_session_duration = var.max_session_duration

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_saml_provider.azure_ad.arn
      }
      Action = "sts:AssumeRoleWithSAML"
      Condition = {
        StringEquals = {
          "SAML:aud" = "https://signin.aws.amazon.com/saml"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "saml_admin" {
  role       = aws_iam_role.saml_admin.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "saml_provider_arn" {
  value = aws_iam_saml_provider.azure_ad.arn
}

output "role_arn" {
  value = aws_iam_role.saml_admin.arn
}

output "role_claim_value" {
  value       = "${aws_iam_role.saml_admin.arn},${aws_iam_saml_provider.azure_ad.arn}"
  description = "Add this exact string as a Role value on your Azure AD Enterprise Application (Single sign-on config → Attributes & Claims → Role claim, or the app's 'AWS Role' assignment) — this is what tells Azure AD it's allowed to issue an assertion for this account/role."
}

output "next_steps" {
  value = <<-EOT
    IAM SAML provider + role created in account ${data.aws_caller_identity.this.account_id}.

    1. In the Entra admin portal, open the Azure AD Enterprise Application
       backing azure-ad-metadata.xml → Single sign-on → Attributes & Claims
       (or "AWS Role" under the app's own SAML settings, depending on how it
       was originally set up).
    2. Add this Role value:
         ${aws_iam_role.saml_admin.arn},${aws_iam_saml_provider.azure_ad.arn}
    3. Assign whichever Azure AD users/groups should get this AWS role
       access under the app's "Users and groups".
    4. Log in via the Azure "My Apps" portal (click the app tile → redirects
       through AWS's SAML endpoint into the console), or from a terminal
       with a SAML-aware CLI tool (e.g. saml2aws, aws-adfs) configured
       against this same Azure AD app — `aws sso login` will NOT work here,
       this isn't an Identity Center setup.
    5. Once you have working AWS credentials, continue with
       infra/terraform/bootstrap and infra/terraform/aws as normal.
  EOT
}
