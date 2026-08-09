# ── EKS cluster ──────────────────────────────────────────────────────────────
# Dev: managed node group (t3.medium × 1–3) — no Fargate to keep it simple.

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = local.name
  cluster_version = var.cluster_version

  vpc_id = module.vpc.vpc_id
  # Control-plane ENI subnets: both public and private, always offered —
  # existing regardless of var.enable_nat_gateway, so this costs nothing and
  # doesn't force a choice at the cluster level (only the node group below
  # actually needs to pick one, per var.enable_nat_gateway).
  subnet_ids                     = concat(module.vpc.public_subnets, module.vpc.private_subnets)
  cluster_endpoint_public_access = true

  # Grant the Terraform executor (SSO role) cluster admin via EKS Access Entries.
  enable_cluster_creator_admin_permissions = true

  # Grant the CI role (GitHub Actions) and the AWS root user cluster admin.
  # The SSO role is already granted via enable_cluster_creator_admin_permissions.
  # Root is never automatically granted EKS access — it needs an explicit entry.
  access_entries = {
    ci = {
      principal_arn = aws_iam_role.ci.arn
      policy_associations = {
        admin = {
          policy_arn   = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = { type = "cluster" }
        }
      }
    }
    root = {
      principal_arn = "arn:aws:iam::${data.aws_caller_identity.this.account_id}:root"
      policy_associations = {
        admin = {
          policy_arn   = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = { type = "cluster" }
        }
      }
    }
  }

  # Enable IRSA (IAM Roles for Service Accounts) — required for backend + adapter.
  enable_irsa = true

  eks_managed_node_groups = {
    main = {
      # Public subnets (default, no NAT cost) unless var.enable_nat_gateway —
      # then private, since that flag exists specifically for accounts whose
      # security policy denies EC2 instances a public IP (see variables.tf).
      subnet_ids = var.enable_nat_gateway ? module.vpc.private_subnets : module.vpc.public_subnets
      # Leaving this unset was expected to defer to the subnet's own
      # map_public_ip_on_launch (confirmed false on both private subnets via
      # `aws ec2 describe-subnets`), but EKS managed node groups' own
      # provisioning path doesn't reliably inherit that the way a
      # self-managed ASG would (confirmed via `aws sts decode-authorization-
      # message` showing ec2:IsLaunchTemplateResource=false on the denied
      # RunInstances call — EKS isn't going through the launch template's
      # NetworkInterfaces array the way a plain ASG launch does). Forcing it
      # explicitly here instead of relying on inheritance.
      network_interfaces = var.enable_nat_gateway ? [{
        associate_public_ip_address = false
        delete_on_termination       = true
        device_index                = 0
      }] : []
      instance_types = [var.node_instance_type]
      ami_type       = "AL2023_x86_64_STANDARD" # correct key in module v20.37.2; AL2 dropped in EKS 1.33
      min_size       = var.node_min
      max_size       = var.node_max
      desired_size   = var.node_desired

      use_latest_ami_release_version = true
    }
  }

  # Core add-ons (aws-ebs-csi-driver omitted — needs its own IRSA role and
  # we use RDS Postgres, not EBS PVCs, so it adds no value here).
  cluster_addons = {
    coredns    = { most_recent = true }
    kube-proxy = { most_recent = true }
    vpc-cni    = { most_recent = true }
  }
}

# ── kubectl / helm providers (configured post-cluster) ────────────────────────
data "aws_eks_cluster_auth" "this" {
  name = module.eks.cluster_name
}

provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
  token                  = data.aws_eks_cluster_auth.this.token
}

provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
    token                  = data.aws_eks_cluster_auth.this.token
  }
}

# ── AWS Load Balancer Controller (Helm) ───────────────────────────────────────
# Watches Ingress objects and creates/updates ALBs.

resource "aws_iam_policy" "alb_controller" {
  name   = "${local.name}-alb-controller"
  policy = file("${path.module}/policies/alb-controller-policy.json")
}

module "alb_controller_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name                              = "${local.name}-alb-controller"
  attach_load_balancer_controller_policy = true

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:aws-load-balancer-controller"]
    }
  }
}

resource "helm_release" "alb_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  namespace  = "kube-system"
  version    = "1.7.2"

  set {
    name  = "clusterName"
    value = module.eks.cluster_name
  }
  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = module.alb_controller_irsa.iam_role_arn
  }

  depends_on = [module.eks]
}

# ── agentify namespace ────────────────────────────────────────────────────────
resource "kubernetes_namespace" "agentify" {
  metadata {
    name = "agentify"
  }
  depends_on = [module.eks]
}
