"""Multi-cloud price history.

Loads every dated snapshot under `data/prices/YYYY-MM-DD.json` and exposes
it as a per-SKU timeseries. Each weekly refresh adds another data point
without ever overwriting old ones — after 12 weeks of refreshes this becomes
a public dataset answering "what did m5.xlarge cost in May?" — a question
no cloud calculator can answer because their pages always show today.

Why this matters:
    - AWS / Azure / GCP / OCI calculators show today's prices only.
    - Commercial FinOps tools (Vantage, Cloudability) keep history but
      charge $30k+/yr for access.
    - Nobody publishes raw multi-cloud price history as open data.
    - We do, as a side effect of how the auto-refresh persists snapshots.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from importlib import resources

from .pricing import Cloud

DATA_PACKAGE = "cloudprice_mcp.data.prices"


@dataclass(frozen=True)
class PricePoint:
    """One observation: SKU price at a specific catalog `as_of` date."""
    as_of: str        # ISO 8601 date string, e.g. "2026-05-12"
    cloud: Cloud
    sku: str
    region: str
    hourly_usd: float

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "cloud": self.cloud,
            "sku": self.sku,
            "region": self.region,
            "hourly_usd": self.hourly_usd,
        }


@dataclass(frozen=True)
class HistoryWindow:
    """A SKU's price timeseries with summary stats — what you'd want to
    show in a release-notes bullet or a LinkedIn post."""
    cloud: Cloud
    sku: str
    region: str
    points: tuple[PricePoint, ...]   # sorted oldest-to-newest

    @property
    def earliest(self) -> PricePoint:
        return self.points[0]

    @property
    def latest(self) -> PricePoint:
        return self.points[-1]

    @property
    def total_change_pct(self) -> float:
        """Percent change from earliest observed price to latest. 0 if only
        one point exists (no baseline). Positive = price went up."""
        if len(self.points) < 2:
            return 0.0
        old = self.earliest.hourly_usd
        new = self.latest.hourly_usd
        if old == 0:
            return 0.0
        return round((new - old) / old * 100, 2)

    @property
    def total_change_usd(self) -> float:
        if len(self.points) < 2:
            return 0.0
        return round(self.latest.hourly_usd - self.earliest.hourly_usd, 6)

    def to_dict(self) -> dict:
        return {
            "cloud": self.cloud,
            "sku": self.sku,
            "region": self.region,
            "data_points": len(self.points),
            "earliest_as_of": self.earliest.as_of,
            "latest_as_of": self.latest.as_of,
            "earliest_hourly_usd": self.earliest.hourly_usd,
            "latest_hourly_usd": self.latest.hourly_usd,
            "total_change_pct": self.total_change_pct,
            "total_change_usd": self.total_change_usd,
            "series": [p.to_dict() for p in self.points],
        }


def list_snapshot_dates() -> list[str]:
    """Return every snapshot date present in the package, oldest-first.

    Each file is `YYYY-MM-DD.json` under the bundled prices/ resource directory.
    """
    dates: list[str] = []
    for entry in resources.files(DATA_PACKAGE).iterdir():
        name = entry.name
        if not name.endswith(".json"):
            continue
        stem = name.removesuffix(".json")
        if _is_iso_date(stem):
            dates.append(stem)
    return sorted(dates)


def load_snapshot(as_of: str) -> dict:
    """Read one snapshot file by its as_of date. Raises FileNotFoundError if
    the date doesn't exist in the bundled history."""
    return json.loads(
        resources.files(DATA_PACKAGE).joinpath(f"{as_of}.json").read_text(encoding="utf-8")
    )


def load_history(
    *,
    cloud: Cloud | None = None,
    sku: str | None = None,
    since: str | None = None,
) -> list[PricePoint]:
    """Flat list of every observation, oldest-first. Filters are AND-composed.

    Args:
        cloud: restrict to one of "aws" | "azure" | "gcp" | "oci"
        sku: restrict to a specific SKU string (e.g., "m5.xlarge")
        since: ISO date string; include points with as_of >= since
    """
    points: list[PricePoint] = []
    for snap_date in list_snapshot_dates():
        if since is not None and snap_date < since:
            continue
        catalog = load_snapshot(snap_date)
        for c in ("aws", "azure", "gcp", "oci"):
            if cloud is not None and c != cloud:
                continue
            block = catalog.get(c)
            if not isinstance(block, dict):
                continue
            region = block.get("region", "")
            for entry in block.get("instances", []):
                entry_sku = entry.get("sku")
                if sku is not None and entry_sku != sku:
                    continue
                hourly = entry.get("hourly_usd")
                if hourly is None:
                    continue
                points.append(
                    PricePoint(
                        as_of=snap_date,
                        cloud=c,  # type: ignore[arg-type]
                        sku=entry_sku,
                        region=region,
                        hourly_usd=float(hourly),
                    )
                )
    return points


def history_window(cloud: Cloud, sku: str, *, since: str | None = None) -> HistoryWindow | None:
    """Build a HistoryWindow for one (cloud, sku) pair. Returns None if no
    snapshots match (e.g., SKU added after `since`).
    """
    points = load_history(cloud=cloud, sku=sku, since=since)
    if not points:
        return None
    first = points[0]
    return HistoryWindow(
        cloud=first.cloud,
        sku=first.sku,
        region=first.region,
        points=tuple(points),
    )


def all_changes_since(since: str) -> list[tuple[Cloud, str, float, float]]:
    """Convenience for the weekly "what changed" report.

    Returns a list of (cloud, sku, earliest_price, latest_price) tuples for every
    SKU whose price moved at all (>=$0.000001) between `since` and the latest snapshot.
    Sorted by absolute % change descending — biggest movers first.
    """
    changes: list[tuple[Cloud, str, float, float, float]] = []
    # Group all post-`since` points by (cloud, sku) and compute first vs last.
    by_key: dict[tuple[str, str], list[PricePoint]] = {}
    for p in load_history(since=since):
        by_key.setdefault((p.cloud, p.sku), []).append(p)
    for (c, sku), pts in by_key.items():
        if len(pts) < 2:
            continue
        old, new = pts[0].hourly_usd, pts[-1].hourly_usd
        delta = abs(new - old)
        if delta < 1e-6:
            continue
        pct = (new - old) / old * 100 if old > 0 else 0.0
        changes.append((c, sku, old, new, pct))  # type: ignore[arg-type]
    changes.sort(key=lambda t: abs(t[4]), reverse=True)
    return [(c, sku, old, new) for c, sku, old, new, _ in changes]


def _is_iso_date(s: str) -> bool:
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False
