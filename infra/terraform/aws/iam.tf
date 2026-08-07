# ── IRSA roles (IAM Roles for Service Accounts) ──────────────────────────────
# Each workload gets a least-privilege IAM role bound to its K8s ServiceAccount
# via OIDC. No long-lived credentials in Pods.

# Backend: needs DynamoDB (pod registry) + Secrets Manager (DB)
module "backend_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "${local.name}-backend"

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["agentify:agentify-backend"]
    }
  }

  role_policy_arns = {
    secrets  = aws_iam_policy.backend_secrets.arn
    dynamodb = aws_iam_policy.backend_dynamodb.arn
  }
}

resource "aws_iam_policy" "backend_secrets" {
  name = "${local.name}-backend-secrets"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = [
          aws_secretsmanager_secret.db.arn,
        ]
      },
      # Integration.Token secrets (ADR 0025): one dynamic, per-row secret per
      # onboarded cluster, created/rotated/deleted by the backend itself at
      # admin-CRUD time — not known at `terraform apply` time like the
      # static secrets above, so this is a prefix grant, not a fixed ARN
      # list. Mirrors the CI role's Langfuse-secret grant below (the only
      # other role in this file that both creates and reads secrets).
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
          "secretsmanager:TagResource",
          "secretsmanager:DeleteSecret",
        ]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.this.account_id}:secret:${var.project}/${var.env}/integrations/*"
      }
    ]
  })
}

resource "aws_iam_policy" "backend_dynamodb" {
  name = "${local.name}-backend-dynamodb"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
          "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan",
        ]
        Resource = [
          aws_dynamodb_table.pod_registry.arn,
          "${aws_dynamodb_table.pod_registry.arn}/index/*",
        ]
      }
    ]
  })
}

# Agent: Secrets Manager — ANTHROPIC_API_KEY + Langfuse prompt-management keys
module "agent_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "${local.name}-agent"

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["agentify:agentify-agent"]
    }
  }

  role_policy_arns = {
    secrets = aws_iam_policy.agent_secrets.arn
  }
}

resource "aws_iam_policy" "agent_secrets" {
  name = "${local.name}-agent-secrets"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = [
          aws_secretsmanager_secret.anthropic.arn,
          aws_secretsmanager_secret.langfuse.arn,
        ]
      }
    ]
  })
}

# Discovery (the merged per-cluster collector, ADR 0027): Kubernetes API
# access is via in-cluster RBAC, not IAM — it authenticates to the Hub with
# COLLECTOR_TOKEN (agentify-discovery-secret), a plain K8s Secret, not
# Secrets Manager. No IRSA role needed.

# CI role (assumed by GitHub Actions via OIDC — no long-lived keys in CI)
#
# var.create_github_oidc_provider (default true) toggles whether this
# resource is actually created. Set to false for an account that already
# has a token.actions.githubusercontent.com OIDC provider registered by
# something else (e.g. a shared/corporate account another team's CI already
# depends on) — AWS allows only one OIDC provider per issuer URL per
# account, so leaving this true there fails with EntityAlreadyExists.
# local.github_oidc_provider_arn below always resolves to the right ARN
# either way, since the ARN shape is deterministic from the account ID —
# no data-source lookup or import needed for the false case, and the
# pre-existing provider is never touched by this Terraform at all.
resource "aws_iam_openid_connect_provider" "github_actions" {
  count = var.create_github_oidc_provider ? 1 : 0

  url = "https://token.actions.githubusercontent.com"

  client_id_list = ["sts.amazonaws.com"]

  # GitHub's current OIDC thumbprint — rotate if GitHub rotates their cert.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

locals {
  github_oidc_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github_actions[0].arn : "arn:aws:iam::${data.aws_caller_identity.this.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

resource "aws_iam_role" "ci" {
  name = "${local.name}-ci"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = local.github_oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringLike = {
          # Locked to these repos only. agentify2 added 2026-07-20 so it can
          # share this same CI role/cluster/ECR (deliberate — not isolated infra).
          # kschandramouli7/agentify2 added 2026-08-07 — a second GitHub
          # account's fork/mirror of agentify2, used to redeploy this stack
          # into a separate AWS account; still sharing this role definition
          # (each AWS account gets its own copy of this role via its own
          # `terraform apply`, only the trust policy's allowed repos differ).
          #
          # GitHub's OIDC `sub` claim embeds immutable owner/repo IDs inline
          # (e.g. "repo:kschandramouli@47712058/agentify2@1306530798:ref:...")
          # rather than the plain "repo:OWNER/REPO:..." form docs commonly show —
          # confirmed 2026-07-20 by decoding an actual token. Both forms are kept
          # here so this survives if GitHub ever reverts the format.
          "token.actions.githubusercontent.com:sub" = [
            "repo:kschandramouli/agentify:*",
            "repo:kschandramouli/agentify2:*",
            "repo:kschandramouli@*/agentify:*",
            "repo:kschandramouli@*/agentify2@*:*",
            "repo:kschandramouli7/agentify2:*",
            "repo:kschandramouli7@*/agentify2@*:*",
          ]
        }
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "ci" {
  name = "${local.name}-ci"
  role = aws_iam_role.ci.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # ECR: create repos + push images.
      # GetAuthorizationToken is account-level (no resource ARN).
      # Repo-level actions use a prefix wildcard so adding new agentify/* repos
      # never requires a terraform apply to update this policy.
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:DescribeRepositories", "ecr:DescribeImages",
          "ecr:CreateRepository",
          "ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload", "ecr:PutImage",
        ]
        Resource = "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.this.account_id}:repository/agentify/*"
      },
      # IAM: read role ARNs for IRSA substitution in manifests during deploy
      {
        Effect = "Allow"
        Action = ["iam:GetRole"]
        Resource = [
          "arn:aws:iam::${data.aws_caller_identity.this.account_id}:role/agentify-dev-*",
        ]
      },
      # Secrets Manager: read secrets to sync into K8s during deploy
      {
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = [
          aws_secretsmanager_secret.db.arn,
          aws_secretsmanager_secret.anthropic.arn,
        ]
      },
      # Secrets Manager: create + populate the Langfuse secret via terraform apply
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
          "secretsmanager:TagResource",
          "secretsmanager:DeleteSecret",
        ]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.this.account_id}:secret:${var.project}/${var.env}/langfuse*"
      },
      # IAM: create/update policy versions for the targeted terraform apply
      {
        Effect = "Allow"
        Action = [
          "iam:GetPolicy",
          "iam:GetPolicyVersion",
          "iam:ListPolicyVersions",
          "iam:CreatePolicyVersion",
          "iam:DeletePolicyVersion",
          "iam:SetDefaultPolicyVersion",
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.this.account_id}:policy/${local.name}-agent-secrets"
      },
      # S3 + DynamoDB: Terraform remote state (targeted apply)
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::agentify-tfstate-f6e00ef8",
          "arn:aws:s3:::agentify-tfstate-f6e00ef8/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = "arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.this.account_id}:table/agentify-tfstate-lock"
      },
      # EKS: kubectl + pause/resume.
      # Nodegroup ARN format differs from cluster ARN: nodegroup/cluster/ng/uuid
      {
        Effect   = "Allow"
        Action   = ["eks:DescribeCluster", "eks:ListNodegroups"]
        Resource = module.eks.cluster_arn
      },
      {
        Effect   = "Allow"
        Action   = ["eks:UpdateNodegroupConfig", "eks:DescribeNodegroup"]
        Resource = "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.this.account_id}:nodegroup/${local.name}/*/*"
      },
      # RDS: pause/resume. CreateDBSnapshot needs both the instance ARN and
      # the snapshot ARN (snapshot name is dynamic, hence the wildcard).
      {
        Effect   = "Allow"
        Action   = ["rds:StopDBInstance", "rds:StartDBInstance", "rds:DescribeDBInstances"]
        Resource = aws_db_instance.this.arn
      },
      {
        Effect = "Allow"
        Action = ["rds:CreateDBSnapshot", "rds:DescribeDBSnapshots"]
        Resource = [
          aws_db_instance.this.arn,
          "arn:aws:rds:${var.aws_region}:${data.aws_caller_identity.this.account_id}:snapshot:agentify-dev-pause-*",
        ]
      },
    ]
  })
}
