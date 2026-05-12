"""Weekly price-refresh orchestrator.

Runs each cloud fetcher in turn, writes a dated snapshot to
`src/cloudprice_mcp/data/prices/YYYY-MM-DD.json`, and updates the canonical
`prices.json` file. Designed for the weekly GitHub Action — also runnable
locally with `python scripts/refresh_prices.py [--dry-run] [--clouds aws,azure]`.

Why dated snapshots:
    The bundled catalog gives us "current prices." The dated snapshots give us
    something nobody else has: a public, MIT-licensed multi-cloud price-history
    timeseries. After 12 weeks of refreshes we can answer "what did m5.xlarge
    cost in May?" — neither AWS Calculator nor GCP Estimator can do that.

Partial-refresh policy:
    If one cloud's fetcher fails (e.g., GCP_API_KEY missing), the orchestrator
    logs the failure and keeps that cloud's prices unchanged. The snapshot is
    still written so the history is dense. The PR description names which
    clouds were refreshed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from importlib import import_module
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Allow `import scripts.fetchers.*` when running this file directly.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
PRICES_FILE = REPO_ROOT / "src" / "cloudprice_mcp" / "data" / "prices.json"
SNAPSHOT_DIR = REPO_ROOT / "src" / "cloudprice_mcp" / "data" / "prices"

ALL_CLOUDS = ("aws", "azure", "gcp", "oci")


class RefreshSummary:
    """Tracks what changed for the release notes / PR body."""

    def __init__(self) -> None:
        self.refreshed: list[str] = []
        self.skipped: list[tuple[str, str]] = []  # (cloud, reason)
        self.diffs: dict[str, list[tuple[str, float, float]]] = {}  # cloud -> [(sku, old, new)]

    def add_diff(self, cloud: str, sku: str, old: float, new: float) -> None:
        if abs(old - new) < 1e-9:
            return
        self.diffs.setdefault(cloud, []).append((sku, old, new))

    def as_markdown(self) -> str:
        lines = []
        if self.refreshed:
            lines.append(f"Refreshed: {', '.join(self.refreshed)}")
        if self.skipped:
            for cloud, reason in self.skipped:
                lines.append(f"Skipped {cloud}: {reason}")
        for cloud, changes in self.diffs.items():
            lines.append(f"\n### {cloud.upper()} ({len(changes)} SKUs changed)")
            for sku, old, new in changes:
                pct = ((new - old) / old * 100) if old else 0.0
                lines.append(f"- `{sku}`: ${old:.5f} -> ${new:.5f} ({pct:+.2f}%)")
        return "\n".join(lines) if lines else "No changes."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch + diff but do not write any files. Prints what would change.",
    )
    parser.add_argument(
        "--clouds", default=",".join(ALL_CLOUDS),
        help=f"Comma-separated subset of {ALL_CLOUDS} to refresh. Default: all.",
    )
    parser.add_argument(
        "--output-summary", type=Path, default=None,
        help="Optional path to write a markdown summary (used by GH Actions for the PR body).",
    )
    args = parser.parse_args(argv)

    selected = tuple(c.strip() for c in args.clouds.split(",") if c.strip())
    invalid = [c for c in selected if c not in ALL_CLOUDS]
    if invalid:
        parser.error(f"Unknown clouds: {invalid}. Valid: {ALL_CLOUDS}")

    catalog = _load_prices_file()
    summary = RefreshSummary()

    for cloud in selected:
        _refresh_cloud(cloud, catalog, summary)

    _bump_as_of(catalog)

    if args.dry_run:
        print("=== DRY RUN — no files written ===")
        print(summary.as_markdown())
        # Even in dry-run, a total fetcher wipeout is an error worth signaling.
        return 0 if summary.refreshed else 2

    if not summary.refreshed:
        # Every cloud failed — don't write a snapshot that's identical to the
        # previous one (the dated copy would be misleading). Fail loudly so the
        # weekly cron PR doesn't open with zero useful changes.
        print("ERROR: no clouds refreshed — refusing to write snapshot.")
        print(summary.as_markdown())
        return 2

    _write_snapshot(catalog)
    _write_current(catalog)
    print(summary.as_markdown())
    if args.output_summary:
        args.output_summary.write_text(summary.as_markdown(), encoding="utf-8")
    return 0


def _load_prices_file() -> dict:
    return json.loads(PRICES_FILE.read_text(encoding="utf-8"))


def _refresh_cloud(cloud: str, catalog: dict, summary: RefreshSummary) -> None:
    if cloud not in catalog:
        summary.skipped.append((cloud, "not in current catalog"))
        return

    block = catalog[cloud]
    instances = block.get("instances", [])

    try:
        fetcher = import_module(f"scripts.fetchers.{cloud}")
        refreshed_instances = fetcher.fetch_instance_prices(list(instances))
    except Exception as e:
        summary.skipped.append((cloud, f"{type(e).__name__}: {e}"))
        return

    # Compute diffs before mutating.
    by_sku = {i["sku"]: i["hourly_usd"] for i in instances}
    for new in refreshed_instances:
        old = by_sku.get(new["sku"])
        if old is not None:
            summary.add_diff(cloud, new["sku"], old, new["hourly_usd"])

    block["instances"] = refreshed_instances
    summary.refreshed.append(cloud)


def _bump_as_of(catalog: dict) -> None:
    catalog["as_of"] = dt.date.today().isoformat()


def _write_snapshot(catalog: dict) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = SNAPSHOT_DIR / f"{catalog['as_of']}.json"
    snapshot_path.write_text(
        json.dumps(catalog, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote snapshot {snapshot_path.relative_to(REPO_ROOT)}")


def _write_current(catalog: dict) -> None:
    PRICES_FILE.write_text(
        json.dumps(catalog, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"Updated {PRICES_FILE.relative_to(REPO_ROOT)} (as_of={catalog['as_of']})")


if __name__ == "__main__":
    sys.exit(main())
