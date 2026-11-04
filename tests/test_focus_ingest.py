"""analyze_bill: FOCUS parsing, the numbers, and the local-only safety rules."""

from __future__ import annotations

import csv
import gzip
import io

import pytest

from cloudprice_mcp import dispatch
from cloudprice_mcp.finops.focus_export import export_focus
from cloudprice_mcp.finops.focus_ingest import BillError, analyze_bill
from cloudprice_mcp.inventory import parse_dict
from cloudprice_mcp.pricing import load_catalog

HEADER = [
    "BillingCurrency", "ChargePeriodStart", "ChargeCategory", "ProviderName",
    "ServiceName", "ServiceCategory", "RegionId", "ListCost", "BilledCost",
    "EffectiveCost", "CommitmentDiscountId",
]  # fmt: skip
ROWS = [
    # month, service, list, billed, effective, commitment
    ("2026-09-01", "Amazon EC2", 1000, 800, 700, "sp-1"),
    ("2026-09-01", "Amazon EC2", 300, 300, 300, ""),
    ("2026-09-01", "Amazon S3", 200, 200, 200, ""),
    ("2026-10-01", "Amazon EC2", 1000, 800, 700, "sp-1"),
    ("2026-10-01", "Amazon EC2", 600, 600, 600, ""),
    ("2026-10-01", "Amazon S3", 100, 100, 100, ""),
]


def write_bill(path, rows=ROWS, header=HEADER):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for month, svc, lst, billed, eff, cid in rows:
            cat = "Storage" if "S3" in svc else "Compute"
            w.writerow(["USD", month, "Usage", "AWS", svc, cat, "us-east-1", lst, billed, eff, cid])
    return path


@pytest.fixture
def bill(tmp_path):
    return write_bill(tmp_path / "focus.csv")


def test_totals_top_and_months(bill):
    r = analyze_bill(str(bill))
    assert r["totals"] == {"billed_cost": 2800.0, "effective_cost": 2600.0, "list_cost": 3200.0}
    assert r["analysed_cost"] == 2600.0  # EffectiveCost preferred
    assert r["top"][0] == {"ServiceName": "Amazon EC2", "cost": 2300.0, "share": 0.8846}
    assert r["by_month"] == [
        {"month": "2026-09", "cost": 1200.0},
        {"month": "2026-10", "cost": 1400.0},
    ]


def test_movers_discounts_and_coverage(bill):
    r = analyze_bill(str(bill))
    assert r["biggest_movers_last_month"][0] == {
        "ServiceName": "Amazon EC2",
        "previous": 1000.0,
        "latest": 1300.0,
        "change": 300.0,
    }
    assert r["discounts"]["saved"] == 600.0  # 3200 list - 2600 effective
    cov = r["commitment_coverage"]
    assert cov["usage_covered_by_commitments"] == 1400.0
    assert cov["coverage_pct"] == pytest.approx(53.8, abs=0.1)
    assert "53.8%" in r["headline"]


def test_group_by_category_and_gzip(tmp_path, bill):
    gz = tmp_path / "focus.csv.gz"
    gz.write_bytes(gzip.compress(bill.read_bytes()))
    r = analyze_bill(str(gz), group_by="ServiceCategory", top_n=1)
    assert r["top"] == [{"ServiceCategory": "Compute", "cost": 2300.0, "share": 0.8846}]


def test_non_focus_file_is_rejected_without_echoing_it(tmp_path):
    secret = tmp_path / "notes.csv"
    secret.write_text("password,hunter2\nalice,s3cret\n", encoding="utf-8")
    with pytest.raises(BillError) as e:
        analyze_bill(str(secret))
    assert "hunter2" not in str(e.value) and "s3cret" not in str(e.value)


def test_only_csv_files_are_opened(tmp_path):
    other = tmp_path / "id_rsa"
    other.write_text("PRIVATE", encoding="utf-8")
    with pytest.raises(BillError, match="only .csv"):
        analyze_bill(str(other))


def test_header_only_and_missing_columns(tmp_path):
    with pytest.raises(BillError, match="no rows"):
        analyze_bill(str(write_bill(tmp_path / "empty.csv", rows=[])))
    minimal = tmp_path / "min.csv"
    minimal.write_text("ChargePeriodStart,BilledCost\n2026-10-01,5\n", encoding="utf-8")
    r = analyze_bill(str(minimal))
    assert r["analysed_cost"] == 5.0
    assert any("ServiceName" in w for w in r["warnings"])


def test_round_trip_from_export_focus(tmp_path):
    inv = parse_dict(
        {
            "source_cloud": "aws",
            "compute": [{"name": "web", "vcpus": 4, "memory_gb": 16, "quantity": 2}],
        }
    )
    out = export_focus("compute_workload", catalog=load_catalog(), inventory=inv, format="csv")
    path = tmp_path / "export.csv"
    path.write_text(out["csv"], encoding="utf-8")
    expected = sum(float(r["ListCost"]) for r in csv.DictReader(io.StringIO(out["csv"])))
    r = analyze_bill(str(path), group_by="ProviderName")
    assert r["analysed_cost"] == pytest.approx(expected, abs=0.05)
    assert len(r["top"]) == 4  # one row per cloud


def test_local_only_gate(bill):
    # Hosted front ends call dispatch without allow_local: the tool must not exist there.
    assert "analyze_bill" not in dispatch.tool_names()
    assert "analyze_bill" in dispatch.tool_names(include_local=True)
    assert dispatch.call("analyze_bill", {"file_path": str(bill)}) == {
        "error": "Unknown tool: analyze_bill"
    }
    r = dispatch.call("analyze_bill", {"file_path": str(bill)}, allow_local=True)
    assert r["kind"] == "bill_analysis"


def test_unused_commitment_is_not_counted_as_covered_usage(tmp_path):
    header = [*HEADER, "CommitmentDiscountStatus"]
    path = tmp_path / "unused.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerow(["USD", "2026-10-01", "Usage", "AWS", "Amazon EC2", "Compute",
                    "us-east-1", 100, 60, 60, "sp-1", "Used"])  # fmt: skip
        w.writerow(["USD", "2026-10-01", "Usage", "AWS", "Amazon EC2", "Compute",
                    "us-east-1", 40, 40, 40, "sp-1", "Unused"])  # fmt: skip
        w.writerow(["USD", "2026-10-01", "Usage", "AWS", "Amazon EC2", "Compute",
                    "us-east-1", 60, 60, 60, "", ""])  # fmt: skip
    cov = analyze_bill(str(path))["commitment_coverage"]
    # Idle commitment hours are waste, not usage: 60 covered of 120 used.
    assert cov == {
        "usage_covered_by_commitments": 60.0,
        "usage_on_demand": 60.0,
        "coverage_pct": 50.0,
    }
