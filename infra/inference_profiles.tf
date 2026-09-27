# ── Bedrock application inference profiles ────────────────────────────────
#
# Two reasons these exist rather than calling a model id directly:
#
#   1. Cost attribution. Tags on an application inference profile flow into
#      Cost Explorer, so Bedrock spend splits per agent role instead of
#      arriving as one undifferentiated line. Activate the tag keys under
#      Billing > Cost allocation tags for them to become filterable.
#
#   2. Data residency. The "us." system profiles are bound to US regions.
#      Copying from one inherits that binding, which is how a regulated
#      workload evidences that inference stayed in-country.
#
# One profile per role, mirroring the orchestrator / recommender split.

data "aws_caller_identity" "current" {}

locals {
  # System-defined profiles to copy from. "us." prefix = US-region bound.
  orchestrator_source = "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
  # WAS Claude 3 Haiku, which reached end of life and took the live chat down.
  # The failure is worth describing because nothing warned first: the profile
  # stayed valid, the harness stayed healthy, and every /chat returned 500 with
  #
  #   ResourceNotFoundException when calling ConverseStream:
  #   This model version has reached the end of its life.
  #
  # Only the Lambda log said so. This is the THIRD chatbot in this account
  # killed by a model retirement, after the Azure and GCP ones.
  #
  # This is deliberately the same model as the orchestrator, which collapses the
  # cheap/expensive tier split the file header describes. That is not tidiness,
  # it is the only working option:
  #
  #   us.anthropic.claude-sonnet-4-5    invokable
  #   us.anthropic.claude-haiku-4-5     AccessDenied - needs an AWS Marketplace
  #                                     subscription the account has not taken
  #   anthropic.claude-3-haiku          end of life
  #
  # Verified by calling bedrock-runtime converse on each, not by reading
  # list-foundation-models, which reports Haiku 4.5 as ACTIVE and says nothing
  # about whether this account may call it.
  #
  # COST: this raises the high-volume role from $0.25/$1.25 per million tokens
  # to $3/$15 - about 12x. The per-visitor quota is what bounds it. Point this
  # back at a Haiku profile if the account ever subscribes to Haiku 4.5; the two
  # profiles still exist separately so Cost Explorer keeps the per-role split.
  recommender_source  = "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

# The reasoning role: plans, decides which tools to call, reads results.
resource "aws_bedrock_inference_profile" "orchestrator" {
  name = "cloudprice-orchestrator"
  # Bedrock validates descriptions against ([0-9a-zA-Z:.][ _-]?)+ so a
  # separator may only follow an alphanumeric. No " - ", no commas.
  description = "Reasoning role for the cloudprice agent tool selection and synthesis"

  model_source {
    copy_from = local.orchestrator_source
  }

  tags = {
    AgentRole = "orchestrator"
    CostGroup = "cloudprice-agent"
  }
}

# The high-volume, low-cost role: short answers over tool output.
resource "aws_bedrock_inference_profile" "recommender" {
  name        = "cloudprice-recommender"
  description = "High volume role for the cloudprice agent summarising tool results"

  model_source {
    copy_from = local.recommender_source
  }

  tags = {
    AgentRole = "recommender"
    CostGroup = "cloudprice-agent"
  }
}
