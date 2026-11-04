"""reprice_bill: the bill's compute priced on four clouds, against a fixed catalogue.

Every figure is worked out by hand from the catalogue below, so a weekly price
refresh cannot move them.
"""

from __future__ import annotations

import json

import pytest

from cloudprice_mcp import dispatch
from cloudprice_mcp.finops.bill_reprice import _unit_kind, reprice_bill
from cloudprice_mcp.finops.focus_ingest import BillError
from cloudprice_mcp.pricing import Instance, PriceCatalog
from tests.test_bill_insights import COLUMNS, row, write

SHAPES = {
    "aws": [
        ("t3.micro", 2, 1, 0.01),
        ("m5.large", 2, 8, 0.10),
        ("m5.xlarge", 4, 16, 0.20),
        ("c5.large", 2, 4, 0.09),
        ("r5.large", 2, 16, 0.13),
        ("m6g.large", 2, 8, 0.08),  # ARM
    ],
    "azure": [
        ("B1s", 1, 1, 0.01),
        ("D2s_v5", 2, 8, 0.10),
        ("E2s_v5", 2, 16, 0.13),
        ("F2s_v2", 2, 4, 0.085),
    ],
    "gcp": [
        ("e2-micro", 2, 1, 0.008),
        ("e2-standard-2", 2, 8, 0.07),
        ("n2-highmem-2", 2, 16, 0.12),
    ],
    "oci": [
        ("VM.Standard.E5.Flex.1OCPU", 2, 8, 0.05),
        ("VM.Standard.A2.Flex.1OCPU", 1, 6, 0.02),  # ARM
        ("VM.Standard.A1.Flex.AlwaysFree", 4, 24, 0.0),
    ],
}
CATALOG = PriceCatalog(
    as_of="2026-11-01",
    currency="USD",
    instances=(
        *(
            Instance(cloud, sku, vcpus, mem, price, "r1")  # type: ignore[arg-type]
            for cloud, shapes in SHAPES.items()
            for sku, vcpus, mem, price in shapes
        ),
        Instance("aws", "p3.2xlarge", 8, 61, 3.06, "r1", gpu_type="V100", gpu_count=1),
    ),
    storage=(),
)
COLS = [
    *COLUMNS, "x_UsageType", "ChargeDescription", "SkuMeter", "SkuPriceDetails",
    "ConsumedQuantity", "ConsumedUnit", "PricingQuantity", "PricingUnit",
]  # fmt: skip


def vm(usage_type, hours, cost, **extra):
    return row(
        "2026-10-01",
        "Amazon Elastic Compute Cloud",
        cost,
        x_UsageType=usage_type,
        ConsumedQuantity=hours,
        ConsumedUnit="Hrs",
        **extra,
    )


def bill(tmp_path, rows):
    return str(write(tmp_path / "bill.csv", rows, columns=COLS))


def by_workload(r):
    return {line["workload"]: line for line in r["lines"]}


def test_aws_instance_priced_on_all_four_clouds(tmp_path):
    # m5.large for 730 h = 1,460 vCPU-h and 5,840 GiB-h, paid 58.40 under a savings plan.
    r = reprice_bill(CATALOG, bill(tmp_path, [vm("USE1-BoxUsage:m5.large", 730, 58.40)]))
    line = r["lines"][0]
    assert (line["workload"], line["shape"], line["shape_source"]) == (
        "m5.large",
        "2 vCPU, 8 GiB",
        "catalogue",
    )
    assert {t: (p["cost"], p["as"]) for t, p in line["list_price"].items()} == {
        "aws": (73.0, "m5.large"),  # 730 h x 0.10; m5.xlarge ties at 365 h x 0.20
        "azure": (73.0, "D2s_v5"),
        "gcp": (51.1, "e2-standard-2"),
        "oci": (36.5, "VM.Standard.E5.Flex.1OCPU"),  # ARM A2 is not offered to x86
    }
    assert line["cheapest"] == "oci"
    assert r["totals"]["current_clouds_at_list"] == 73.0
    assert r["coverage"]["repriced_pct_of_compute"] == 100.0
    assert r["headline"] == (
        "Repriced 58.40 of compute (100.0% of compute, 2026-10-01 to 2026-10-01). Same "
        "capacity at list price: OCI 36.50, GCP 51.10, AWS 73.00, Azure 73.00. OCI is "
        "cheapest, 50% below AWS list. You already pay 20% under AWS list, so the real "
        "saving depends on similar discounts there."
    )


def test_shape_from_sku_price_details_name_and_meter(tmp_path):
    details = json.dumps({"InstanceType": "c7i.large", "CoreCount": 2, "MemorySize": 4})
    rows = [
        vm("USE1-BoxUsage:m6i.xlarge", 100, 19.2),  # not in the catalogue
        vm("", 100, 8.9, SkuPriceDetails=details),
        row(
            "2026-10-01",
            "Virtual Machines",
            9.6,
            ProviderName="Microsoft",
            SkuMeter="D2s v5",
            ConsumedQuantity=100,
            ConsumedUnit="1 Hour",
        ),
    ]
    lines = by_workload(reprice_bill(CATALOG, bill(tmp_path, rows)))
    m6i, c7i, d2s = lines["m6i.xlarge"], lines["c7i.large"], lines["D2s_v5"]
    assert (m6i["shape"], m6i["shape_source"]) == ("4 vCPU, 16 GiB", "instance_name")
    # m5.large and m5.xlarge both cost 20.00; the one matching the shape is named.
    assert m6i["list_price"]["aws"] == {"cost": 20.0, "instance_hours": 100.0, "as": "m5.xlarge"}
    assert (c7i["shape"], c7i["shape_source"]) == ("2 vCPU, 4 GiB", "sku_price_details")
    assert c7i["list_price"]["aws"]["as"] == "c5.large"  # 100 h x 0.09
    assert (d2s["cloud"], d2s["shape_source"], d2s["vcpu_hours"]) == ("azure", "catalogue", 200.0)


def test_gcp_and_oci_vcpu_and_memory_rows_are_paired(tmp_path):
    gcp = {"ProviderName": "Google Cloud", "ResourceId": "vm-1"}
    oci = {"ProviderName": "Oracle", "ResourceId": "ocid1.instance.x"}
    rows = [
        # GCP n2-standard-4 for 730 h: 2,920 vCPU-h in seconds, 11,680 GiB-h in the pricing unit.
        row("2026-10-01", "Compute Engine", 92.0, **gcp,
            ChargeDescription="N2 Instance Core running in Americas",
            ConsumedQuantity=2920 * 3600, ConsumedUnit="seconds"),
        row("2026-10-01", "Compute Engine", 24.0, **gcp,
            ChargeDescription="N2 Instance Ram running in Americas",
            PricingQuantity=11680, PricingUnit="gibibyte hour"),
        # OCI: 1 OCPU (2 x86 vCPUs) and 16 GB for 730 h.
        row("2026-10-01", "Compute", 30.0, **oci,
            ChargeDescription="Compute - Standard - E4 - OCPU",
            ConsumedQuantity=730, ConsumedUnit="OCPU Hours"),
        row("2026-10-01", "Compute", 20.0, **oci,
            ChargeDescription="Compute - Standard - E4 - Memory",
            ConsumedQuantity=11680, ConsumedUnit="GB Hours"),
    ]  # fmt: skip
    r = reprice_bill(CATALOG, bill(tmp_path, rows))
    n2, e4 = (
        by_workload(r)["N2 Instance running in Americas"],
        by_workload(r)["Compute - Standard - E4"],
    )
    assert (n2["vcpu_hours"], n2["gib_hours"], n2["actual_cost"]) == (2920.0, 11680.0, 116.0)
    assert n2["shape_source"] == "vcpu_and_memory_rows"
    assert {t: p["cost"] for t, p in n2["list_price"].items()} == {
        "aws": 146.0,  # 1,460 h of m5.large
        "azure": 146.0,
        "gcp": 102.2,
        "oci": 73.0,
    }
    # 8 GiB per vCPU: memory-optimised shapes win, E5 Flex is sized by memory.
    assert (e4["vcpu_hours"], e4["gib_hours"]) == (1460.0, 11680.0)
    assert {t: (p["cost"], p["as"]) for t, p in e4["list_price"].items()} == {
        "aws": (94.9, "r5.large"),
        "azure": (94.9, "E2s_v5"),
        "gcp": (87.6, "n2-highmem-2"),
        "oci": (73.0, "VM.Standard.E5.Flex.1OCPU"),
    }
    assert r["source_clouds"] == ["gcp", "oci"]
    assert r["headline"].endswith("Cheapest: OCI.")


def test_burstable_and_arm_workloads_stay_like_for_like(tmp_path):
    rows = [vm("USE1-BoxUsage:t3.micro", 730, 7.3), vm("USE1-BoxUsage:m6g.large", 730, 58.4)]
    lines = by_workload(reprice_bill(CATALOG, bill(tmp_path, rows)))
    t3 = {t: (p["cost"], p["as"]) for t, p in lines["t3.micro"]["list_price"].items()}
    assert t3 == {
        "aws": (7.3, "t3.micro"),
        "azure": (14.6, "B1s"),  # 1 vCPU each: 1,460 h
        "gcp": (5.84, "e2-micro"),
        "oci": (36.5, "VM.Standard.E5.Flex.1OCPU"),  # no burstable shape; never AlwaysFree
    }
    arm = lines["m6g.large"]["list_price"]
    assert (arm["aws"]["as"], arm["oci"]["as"], arm["oci"]["cost"]) == (
        "m6g.large",
        "VM.Standard.A2.Flex.1OCPU",
        29.2,  # 1,460 h of the 1 vCPU A2 at 0.02
    )


def test_what_cannot_be_repriced_is_reported_with_its_cost(tmp_path):
    windows = "$0.192 per On Demand Windows m5.large Instance Hour"
    rows = [
        vm("USE1-BoxUsage:m5.large", 730, 73.0),
        vm("USE1-BoxUsage:m5.large", 730, 140.0, ChargeDescription=windows),
        vm("USE1-BoxUsage:p3.2xlarge", 10, 30.6),
        vm("USE1-BoxUsage:mac2.metal", 24, 15.6),
        vm("USE1-EBSOptimized:m4.large", 730, 7.3),
        vm("USE1-BoxUsage:m5.large", 0, 20.0, CommitmentDiscountId="ri-1",
           CommitmentDiscountStatus="Unused"),
        row("2026-10-01", "AWS Lambda", 5.0, ResourceId="fn",
            x_UsageType="USE1-Lambda-GB-Second", ConsumedQuantity=3e5,
            ConsumedUnit="Lambda-GB-Second"),
        row("2026-10-01", "Other", 4.0, ProviderName="Contoso Cloud"),
        row("2026-10-01", "Amazon S3", 50.0, category="Storage"),
    ]  # fmt: skip
    r = reprice_bill(CATALOG, bill(tmp_path, rows))
    skipped = {
        s["reason"].split(":")[0].split(" (")[0]: (s["cost"], s["rows"]) for s in r["not_repriced"]
    }
    assert skipped == {
        "licensed OS": (140.0, 1),
        "GPU and accelerator instances": (30.6, 1),
        "unused commitment": (20.0, 1),
        "instance type not recognised": (15.6, 1),
        "no instance-hours or vCPU/memory usage on the row": (7.3, 1),
        "memory-only charges, such as serverless functions": (5.0, 1),
        "provider is not AWS, Azure, GCP or OCI": (4.0, 1),
    }
    assert r["coverage"] == {
        "usage_cost": 345.5,
        "compute_cost": 295.5,
        "repriced_cost": 73.0,
        "repriced_pct_of_compute": 24.7,
        "repriced_pct_of_usage": 21.1,
    }


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("hrs", ("hours", 1.0)),
        ("seconds", ("hours", 1 / 3600)),
        ("byte-seconds", ("gib_hours", 1 / 3600 / 2**30)),
        ("gibibyte hour", ("gib_hours", 1.0)),
        ("gb-hours", ("gib_hours", 1.0)),
        ("gb-mo", None),
        ("requests", None),
    ],
)
def test_unit_kind(unit, expected):
    assert _unit_kind(unit) == expected


def test_input_checks(tmp_path):
    with pytest.raises(BillError, match="Compute"):
        reprice_bill(CATALOG, bill(tmp_path, [row("2026-10-01", "S3", 5, category="Storage")]))
    with pytest.raises(BillError, match="top_n"):
        reprice_bill(CATALOG, bill(tmp_path, [vm("BoxUsage:m5.large", 1, 1)]), top_n=0)


def test_reprice_bill_is_local_only(tmp_path):
    args = {"file_path": bill(tmp_path, [vm("USE1-BoxUsage:m5.large", 730, 70.08)])}
    assert "reprice_bill" not in dispatch.tool_names()
    assert dispatch.call("reprice_bill", args) == {"error": "Unknown tool: reprice_bill"}
    assert dispatch.call("reprice_bill", args, allow_local=True)["kind"] == "bill_repricing"
    assert "error" in dispatch.call("reprice_bill", {}, allow_local=True)
