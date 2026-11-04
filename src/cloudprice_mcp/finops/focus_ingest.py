"""analyze_bill — read a FOCUS billing export and say where the money goes.

Every other tool here works from list prices. This one reads what you were
actually charged: a FOCUS 1.x CSV export, which AWS (Data Exports), Azure
(Cost Management exports), GCP (BigQuery FOCUS view) and OCI all produce.

Privacy and safety, by design:
  - The file is read from local disk and never sent anywhere; no network calls.
  - This tool is local-only: dispatch refuses it unless the caller opts in
    (the stdio server does; the hosted HTTP and gateway front ends do not), so
    a hosted deployment can never be asked to read files on its own disk.
  - Only .csv / .csv.gz files under a size cap are opened, and a file whose
    header is not FOCUS is rejected without echoing any of its contents.
"""

from __future__ import annotations

import csv
import gzip
import io
from collections import defaultdict
from pathlib import Path
from typing import Any

MAX_BYTES = 500 * 1024 * 1024  # 500 MB on disk
_COST_COLUMNS = ("BilledCost", "EffectiveCost", "ListCost")
_GROUPS = ("ServiceName", "ServiceCategory", "ProviderName", "RegionId", "SkuId", "ResourceId")


class BillError(ValueError):
    """A file that cannot be analysed; the message never contains file content."""


def read_focus(file_path: str) -> tuple[list[dict[str, str]], list[str]]:
    """Rows of a FOCUS CSV, and warnings about columns that are missing."""
    path = Path(file_path).expanduser()
    name = path.name.lower()
    if not (name.endswith(".csv") or name.endswith(".csv.gz")):
        raise BillError("only .csv or .csv.gz FOCUS exports are read")
    if not path.is_file():
        raise BillError(f"no such file: {path}")
    if path.stat().st_size > MAX_BYTES:
        raise BillError(f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")

    raw = path.read_bytes()
    if name.endswith(".gz"):
        try:
            raw = gzip.decompress(raw)
        except OSError as e:
            raise BillError("not a valid gzip file") from e
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    header = set(reader.fieldnames or [])
    if not header & set(_COST_COLUMNS) or "ChargePeriodStart" not in header:
        raise BillError(
            "this does not look like a FOCUS export: it needs a cost column "
            "(BilledCost, EffectiveCost or ListCost) and ChargePeriodStart"
        )
    warnings = [
        f"column {c} is missing; results that need it are skipped"
        for c in ("BilledCost", "EffectiveCost", "ServiceName", "ProviderName")
        if c not in header
    ]
    return list(reader), warnings


def _num(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def analyze_bill(file_path: str, group_by: str = "ServiceName", top_n: int = 10) -> dict[str, Any]:
    if group_by not in _GROUPS:
        raise BillError(f"group_by must be one of {', '.join(_GROUPS)}")
    if top_n < 1:
        raise BillError("top_n must be at least 1")
    rows, warnings = read_focus(file_path)
    if not rows:
        raise BillError("the export has a header but no rows")

    totals = dict.fromkeys(_COST_COLUMNS, 0.0)
    seen = dict.fromkeys(_COST_COLUMNS, False)
    groups: dict[str, float] = defaultdict(float)
    months: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    currencies: set[str] = set()
    covered = on_demand = 0.0
    has_commitments = False
    bad_rows = 0

    for r in rows:
        costs = {c: _num(r.get(c)) for c in _COST_COLUMNS}
        for c, v in costs.items():
            if v is not None:
                totals[c] += v
                seen[c] = True
        cost = next(
            (costs[c] for c in ("EffectiveCost", "BilledCost", "ListCost") if costs[c] is not None),
            None,
        )
        if cost is None:
            bad_rows += 1
            continue
        key = r.get(group_by) or "(blank)"
        groups[key] += cost
        months[(r.get("ChargePeriodStart") or "")[:7] or "(no date)"][key] += cost
        if r.get("BillingCurrency"):
            currencies.add(r["BillingCurrency"])
        if r.get("ChargeCategory", "Usage") == "Usage":
            if r.get("CommitmentDiscountId"):
                has_commitments = True
                covered += cost
            else:
                on_demand += cost

    if bad_rows:
        warnings.append(f"{bad_rows} rows had no readable cost and were skipped")
    if len(currencies) > 1:
        warnings.append(
            f"multiple billing currencies ({', '.join(sorted(currencies))}); totals mix them"
        )

    total = sum(groups.values())
    ranked = sorted(groups.items(), key=lambda kv: kv[1], reverse=True)
    top = [
        {group_by: k, "cost": round(v, 2), "share": round(v / total, 4) if total else 0.0}
        for k, v in ranked[:top_n]
    ]
    period_list = sorted(months)
    by_month = [{"month": m, "cost": round(sum(months[m].values()), 2)} for m in period_list]

    movers: list[dict[str, Any]] = []
    if len(period_list) >= 2:
        prev, last = months[period_list[-2]], months[period_list[-1]]
        for k in set(prev) | set(last):
            delta = last.get(k, 0.0) - prev.get(k, 0.0)
            if abs(delta) >= 0.01:
                movers.append(
                    {
                        group_by: k,
                        "previous": round(prev.get(k, 0.0), 2),
                        "latest": round(last.get(k, 0.0), 2),
                        "change": round(delta, 2),
                    }
                )
        movers.sort(key=lambda m: abs(m["change"]), reverse=True)

    discount = None
    if seen["ListCost"] and seen["EffectiveCost"] and totals["ListCost"]:
        saved = totals["ListCost"] - totals["EffectiveCost"]
        discount = {
            "list_cost": round(totals["ListCost"], 2),
            "effective_cost": round(totals["EffectiveCost"], 2),
            "saved": round(saved, 2),
            "saved_pct": round(saved / totals["ListCost"] * 100, 1),
        }

    coverage = None
    if has_commitments and covered + on_demand:
        coverage = {
            "usage_covered_by_commitments": round(covered, 2),
            "usage_on_demand": round(on_demand, 2),
            "coverage_pct": round(covered / (covered + on_demand) * 100, 1),
        }

    return {
        "kind": "bill_analysis",
        "file": Path(file_path).name,
        "rows": len(rows),
        "currency": next(iter(currencies)) if len(currencies) == 1 else None,
        "headline": _headline(total, top, group_by, by_month, movers, coverage),
        "totals": {
            c.replace("Cost", "_cost").lower(): round(totals[c], 2)
            for c in _COST_COLUMNS
            if seen[c]
        },
        "analysed_cost": round(total, 2),
        "group_by": group_by,
        "top": top,
        "by_month": by_month,
        "biggest_movers_last_month": movers[:top_n],
        "discounts": discount,
        "commitment_coverage": coverage,
        "warnings": warnings,
        "honest_gaps": [
            "Costs use EffectiveCost (after discounts and amortised commitments) when "
            "present, else BilledCost, else ListCost.",
            "Coverage counts Usage rows with a CommitmentDiscountId as covered; it is "
            "only reported when the export carries commitment columns.",
            "Month-over-month compares the last two months present; a partial month "
            "(an export taken mid-month) will look like a drop.",
            "Read locally; nothing in the file is sent anywhere.",
        ],
    }


def _headline(total, top, group_by, by_month, movers, coverage) -> str:
    if not top:
        return "No costed rows."
    lead = top[0]
    line = (
        f"{total:,.2f} analysed across {len(by_month)} month(s). Largest {group_by}: "
        f"{lead[group_by]} at {lead['cost']:,.2f} ({lead['share']:.0%})."
    )
    if movers:
        m = movers[0]
        line += f" Biggest move last month: {m[group_by]} {m['change']:+,.2f}."
    if coverage:
        line += f" Commitment coverage of usage: {coverage['coverage_pct']}%."
    return line
