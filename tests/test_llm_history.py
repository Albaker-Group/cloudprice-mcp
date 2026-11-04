"""Weekly LLM price archive: compact snapshots and week-over-week diffs."""

from __future__ import annotations

from cloudprice_mcp import dispatch, llm_history


def _litellm(as_of, models):
    return {
        "as_of": as_of,
        "models": [
            {"litellm_id": i, "provider": p, "input_per_1m_usd": inp, "output_per_1m_usd": out}
            for i, p, inp, out in models
        ],
    }


def _openrouter(models):
    return {
        "models": [
            {
                "openrouter_id": i,
                "provider_underlying": p,
                "input_per_1m_usd": inp,
                "output_per_1m_usd": out,
            }
            for i, p, inp, out in models
        ]
    }


OLD = llm_history.compact(
    _litellm(
        "2026-10-25", [("a/keep", "a", 1.0, 2.0), ("a/cut", "a", 4.0, 8.0), ("a/gone", "a", 1, 1)]
    ),
    _openrouter([("x/y", "x", 0.5, 1.0)]),
)
NEW = llm_history.compact(
    _litellm(
        "2026-11-01", [("a/keep", "a", 1.0, 2.0), ("a/cut", "a", 1.0, 2.0), ("b/new", "b", 3, 6)]
    ),
    _openrouter([("x/y", "x", 0.5, 1.0)]),
)


def test_compact_keeps_prices_keyed_by_source_id():
    assert NEW["as_of"] == "2026-11-01"
    assert NEW["sources"]["litellm"]["a/cut"] == ["a", 1.0, 2.0, None]
    assert set(NEW["sources"]) == {"litellm", "openrouter"}


def test_diff_reports_added_removed_and_price_changes():
    d = llm_history.diff(OLD, NEW)
    lit = d["sources"]["litellm"]
    assert [m["model_id"] for m in lit["added"]] == ["b/new"]
    assert [m["model_id"] for m in lit["removed"]] == ["a/gone"]
    assert len(lit["price_changes"]) == 1
    change = lit["price_changes"][0]
    assert change["model_id"] == "a/cut"
    assert change["change_pct"] == {"input": -75.0, "output": -75.0}
    assert d["sources"]["openrouter"] == {"added": [], "removed": [], "price_changes": []}


def test_snapshot_bytes_are_reproducible(tmp_path):
    first = llm_history.write_snapshot(NEW, tmp_path / "a").read_bytes()
    second = llm_history.write_snapshot(NEW, tmp_path / "b").read_bytes()
    assert first == second  # an unchanged week is no git diff


def test_bundled_archive_loads_every_week():
    dates = llm_history.list_dates()
    assert dates[0] == "2026-05-23"  # backfilled from git history
    assert len(dates) >= 8
    for d in dates:
        snap = llm_history.load(d)
        assert snap["as_of"] == d
        assert len(snap["sources"]["litellm"]) > 1000


def test_price_history_flags_the_oci_placeholder_correction():
    r = dispatch.call("get_price_history", {"cloud": "oci", "sku": "VM.Standard.E5.Flex.1OCPU"})
    assert r["data_corrections"][0]["superseded_before"] == "2026-05-04"
    assert all(p["as_of"] >= "2026-05-04" for p in r["series"])
