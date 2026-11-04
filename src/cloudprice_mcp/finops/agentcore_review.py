"""review_agentcore — a Well-Architected review of an AgentCore deployment.

Reads what Terraform is about to build (`terraform show -json plan.out`) or has
built (`terraform show -json` on state) and checks every AgentCore Runtime,
Harness and Gateway, and the IAM policies, log groups and alarms around them,
against coded rules. Each rule is deterministic and cites the AWS page it rests
on. Given a FOCUS bill as well, it adds what the agent costs split by component,
and the spend findings from analyze_ai_spend.

  S1-S5 security   C1-C4 cost   O1-O2 operations   SP1-SP2 spend (needs a bill)

Same privacy rules as the bill tools: files are read locally, nothing is sent
anywhere, and it is a local-only tool. The only plan values echoed back are the
settings a finding is about; environment variables are named, never shown.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from .agent_cost import _hosting_prices
from .bill_ai import _AI_SERVICE, _TEXT_COLUMNS, ai_spend_from_rows
from .bill_insights import _load, _tag, _tags
from .focus_ingest import BillError, row_cost

MAX_PLAN_BYTES = 100 * 1024 * 1024
SEVERITIES = ("high", "medium", "low", "info")
_GUIDE = "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/"
_SECURITY, _COST, _OPS = "Security", "Cost optimization", "Operational excellence"

RULES: dict[str, dict[str, str]] = {
    "S1": {
        "title": "Agent runs on the public network, not in your VPC",
        "severity": "medium",
        "pillar": _SECURITY,
        "fix": 'network_configuration { network_mode = "VPC"  network_mode_config { '
        "subnets = [...]  security_groups = [...] } }",
        "doc": _GUIDE + "agentcore-vpc.html",
    },
    "S2": {
        "title": "Gateway accepts callers without authentication",
        "severity": "high",
        "pillar": _SECURITY,
        "fix": 'authorizer_type = "AWS_IAM" (callers sign with SigV4), or "CUSTOM_JWT" '
        "with an authorizer_configuration for your identity provider",
        "doc": _GUIDE + "gateway-inbound-auth.html",
    },
    "S3": {
        "title": "Gateway returns debug error details to callers",
        "severity": "medium",
        "pillar": _SECURITY,
        "fix": "remove exception_level once debugging is done; callers then get a generic error",
        "doc": _GUIDE + "gateway-debug-messages.html",
    },
    "S4": {
        "title": "IAM policy lets the agent invoke any Bedrock model",
        "severity": "medium",
        "pillar": _SECURITY,
        "fix": "scope Resource to the models the agent uses: "
        "arn:aws:bedrock:<region>::foundation-model/<model-id> and its inference-profile ARN",
        "doc": _GUIDE + "runtime-permissions.html",
    },
    "S5": {
        "title": "Secret-looking environment variables hold plain values",
        "severity": "high",
        "pillar": _SECURITY,
        "fix": "keep the value in Secrets Manager or an AgentCore Identity credential "
        "provider, pass its ARN, and read it at run time",
        "doc": _GUIDE + "identity.html",
    },
    "C1": {
        "title": "Idle sessions are kept longer than the AWS default",
        "severity": "low",
        "pillar": _COST,
        "fix": "lifecycle_configuration { idle_runtime_session_timeout = 900 } or lower",
        "doc": _GUIDE + "runtime-lifecycle-settings.html",
    },
    "C2": {
        "title": "Sessions can live longer than AWS's production pattern (4 hours)",
        "severity": "low",
        "pillar": _COST,
        "fix": "lifecycle_configuration { max_lifetime = 14400 }",
        "doc": _GUIDE + "runtime-lifecycle-settings.html",
    },
    "C3": {
        "title": "Harness has no cap on loop iterations, tokens or run time",
        "severity": "medium",
        "pillar": _COST,
        "fix": "max_iterations = 15, max_tokens = 60000, timeout_seconds = 300 "
        "(set the ones that are missing, sized to the task)",
        "doc": _GUIDE + "harness.html",
    },
    "C4": {
        "title": "Log group keeps logs forever",
        "severity": "low",
        "pillar": _COST,
        "fix": "retention_in_days = 30 (or what your audit policy requires)",
        "doc": "https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/"
        "Working-with-log-groups-and-streams.html",
    },
    "O1": {
        "title": "No alarm watches the agent's AgentCore metrics",
        "severity": "medium",
        "pillar": _OPS,
        "fix": "an aws_cloudwatch_metric_alarm on the AgentCore namespace (errors, "
        "throttles, latency) with an alarm action",
        "doc": _GUIDE + "observability.html",
    },
    "O2": {
        "title": "No tags, so the agent's spend cannot be told apart on the bill",
        "severity": "medium",
        "pillar": _COST,
        "fix": "provider default_tags { tags = { app = ..., owner = ... } }, then activate "
        "them as cost allocation tags in Billing",
        "doc": "https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/cost-alloc-tags.html",
    },
    "SP1": {
        "title": "Little of the prompt is served from cache",
        "severity": "medium",
        "pillar": _COST,
        "fix": "cache the stable prefix (system prompt, tool definitions) with prompt caching",
        "doc": "https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html",
    },
    "SP2": {
        "title": "Paying more than 5% over the model's list price",
        "severity": "medium",
        "pillar": _COST,
        "fix": "check the inference tier (priority, cross-region profile) the agent calls; "
        "standard on-demand is the list price",
        "doc": "https://aws.amazon.com/bedrock/pricing/",
    },
}

_RUNTIME, _HARNESS = "aws_bedrockagentcore_agent_runtime", "aws_bedrockagentcore_harness"
_GATEWAY = "aws_bedrockagentcore_gateway"
_IAM = ("aws_iam_policy", "aws_iam_role_policy", "aws_iam_role")
_DEFAULT_IDLE, _PRODUCTION_LIFETIME = 900, 14400
_HARNESS_CAPS = ("max_iterations", "max_tokens", "timeout_seconds")
# Names that hold a secret, and suffixes that mark a reference to one instead.
_SECRET_NAME = re.compile(
    r"(^|_)(SECRET|PASSWORD|PASSWD|PWD|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIALS?)($|_)", re.I
)
_REFERENCE_NAME = re.compile(r"_(ARN|ID|NAME|PATH|PARAM|PARAMETER|URL|URI|REGION)$", re.I)
# A resource pattern that matches made-up model ARNs allows every model.
_ANY_MODEL = (
    "arn:aws:bedrock:us-east-1::foundation-model/zz-any-model",
    "arn:aws:bedrock:us-east-1:000000000000:inference-profile/zz-any-profile",
)
# AgentCore usage lines, split by the words in their usage type.
_AGENTCORE_PARTS = (
    ("gateway", re.compile(r"gateway|tool[-_ ]?index|search[-_ ]?api", re.I)),
    ("browser", re.compile(r"browser", re.I)),
    ("code_interpreter", re.compile(r"code[-_ ]?interpreter", re.I)),
    (
        "memory",
        re.compile(
            r"memory[-_ ]?(event|record|retriev|ingest|storage)|short[-_ ]?term|long[-_ ]?term",
            re.I,
        ),
    ),
    ("runtime", re.compile(r"runtime|vcpu|gb[-_ ]?h|harness", re.I)),
)
_DAYS_PER_MONTH = 30.4


class PlanError(ValueError):
    """A plan that cannot be reviewed; the message never contains file content."""


def read_plan(file_path: str) -> dict[str, Any]:
    """The JSON of `terraform show -json`, for a saved plan or for state."""
    path = Path(file_path).expanduser()
    if path.suffix.lower() != ".json":
        raise PlanError("only .json files from `terraform show -json` are read")
    if not path.is_file():
        raise PlanError(f"no such file: {path}")
    if path.stat().st_size > MAX_PLAN_BYTES:
        raise PlanError(f"file is larger than {MAX_PLAN_BYTES // (1024 * 1024)} MB")
    try:
        plan = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        raise PlanError("not valid JSON") from e
    if not isinstance(plan, dict) or not isinstance(
        plan.get("planned_values") or plan.get("values"), dict
    ):
        raise PlanError(
            "this is not `terraform show -json` output: it has no planned_values or values"
        )
    return plan


def _resources(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Managed resources from the root module and every child module."""
    values = plan.get("planned_values") or plan.get("values") or {}
    out: list[dict[str, Any]] = []
    stack = [values.get("root_module") or {}]
    while stack:
        module = stack.pop()
        out += [r for r in module.get("resources", []) if r.get("mode", "managed") == "managed"]
        stack += module.get("child_modules", [])
    return sorted(out, key=lambda r: str(r.get("address", "")))


def _block(values: Any, *path: str) -> dict[str, Any]:
    """A nested block, which plan JSON writes as a list of one object; {} if absent."""
    cur = values
    for key in path:
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(key)
    if isinstance(cur, list):
        cur = cur[0] if cur else None
    return cur if isinstance(cur, dict) else {}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _finding(
    rule: str,
    resource: str,
    detail: str,
    *,
    attribute: str | None = None,
    value: Any = None,
    severity: str | None = None,
    saving: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = RULES[rule]
    return {
        "rule": rule,
        "severity": severity or meta["severity"],
        "pillar": meta["pillar"],
        "title": meta["title"],
        "resource": resource,
        "attribute": attribute,
        "value": value,
        "detail": detail,
        "fix": meta["fix"],
        "saving": saving,
        "doc": meta["doc"],
    }


def _check_compute(r: dict[str, Any], gb_hour: float) -> list[dict[str, Any]]:
    """S1, S5, C1, C2 for a Runtime or a Harness, and C3 for a Harness."""
    address, values = r["address"], r.get("values") or {}
    if r["type"] == _HARNESS:
        prefix, cfg = (
            "environment.agentcore_runtime_environment.",
            _block(values, "environment", "agentcore_runtime_environment"),
        )
    else:
        prefix, cfg = "", values
    out = []

    if _block(cfg, "network_configuration").get("network_mode") == "PUBLIC":
        out.append(
            _finding(
                "S1",
                address,
                "network_mode is PUBLIC, so the agent's outbound traffic leaves through "
                "AWS's network rather than your VPC, its egress controls and endpoints.",
                attribute=prefix + "network_configuration.network_mode",
                value="PUBLIC",
            )
        )

    env = values.get("environment_variables")
    names = sorted(
        k
        for k, v in (env.items() if isinstance(env, dict) else [])
        if _SECRET_NAME.search(k)
        and not _REFERENCE_NAME.search(k)
        and isinstance(v, str)
        and v
        and not v.startswith(("arn:", "/", "{{resolve:"))
    )
    if names:
        out.append(
            _finding(
                "S5",
                address,
                f"{', '.join(names)} hold plain values. Environment variables are stored "
                "in Terraform state and returned by the AgentCore API.",
                attribute="environment_variables",
                value=names,
            )
        )

    life = _block(cfg, "lifecycle_configuration")
    idle, lifetime = (
        _int(life.get("idle_runtime_session_timeout")),
        _int(life.get("max_lifetime")),
    )
    if idle and idle > _DEFAULT_IDLE and values.get("platform_version") in (None, "", "V1"):
        usd = (idle - _DEFAULT_IDLE) / 3600 * gb_hour * 1000
        out.append(
            _finding(
                "C1",
                address,
                f"Sessions stay up {idle // 60} minutes after their last request "
                f"(AWS default 15). On Runtime v1 memory is billed for that idle time.",
                attribute=prefix + "lifecycle_configuration.idle_runtime_session_timeout",
                value=idle,
                saving={
                    "usd": round(usd, 2),
                    "basis": "per 1,000 sessions left idle to the timeout, per GB of memory "
                    "held, at the Runtime v1 rate",
                },
            )
        )
    if lifetime is None:
        out.append(
            _finding(
                "C2",
                address,
                "max_lifetime is not set, so a session can run 8 hours (the default and the "
                "microVM maximum) and keeps running the code it started with after a deploy.",
                attribute=prefix + "lifecycle_configuration.max_lifetime",
                severity="info",
            )
        )
    elif lifetime > _PRODUCTION_LIFETIME:
        out.append(
            _finding(
                "C2",
                address,
                f"Sessions can run {lifetime / 3600:g} hours; AWS's production API pattern "
                "is 4. A long session keeps running the code it started with after a deploy.",
                attribute=prefix + "lifecycle_configuration.max_lifetime",
                value=lifetime,
            )
        )

    if r["type"] == _HARNESS:
        missing = [k for k in _HARNESS_CAPS if not values.get(k)]
        if missing:
            out.append(
                _finding(
                    "C3",
                    address,
                    f"{', '.join(missing)} not set: a looping or verbose run is stopped "
                    "only by AWS defaults, and every extra step is billed model tokens.",
                    attribute=", ".join(missing),
                )
            )
    return out


def _check_gateway(r: dict[str, Any]) -> list[dict[str, Any]]:
    address, values = r["address"], r.get("values") or {}
    out = []
    if values.get("authorizer_type") == "NONE":
        out.append(
            _finding(
                "S2",
                address,
                "authorizer_type is NONE: anyone who can reach the gateway URL can list "
                "and call its tools.",
                attribute="authorizer_type",
                value="NONE",
            )
        )
    if values.get("exception_level") == "DEBUG":
        out.append(
            _finding(
                "S3",
                address,
                "exception_level is DEBUG: Lambda, authorizer and validation error details "
                "go back to the caller instead of a generic error.",
                attribute="exception_level",
                value="DEBUG",
            )
        )
    return out


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _allows_any_model(policy: dict[str, Any]) -> bool:
    for s in _as_list(policy.get("Statement")):
        if not isinstance(s, dict) or s.get("Effect") != "Allow":
            continue
        actions = [a.lower() for a in _as_list(s.get("Action")) if isinstance(a, str)]
        if not any(fnmatchcase("bedrock:invokemodel", a) for a in actions):
            continue
        resources = [x for x in _as_list(s.get("Resource")) if isinstance(x, str)]
        if any(fnmatchcase(probe, x) for probe in _ANY_MODEL for x in resources):
            return True
    return False


def _check_iam(r: dict[str, Any], not_checked: list[str]) -> list[dict[str, Any]]:
    address, values = r["address"], r.get("values") or {}
    if r["type"] == "aws_iam_role":
        docs = [
            (f"inline_policy[{i}].policy", p.get("policy"))
            for i, p in enumerate(values.get("inline_policy") or [])
            if isinstance(p, dict)
        ]
    else:
        docs = [("policy", values.get("policy"))]
    out = []
    for attribute, raw in docs:
        if not isinstance(raw, str) or not raw:
            if r["type"] != "aws_iam_role":
                not_checked.append(f"{address}: the policy is not known until apply")
            continue
        try:
            policy = json.loads(raw)
        except ValueError:
            not_checked.append(f"{address}: the policy is not valid JSON")
            continue
        if isinstance(policy, dict) and _allows_any_model(policy):
            out.append(
                _finding(
                    "S4",
                    address,
                    "An Allow statement grants bedrock:InvokeModel on every model, so a "
                    "prompt-injected or misconfigured agent can call the most expensive one.",
                    attribute=attribute,
                )
            )
    return out


def _watches_agentcore(values: dict[str, Any]) -> bool:
    namespaces = [values.get("namespace")] + [
        _block(q, "metric").get("namespace") for q in values.get("metric_query") or []
    ]
    return any(isinstance(n, str) and "agentcore" in n.lower() for n in namespaces)


def _skip_list(skips: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    out = []
    for s in skips or []:
        if not isinstance(s, dict) or s.get("rule") not in RULES:
            raise PlanError(f"each skip needs a rule, one of {', '.join(RULES)}")
        if not str(s.get("reason") or "").strip():
            raise PlanError(f"the skip for {s['rule']} needs a reason")
        out.append(
            {
                "rule": s["rule"],
                "resource": str(s.get("resource") or "*"),
                "reason": str(s["reason"]).strip(),
            }
        )
    return out


def review_agentcore(
    plan_path: str,
    bill_path: str | None = None,
    tag: str | None = None,
    skips: list[dict[str, Any]] | None = None,
    *,
    models: list[dict[str, Any]] | None = None,
    archive: list[tuple[str, dict[str, list[Any]]]] | None = None,
) -> dict[str, Any]:
    """`models` and `archive` default to the bundled LLM catalogue and price archive."""
    plan = read_plan(plan_path)
    skip_list = _skip_list(skips)
    gb_hour = float(_hosting_prices()["agentcore_runtime_v1"]["gb_hour"])

    findings: list[dict[str, Any]] = []
    not_checked: list[str] = []
    counts: Counter[str] = Counter()
    alarmed = False
    for r in _resources(plan):
        kind, values = str(r.get("type", "")), r.get("values") or {}
        r.setdefault("address", kind)
        if kind in (_RUNTIME, _HARNESS):
            findings += _check_compute(r, gb_hour)
        elif kind == _GATEWAY:
            findings += _check_gateway(r)
        elif kind in _IAM:
            findings += _check_iam(r, not_checked)
        elif kind == "aws_cloudwatch_log_group" and not values.get("retention_in_days"):
            findings.append(
                _finding(
                    "C4",
                    r["address"],
                    "retention_in_days is not set, so every log line is stored, and billed, "
                    "forever.",
                    attribute="retention_in_days",
                )
            )
        elif kind == "aws_cloudwatch_metric_alarm":
            alarmed = alarmed or _watches_agentcore(values)
        if kind.startswith("aws_bedrockagentcore_"):
            counts[kind.removeprefix("aws_bedrockagentcore_")] += 1
            if ("tags" in values or "tags_all" in values) and not (
                values.get("tags_all") or values.get("tags")
            ):
                findings.append(
                    _finding("O2", r["address"], "The resource has no tags.", attribute="tags")
                )
    if not counts:
        raise PlanError("no AgentCore resources (aws_bedrockagentcore_*) in this plan")
    if not alarmed and counts["agent_runtime"] + counts["harness"] + counts["gateway"]:
        findings.append(
            _finding(
                "O1",
                "(plan)",
                "No aws_cloudwatch_metric_alarm in this plan uses an AgentCore namespace, "
                "so failing or throttled agent calls are found by users, not by you.",
            )
        )

    warnings: list[str] = []
    spend = None
    if bill_path:
        try:
            spend = _spend(bill_path, tag, warnings, models, archive)
        except BillError as e:
            warnings.append(f"spend not analysed: {e}")
        else:
            findings += spend.pop("findings")

    kept, skipped = [], []
    used: set[int] = set()
    for f in findings:
        for i, s in enumerate(skip_list):
            if s["rule"] == f["rule"] and s["resource"] in ("*", f["resource"]):
                skipped.append({**f, "skip_reason": s["reason"]})
                used.add(i)
                break
        else:
            kept.append(f)
    warnings += [
        f"skip {s['rule']} on {s['resource']} matched no finding; remove it if it is stale"
        for i, s in enumerate(skip_list)
        if i not in used
    ]
    kept.sort(key=lambda f: (SEVERITIES.index(f["severity"]), f["rule"], f["resource"]))

    result = {
        "kind": "agentcore_review",
        "plan": Path(plan_path).name,
        "resources": dict(sorted(counts.items())),
        "summary": {
            **{s: sum(f["severity"] == s for f in kept) for s in SEVERITIES},
            "skipped": len(skipped),
            "monthly_saving_from_spend_findings": round(
                sum(f["saving"]["usd"] for f in kept if f["saving"] and f["saving"].get("monthly")),
                2,
            ),
        },
        "findings": kept,
        "skipped": skipped,
        "not_checked": not_checked,
        "spend": spend,
        "warnings": warnings,
        "honest_gaps": [
            "Reads Terraform plan or state JSON. CDK/CloudFormation templates and "
            "agentcore.json/harness.json are not read yet.",
            "Rules see configuration, not behaviour. Values only known after apply, and "
            "anything changed in the console, are not checked (see not_checked).",
            "A Harness without an environment block runs on AWS defaults Terraform does not "
            "show, so its network mode is not checked.",
            "C1's saving is per 1,000 idle sessions per GB: the plan does not say how many "
            "sessions you run. Harnesses have no platform_version and are priced as v1.",
            "O1 passes for any metric alarm on an AgentCore namespace; it does not judge "
            "whether that alarm is the right one.",
            "Spend: AgentCore usage lines are split into runtime, gateway and memory by "
            "keywords in their usage type; anything unmatched is agentcore_other.",
        ],
    }
    result["headline"] = _headline(result)
    return result


def _period(rows: list[dict[str, str]]) -> tuple[str | None, str | None, int | None]:
    """First day, last day and length in days. FOCUS ChargePeriodEnd is exclusive."""
    starts = sorted(d for r in rows if (d := (r.get("ChargePeriodStart") or "")[:10]))
    ends = sorted(d for r in rows if (d := (r.get("ChargePeriodEnd") or "")[:10]))
    if not starts:
        return None, None, None
    try:
        first = date.fromisoformat(starts[0])
        if ends:
            return starts[0], ends[-1], max((date.fromisoformat(ends[-1]) - first).days, 1)
        return starts[0], starts[-1], (date.fromisoformat(starts[-1]) - first).days + 1
    except ValueError:
        return starts[0], starts[-1], None


def _spend(
    bill_path: str,
    tag: str | None,
    warnings: list[str],
    models: list[dict[str, Any]] | None,
    archive: list[tuple[str, dict[str, list[Any]]]] | None,
) -> dict[str, Any]:
    rows, bill_warnings = _load(bill_path)
    warnings += bill_warnings
    if tag:
        key, _, want = (p.strip() for p in tag.partition("="))
        if not key or not want:
            raise BillError("tag must look like key=value, e.g. agent=genie")
        if "Tags" not in rows[0]:
            raise BillError("the bill has no Tags column, so it cannot be narrowed to one agent")
        rows = [r for r in rows if _tag(_tags(r.get("Tags"))[0], key) == want]
        if not rows:
            raise BillError(f"no rows carry the tag {key}={want}")
    else:
        warnings.append(
            "no tag given: spend covers every AI and AgentCore line in the bill, not one agent"
        )

    parts: dict[str, float] = defaultdict(float)
    used: list[dict[str, str]] = []
    for r in rows:
        cost = row_cost(r)
        if cost is None or (r.get("ChargeCategory") or "Usage") != "Usage":
            continue
        service = r.get("ServiceName") or ""
        if "agentcore" in service.lower():
            text = " ".join(r.get(c) or "" for c in _TEXT_COLUMNS)
            part = next((n for n, p in _AGENTCORE_PARTS if p.search(text)), "agentcore_other")
        elif r.get("ServiceCategory") == "AI and Machine Learning" or _AI_SERVICE.search(service):
            used.append(r)  # priced below by analyze_ai_spend
            continue
        elif not tag:
            continue
        elif "cloudwatch" in service.lower():
            part = "logs_and_monitoring"
        else:
            part = "other"
        parts[part] += cost
        used.append(r)

    try:
        ai = ai_spend_from_rows(
            rows, [], Path(bill_path).name, top_n=50, models=models, archive=archive
        )
    except BillError:
        ai = None
    if ai:
        parts["model_tokens"] += ai["totals"]["token_cost"]
        parts["other_ai"] += sum(
            o["cost"] for o in ai["other_ai_spend"] if "agentcore" not in o["service"].lower()
        )
        warnings += ai["warnings"]
    if not used:
        raise BillError("no AI or AgentCore rows in the bill")

    first, last, days = _period(used)
    factor = _DAYS_PER_MONTH / days if days else None
    total = sum(parts.values())
    findings = []
    for m in (ai or {}).get("models", []):
        label = f"{m['model']} ({m['cloud']})"
        per_10, share = m.get("cache_saving_per_10_points"), m.get("cache_read_share_pct") or 0
        if per_10 and factor and share < 50 and per_10 * factor >= 1:
            usd = per_10 * factor
            findings.append(
                _finding(
                    "SP1",
                    label,
                    f"{share}% of input tokens are cache reads. Each 10 points more saves "
                    f"about {usd:.2f} a month.",
                    value=share,
                    severity="medium" if usd >= 10 else "low",
                    saving={
                        "usd": round(usd, 2),
                        "basis": "per month, per 10 points of cache share",
                        "monthly": True,
                    },
                )
            )
        pct, list_cost = m.get("paid_vs_list_pct"), m.get("list_cost_on_your_provider")
        if pct is not None and pct > 5 and list_cost is not None and factor:
            usd = (m["cost"] - list_cost) * factor
            findings.append(
                _finding(
                    "SP2",
                    label,
                    f"Paid {pct}% over list price: about {usd:.2f} a month above standard "
                    "on-demand.",
                    value=pct,
                    saving={"usd": round(usd, 2), "basis": "per month", "monthly": True},
                )
            )
    return {
        "bill": Path(bill_path).name,
        "tag": tag,
        "period": {"from": first, "to": last, "days": days},
        "total": round(total, 2),
        "monthly_total": round(total * factor, 2) if factor else None,
        "components": {
            k: round(v, 2) for k, v in sorted(parts.items(), key=lambda kv: -kv[1]) if v
        },
        "ai_headline": ai["headline"] if ai else None,
        "findings": findings,
    }


def _headline(r: dict[str, Any]) -> str:
    s, found = r["summary"], r["findings"]
    counts = ", ".join(f"{s[k]} {k}" for k in SEVERITIES if s[k])
    names = (("agent_runtime", "runtime", "runtimes"), ("harness", "harness", "harnesses"),
             ("gateway", "gateway", "gateways"))  # fmt: skip
    parts = [
        f"{n} {one if n == 1 else many}"
        for key, one, many in names
        if (n := r["resources"].get(key, 0))
    ]
    where = ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else "".join(parts)
    line = f"{len(found)} findings ({counts or 'none'}) across {where or 'the plan'}."
    if found:
        top = found[0]
        line += f" Top: {top['rule']} {top['resource']}: {top['title'].lower()}."
    spend = r["spend"]
    if spend and spend["total"]:
        tokens = spend["components"].get("model_tokens", 0) / spend["total"]
        cost = spend["monthly_total"] if spend["monthly_total"] is not None else spend["total"]
        per = " a month" if spend["monthly_total"] is not None else ""
        line += f" The agent costs about {cost:,.2f}{per}; {tokens:.0%} is model tokens."
    if s["monthly_saving_from_spend_findings"]:
        line += (
            f" Spend fixes are worth about {s['monthly_saving_from_spend_findings']:,.2f} a month."
        )
    if s["skipped"]:
        line += f" {s['skipped']} skipped with a reason."
    return line
