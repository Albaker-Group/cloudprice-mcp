"""`cloudprice-mcp history` — query the bundled price-history dataset.

Each weekly auto-refresh persists a dated snapshot. This CLI exposes that
dataset so FinOps practitioners can answer "what did m5.xlarge cost in May?"
without paying for a commercial tool.

Examples:
    cloudprice-mcp history                          # list every tracked SKU
    cloudprice-mcp history --cloud aws              # all AWS SKUs
    cloudprice-mcp history --cloud aws --sku m5.xlarge
    cloudprice-mcp history --since 2026-01-01       # window
    cloudprice-mcp history --format json            # machine-readable
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import get_args

from .history import (
    HistoryWindow,
    history_window,
    list_snapshot_dates,
    load_history,
)
from .pricing import Cloud

_VALID_CLOUDS: tuple[str, ...] = get_args(Cloud)


def add_history_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cloud",
        choices=_VALID_CLOUDS,
        help="Restrict to one cloud (aws / azure / gcp / oci).",
    )
    parser.add_argument(
        "--sku",
        help="Specific SKU, e.g. m5.xlarge or VM.Standard.E5.Flex.1OCPU.",
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Only include snapshots on or after this date.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format. text = human-readable table; json = machine-readable.",
    )


def run_history(args) -> int:
    snapshots = list_snapshot_dates()
    if not snapshots:
        print("No price snapshots bundled with this build.", file=sys.stderr)
        return 1

    if args.sku and args.cloud:
        return _emit_single_sku(args.cloud, args.sku, args.since, args.format)

    return _emit_summary(args.cloud, args.sku, args.since, args.format, snapshots)


def _emit_single_sku(cloud: str, sku: str, since: str | None, fmt: str) -> int:
    window = history_window(cloud, sku, since=since)
    if window is None:
        msg = f"No history for {cloud}/{sku}" + (f" since {since}" if since else "")
        print(msg, file=sys.stderr)
        return 1
    if fmt == "json":
        print(json.dumps(window.to_dict(), indent=2, default=str))
        return 0
    _print_window(window)
    return 0


def _emit_summary(
    cloud: str | None,
    sku: str | None,
    since: str | None,
    fmt: str,
    snapshots: list[str],
) -> int:
    points = load_history(cloud=cloud, sku=sku, since=since)
    if fmt == "json":
        print(json.dumps({
            "snapshots": snapshots,
            "filters": {"cloud": cloud, "sku": sku, "since": since},
            "data_points": len(points),
            "series": [p.to_dict() for p in points],
        }, indent=2, default=str))
        return 0

    # Group into per-(cloud, sku) windows so we can print one-line summaries.
    by_key: dict[tuple[str, str], list] = {}
    for p in points:
        by_key.setdefault((p.cloud, p.sku), []).append(p)

    print(f"Tracked snapshots: {len(snapshots)} "
          f"({snapshots[0]} -> {snapshots[-1]})")
    if cloud or sku or since:
        filters = []
        if cloud:
            filters.append(f"cloud={cloud}")
        if sku:
            filters.append(f"sku={sku}")
        if since:
            filters.append(f"since={since}")
        print(f"Filters: {', '.join(filters)}")
    print()
    print(f"{'CLOUD':<6} {'SKU':<34} {'PTS':>4} {'LATEST':>12} {'CHANGE':>10}")
    print("-" * 70)
    for (c, s), pts in sorted(by_key.items()):
        pts_sorted = sorted(pts, key=lambda p: p.as_of)
        latest = pts_sorted[-1].hourly_usd
        oldest = pts_sorted[0].hourly_usd
        change_pct = ((latest - oldest) / oldest * 100) if oldest > 0 and len(pts_sorted) > 1 else 0.0
        change_str = f"{change_pct:+.2f}%" if change_pct != 0 else "-"
        print(f"{c:<6} {s:<34} {len(pts):>4} ${latest:>11.5f} {change_str:>10}")
    return 0


def _print_window(w: HistoryWindow) -> None:
    print(f"{w.cloud}/{w.sku} ({w.region}) — {len(w.points)} data point(s)")
    print()
    print(f"{'AS_OF':<12} {'HOURLY USD':>12}")
    print("-" * 26)
    for p in w.points:
        print(f"{p.as_of:<12} ${p.hourly_usd:>11.5f}")
    if len(w.points) >= 2:
        print()
        print(f"Change: {w.total_change_pct:+.2f}% (${w.total_change_usd:+.5f}/h)")
