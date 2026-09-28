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
  # The cheap, high-volume role. Two model retirements are recorded here because
  # each one took the live chat down and neither announced itself.
  #
  # Claude 3 Haiku reached end of life on 2026-09-27. Nothing warned: the profile
  # stayed valid, the harness stayed healthy, no alarm fired, and terraform plan
  # reported no changes, because no config was wrong. Every /chat returned 500
  # and only the Lambda log said why:
  #
  #   ResourceNotFoundException when calling ConverseStream:
  #   This model version has reached the end of its life.
  #
  # The stopgap was Sonnet 4.5 - correct but 12x the token price - because
  # Haiku 4.5 needed an AWS Marketplace subscription the account had not taken.
  # That subscription was accepted on 2026-09-28 (usage-based, no fixed fee), so
  # the cheap tier exists again and this points back at it.
  #
  #   anthropic.claude-3-haiku          end of life
  #   us.anthropic.claude-haiku-4-5     invokable since the agreement
  #   us.anthropic.claude-sonnet-4-5    invokable, 3x the price
  #
  # Verified by calling bedrock-runtime converse on each, not by reading
  # list-foundation-models, which reported Haiku 4.5 as ACTIVE the whole time it
  # was unusable and would have sent you looking in the wrong place.
  #
  # COST: $1/$5 per million tokens against Sonnet's $3/$15. With the 150/day cap
  # in cloudprice-web that moves a worst-case day from ~$88/month to ~$29.
  #
  # Note the "us." prefix is required. The bare foundation-model id is rejected -
  # this model can only be called through an inference profile.
  recommender_source  = "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0"
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
