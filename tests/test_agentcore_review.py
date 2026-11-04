"""review_agentcore: coded rules over a synthetic `terraform show -json` plan.

The plan below is made up to trip every rule once; nothing in it comes from a
real deployment. Spend figures are worked by hand from tests/test_bill_ai.py's
fixed MODELS catalogue.
"""

from __future__ import annotations

import json

import pytest

from cloudprice_mcp import dispatch
from cloudprice_mcp.finops.agentcore_review import PlanError, review_agentcore
from tests.test_bill_ai import ARCHIVE, COLS, MODELS, bedrock
from tests.test_bill_insights import row, write

RUNTIME, HARNESS = "aws_bedrockagentcore_agent_runtime", "aws_bedrockagentcore_harness"


def res(address, kind, **values):
    return {"address": address, "mode": "managed", "type": kind, "values": values}


def invoke(resource):
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "bedrock:InvokeModel*", "Resource": resource}
            ],
        }
    )


def plan(*extra, runtime=None):
    chat = {
        "network_configuration": [{"network_mode": "PUBLIC"}],
        "lifecycle_configuration": [{"idle_runtime_session_timeout": 3600, "max_lifetime": 28800}],
        "environment_variables": {
            "OPENAI_API_KEY": "sk-not-a-real-key",
            "DB_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:000000000000:secret:db",
            "MAX_TOKENS": "4000",
            "LOG_LEVEL": "info",
        },
        "tags_all": {"app": "demo"},
    }
    chat.update(runtime or {})
    helper = res(
        "module.helper.aws_bedrockagentcore_harness.helper",
        HARNESS,
        max_iterations=10,
        environment=[
            {
                "agentcore_runtime_environment": [
                    {
                        "network_configuration": [{"network_mode": "VPC"}],
                        "lifecycle_configuration": [
                            {"idle_runtime_session_timeout": 600, "max_lifetime": 3600}
                        ],
                    }
                ]
            }
        ],
        tags_all={"app": "demo"},
    )
    scoped = res(
        "module.helper.aws_iam_role.scoped",
        "aws_iam_role",
        inline_policy=[
            {"policy": invoke("arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-*")}
        ],
    )
    return {
        "format_version": "1.2",
        "planned_values": {
            "root_module": {
                "resources": [
                    res("aws_bedrockagentcore_agent_runtime.chat", RUNTIME, **chat),
                    res(
                        "aws_bedrockagentcore_gateway.tools",
                        "aws_bedrockagentcore_gateway",
                        authorizer_type="NONE",
                        exception_level="DEBUG",
                        tags=None,
                        tags_all={},
                    ),
                    res("aws_iam_role_policy.agent", "aws_iam_role_policy", policy=invoke("*")),
                    res("aws_iam_policy.later", "aws_iam_policy"),  # known only after apply
                    res(
                        "aws_cloudwatch_log_group.agent",
                        "aws_cloudwatch_log_group",
                        retention_in_days=0,
                    ),
                    *extra,
                ],
                "child_modules": [{"resources": [helper, scoped]}],
            }
        },
    }


def save(tmp_path, p, name="plan.json"):
    path = tmp_path / name
    path.write_text(json.dumps(p), encoding="utf-8")
    return str(path)


def rules(r):
    return [(f["rule"], f["resource"]) for f in r["findings"]]


def test_every_rule_fires_once_in_severity_order(tmp_path):
    r = review_agentcore(save(tmp_path, plan()))
    assert rules(r) == [
        ("S2", "aws_bedrockagentcore_gateway.tools"),
        ("S5", "aws_bedrockagentcore_agent_runtime.chat"),
        ("C3", "module.helper.aws_bedrockagentcore_harness.helper"),
        ("O1", "(plan)"),
        ("O2", "aws_bedrockagentcore_gateway.tools"),
        ("S1", "aws_bedrockagentcore_agent_runtime.chat"),
        ("S3", "aws_bedrockagentcore_gateway.tools"),
        ("S4", "aws_iam_role_policy.agent"),
        ("C1", "aws_bedrockagentcore_agent_runtime.chat"),
        ("C2", "aws_bedrockagentcore_agent_runtime.chat"),
        ("C4", "aws_cloudwatch_log_group.agent"),
    ]
    assert r["resources"] == {"agent_runtime": 1, "gateway": 1, "harness": 1}
    assert r["not_checked"] == ["aws_iam_policy.later: the policy is not known until apply"]
    assert r["summary"] == {
        "high": 2,
        "medium": 6,
        "low": 3,
        "info": 0,
        "skipped": 0,
        "monthly_saving_from_spend_findings": 0,
    }
    by = {f["rule"]: f for f in r["findings"]}
    assert by["S5"]["value"] == ["OPENAI_API_KEY"]  # names only; the ARN and MAX_TOKENS pass
    assert "sk-not-a-real-key" not in json.dumps(r)
    assert by["C1"]["saving"]["usd"] == 7.09  # 45 idle min x 0.00945/GB-h x 1,000 sessions
    assert by["C3"]["attribute"] == "max_tokens, timeout_seconds"
    assert by["S1"]["doc"].endswith("agentcore-vpc.html")
    assert r["headline"].startswith(
        "11 findings (2 high, 6 medium, 3 low) across 1 runtime, 1 harness and 1 gateway. "
        "Top: S2 aws_bedrockagentcore_gateway.tools: gateway accepts callers without "
        "authentication."
    )


def test_alarm_on_agentcore_namespace_clears_o1(tmp_path):
    direct = res(
        "aws_cloudwatch_metric_alarm.errors",
        "aws_cloudwatch_metric_alarm",
        namespace="AWS/Bedrock-AgentCore",
    )
    query = res(
        "aws_cloudwatch_metric_alarm.math",
        "aws_cloudwatch_metric_alarm",
        namespace=None,
        metric_query=[{"metric": [{"namespace": "bedrock-agentcore"}]}],
    )
    other = res(
        "aws_cloudwatch_metric_alarm.lambda", "aws_cloudwatch_metric_alarm", namespace="AWS/Lambda"
    )
    for alarm, cleared in ((direct, True), (query, True), (other, False)):
        r = review_agentcore(save(tmp_path, plan(alarm)))
        assert ("O1", "(plan)") not in rules(r) if cleared else ("O1", "(plan)") in rules(r)


def test_lifecycle_defaults_and_v2(tmp_path):
    r = review_agentcore(
        save(tmp_path, plan(runtime={"lifecycle_configuration": [], "platform_version": "V2"}))
    )
    c2 = [f for f in r["findings"] if f["rule"] == "C2"]
    assert [(f["severity"], f["value"]) for f in c2] == [("info", None)]  # 8 h default
    r = review_agentcore(
        save(
            tmp_path,
            plan(runtime={"platform_version": "V2"}),
        )
    )
    assert "C1" not in {f["rule"] for f in r["findings"]}  # v2 reclaims idle memory


def test_skips_need_a_reason_and_stale_ones_are_reported(tmp_path):
    path = save(tmp_path, plan())
    r = review_agentcore(
        path,
        skips=[
            {
                "rule": "S1",
                "resource": "aws_bedrockagentcore_agent_runtime.chat",
                "reason": "public demo agent, no private data",
            },
            {"rule": "C4", "reason": "retention is set by an org-wide policy"},
            {"rule": "S2", "resource": "aws_bedrockagentcore_gateway.other", "reason": "old"},
        ],
    )
    assert [(f["rule"], f["skip_reason"]) for f in r["skipped"]] == [
        ("S1", "public demo agent, no private data"),
        ("C4", "retention is set by an org-wide policy"),
    ]
    assert r["summary"]["skipped"] == 2
    assert "S1" not in {f["rule"] for f in r["findings"]}
    assert r["warnings"] == [
        "skip S2 on aws_bedrockagentcore_gateway.other matched no finding; remove it if it is stale"
    ]
    with pytest.raises(PlanError, match="needs a reason"):
        review_agentcore(path, skips=[{"rule": "S1"}])
    with pytest.raises(PlanError, match="one of S1"):
        review_agentcore(path, skips=[{"rule": "Z9", "reason": "x"}])


def test_spend_for_one_tagged_agent(tmp_path):
    tag = '{"agent": "chat"}'
    rows = [
        # Claude Sonnet 4.6: input paid 20% over list, 1M of 11M input-side tokens cached.
        bedrock("USE1-Claude4.6Sonnet-input-tokens", 36.0, 10e6, Tags=tag),
        bedrock("USE1-Claude4.6Sonnet-output-tokens", 30.0, 2e6, Tags=tag),
        bedrock("USE1-Claude4.6Sonnet-cache-read-input-token-count", 0.3, 1e6, Tags=tag),
        bedrock("USE1-Runtime-vCPU-Hours", 4.0, ServiceName="Amazon Bedrock AgentCore", Tags=tag),
        bedrock("USE1-Gateway-Invocations", 1.0, ServiceName="Amazon Bedrock AgentCore", Tags=tag),
        bedrock(
            "USE1-Memory-ShortTerm-Events", 2.0, ServiceName="Amazon Bedrock AgentCore", Tags=tag
        ),
        row("2026-10-15", "AmazonCloudWatch", 0.5, category="Management and Governance", Tags=tag),
        bedrock("USE1-Claude4.6Sonnet-input-tokens", 100.0, 30e6),  # another agent
        row("2026-10-15", "Amazon EC2", 50.0),
    ]
    bill = str(write(tmp_path / "bill.csv", rows, columns=COLS))
    r = review_agentcore(
        save(tmp_path, plan()), bill, tag="agent=chat", models=MODELS, archive=ARCHIVE
    )
    spend = r["spend"]
    assert spend["components"] == {
        "model_tokens": 66.3,
        "runtime": 4.0,
        "memory": 2.0,
        "gateway": 1.0,
        "logs_and_monitoring": 0.5,
    }
    assert (spend["total"], spend["period"]["days"], spend["monthly_total"]) == (73.8, 1, 2243.52)
    sp = {f["rule"]: f for f in r["findings"] if f["rule"].startswith("SP")}
    # 0.1 x 11M x (3.00 - 0.30) / 1M = 2.97 a day, x 30.4
    assert (sp["SP1"]["saving"]["usd"], sp["SP1"]["severity"]) == (90.29, "medium")
    assert sp["SP2"]["saving"]["usd"] == 182.4  # (66.30 - 60.30) x 30.4
    assert r["summary"]["monthly_saving_from_spend_findings"] == 272.69
    assert "The agent costs about 2,243.52 a month; 90% is model tokens." in r["headline"]


def test_spend_problems_do_not_stop_the_review(tmp_path):
    bill = str(write(tmp_path / "b.csv", [row("2026-10-15", "Amazon EC2", 5.0)], columns=COLS))
    r = review_agentcore(save(tmp_path, plan()), bill, tag="agent=chat", models=MODELS)
    assert r["spend"] is None
    assert "spend not analysed: no rows carry the tag agent=chat" in r["warnings"]
    assert len(r["findings"]) == 11


def test_input_checks(tmp_path):
    with pytest.raises(PlanError, match="only .json"):
        review_agentcore(str(tmp_path / "plan.tfplan"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(PlanError, match="not valid JSON"):
        review_agentcore(str(bad))
    with pytest.raises(PlanError, match="no planned_values"):
        review_agentcore(save(tmp_path, {"resource_changes": []}))
    empty = {
        "planned_values": {"root_module": {"resources": [res("aws_s3_bucket.b", "aws_s3_bucket")]}}
    }
    with pytest.raises(PlanError, match="no AgentCore resources"):
        review_agentcore(save(tmp_path, empty))


def test_state_json_is_read_too(tmp_path):
    state = {"values": plan()["planned_values"]}
    assert len(review_agentcore(save(tmp_path, state))["findings"]) == 11


def test_review_agentcore_is_local_only(tmp_path):
    args = {"plan_path": save(tmp_path, plan())}
    assert "review_agentcore" not in dispatch.tool_names()
    assert dispatch.call("review_agentcore", args) == {"error": "Unknown tool: review_agentcore"}
    assert dispatch.call("review_agentcore", args, allow_local=True)["kind"] == "agentcore_review"
    assert "error" in dispatch.call("review_agentcore", {}, allow_local=True)
