#!/usr/bin/env python3
"""Build data-release artifacts from a registry snapshot.

Reads a ForecastLedger data-contract v1 registry document and emits the
three public release artifacts (ledger snapshot, per-horizon scorecards,
MCP release manifest). Pure stdlib; no network access; no credentials.

Usage:
    python3 scripts/build_data_artifacts.py --registry PATH --out DEST_DIR

The artifacts land under a release directory on the data distribution
repository (see README-dist.md). All inputs are local files or CLI
arguments.
"""

from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime, timezone
from typing import Any, Dict, List

HORIZONS = ("24h", "3d", "7d", "4w")
STATUSES = ("resolved", "open")
DIRECTIONS = ("Bullish", "Bearish", "Neutral")
# Minimum graded samples before a horizon's stats are claimed as evidence.
MIN_N = {"24h": 14, "3d": 10, "7d": 6, "4w": 3}


def _fmt_px(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_rate(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def normalize_registry(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize both known registry schemas into the brief-v1 shape.

    Producer schema (real, produced by grade_engine.py v1):
      calls[] flat: horizon, call_id, band_lo_usd, band_hi_usd,
      prob_pct, direction, status, verdict, resolved_price_usd,
      resolved_snapshot_utc, expires_utc
    Brief-v1 schema (tests/fixtures): horizons[hz].calls[] with id/lo/hi/
      prob/realized_px/issued_ts/stats objects nested per horizon.
    Normalized output: horizons[hz] = {"calls": [...], "stats": {...}} with
    unified field names (id, lo, hi, prob, realized_px, issued_ts).
    """
    out: Dict[str, Any] = {}
    src = doc.get("horizons", {})
    if not isinstance(src, dict):
        raise SystemExit("registry is not a registry document (no horizons map)")
    src_stats = {k: (v if isinstance(v, dict) else {}) for k, v in src.items()}
    flat = doc.get("calls") if isinstance(doc.get("calls"), list) else []
    by_hz: Dict[str, List[Dict[str, Any]]] = {}
    for c in flat:
        hz = c.get("horizon")
        if isinstance(hz, str):
            by_hz.setdefault(hz, []).append({
                "id": c.get("call_id") or c.get("id"),
                "lo": c.get("band_lo_usd", c.get("lo")),
                "hi": c.get("band_hi_usd", c.get("hi")),
                "prob": c.get("prob_pct", c.get("prob")),
                "direction": c.get("direction"),
                "status": c.get("status"),
                "verdict": c.get("verdict"),
                "realized_px": c.get("resolved_price_usd", c.get("realized_px")),
                "issued_ts": c.get("issued_ts"),
            })
    for hz, entry in src.items():
        calls = entry.get("calls", []) if isinstance(entry, dict) else []
        norm_calls = []
        for c in calls:
            norm_calls.append({
                "id": c.get("id") or c.get("call_id"),
                "lo": c.get("lo", c.get("band_lo_usd")),
                "hi": c.get("hi", c.get("band_hi_usd")),
                "prob": c.get("prob", c.get("prob_pct")),
                "direction": c.get("direction"),
                "status": c.get("status"),
                "verdict": c.get("verdict"),
                "realized_px": c.get("realized_px", c.get("resolved_price_usd")),
                "issued_ts": c.get("issued_ts"),
            })
        norm_calls.extend(by_hz.pop(hz, []))
        st = entry.get("stats") if isinstance(entry, dict) else None
        out[hz] = {"calls": norm_calls, "stats": st or {
            # producer-schema stats live at horizons[hz] itself
            "n_graded": entry.get("n_resolved"),
            "n_resolved": entry.get("n_resolved"),
            "n_open": entry.get("n_open"),
            "hit_rate": entry.get("hit_rate"),
            "brier": entry.get("brier"),
            "baseline_brier": 0.25,
            "armed": entry.get("armed"),
            "min_n": entry.get("min_n"),
        } if isinstance(entry, dict) else {}}
    for hz, extra in by_hz.items():
        out[hz] = {"calls": extra, "stats": src_stats.get(hz, {})}
    norm: Dict[str, Any] = {"horizons": out}
    # carry top-level metadata with producer/brief aliases
    cov = doc.get("coverage") if isinstance(doc.get("coverage"), dict) else {}
    norm["generated_ts"] = doc.get("generated_ts") or doc.get("anchored_to_last_snapshot_utc", cov.get("coverage_end_utc"))
    norm["registry_start"] = doc.get("registry_start") or cov.get("production_era_start_utc") or cov.get("coverage_start_utc")
    return norm


def load_registry(path: pathlib.Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return normalize_registry(doc)


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{float(value):.4g}"
    return str(value)


def ledger_entry(horizon: str, entry: Dict[str, Any], window: int = 30) -> str:
    calls = [c for c in entry.get("calls", []) if isinstance(c, dict)]
    resolved = [c for c in calls if c.get("status") == "resolved"]
    ordered = sorted(resolved, key=lambda c: c.get("issued_ts") or 0)
    recent = ordered[-window:] if window else ordered
    tail_lines = []
    for c in recent:
        tail_lines.append(
            "| {id} | {direction} | {prob} | {verdict} | {px} |".format(
                id=c.get("id", "?"),
                direction=str(c.get("direction") or "?"),
                prob=_fmt_px(c.get("prob")),
                verdict=str(c.get("verdict") or "open"),
                px=_fmt_px(c.get("realized_px")),
            )
        )
    stats = entry.get("stats") or {}
    stats_block = ", ".join(
        f"{key}={_fmt(stats[key])}" if key != "hit_rate" else f"hit_rate={_fmt_rate(stats.get('hit_rate'))}"
        for key in ("n_graded", "hit_rate", "brier", "baseline_brier", "armed")
    )
    return "\n".join(
        [
            f"## {horizon}",
            "",
            f"Stats: {stats_block}",
            "",
            f"Recent resolved calls (up to {window}):",
            "",
            "| call | direction | prob | verdict | realized price |",
            "|------|-----------|------|---------|----------------|",
        ]
        + tail_lines
    )


def scorecard(horizon: str, entry: Dict[str, Any]) -> str:
    calls = [c for c in entry.get("calls", []) if isinstance(c, dict)]
    lines = [
        f"# {horizon} — scorecard",
        "",
        "| issued_ts | id | dir | prob | lo | hi | status | verdict | realized |",
        "|-----------|----|----|------|----|----|--------|---------|----------|",
    ]
    for c in sorted(calls, key=lambda c: c.get("issued_ts") or 0):
        lines.append(
            "| {issued} | {id} | {dir} | {prob} | {lo} | {hi} | {status} | {verdict} | {px} |".format(
                issued=c.get("issued_ts", "?"),
                id=c.get("id", "?"),
                dir=str(c.get("direction") or "?"),
                prob=_fmt_px(c.get("prob")),
                lo=_fmt_px(c.get("lo")),
                hi=_fmt_px(c.get("hi")),
                status=str(c.get("status") or "?"),
                verdict=str(c.get("verdict") or "n/a"),
                px=_fmt_px(c.get("realized_px")),
            )
        )
    ordered = sorted(
        (c for c in calls if c.get("status") == "resolved"),
        key=lambda c: c.get("issued_ts") or 0,
    )
    recent = ordered[-30:]
    lines += ["", "Recent resolved (max 30):"]
    recent_summary = ", ".join(
        "{}={}".format(c.get("id", "?"), c.get("verdict") or "n/a") for c in recent
    ) or "(none yet)"
    lines.append(recent_summary)
    stats = entry.get("stats") or {}
    lines += [
        "",
        f"Arming gate: {horizon} needs n_graded >= {MIN_N.get(horizon, 0)} "
        f"(current: {stats.get('n_graded', 0)}, "
        f"{'armed' if stats.get('armed') else 'un-armed — stats shown, not claimed as evidence'}).",
        "Scoring: Brier score against the published naive baseline "
        "(baseline_brier = 0.25).",
        "",
    ]
    return "\n".join(lines)


def mcp_manifest(registry: Dict[str, Any]) -> str:
    horizons_out = {}
    for h in HORIZONS:
        entry = registry.get("horizons", {}).get(h)
        if not isinstance(entry, dict):
            continue
        stats = entry.get("stats") or {}
        horizons_out[h] = {
            "n_graded": stats.get("n_graded"),
            "hit_rate": stats.get("hit_rate"),
            "brier": stats.get("brier"),
            "baseline_brier": stats.get("baseline_brier", 0.25),
            "armed": bool(stats.get("armed", False)),
        }
    out = {
        "generated_ts": registry.get("generated_ts"),
        "registry_start": registry.get("registry_start"),
        "horizons": horizons_out,
        "framing": "critical decision-making data points",
        "exchange_derived_note": "perp funding and OI are internal analysis inputs, not part of this dataset",
    }
    return json.dumps(out, indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, help="path to registry.json (data contract v1)")
    parser.add_argument("--out", required=True, help="output directory for artifacts")
    parser.add_argument("--snapshot-date", default=None, help="YYYY-MM-DD label embedded in filenames")
    args = parser.parse_args()

    if args.snapshot_date:
        try:
            datetime.strptime(args.snapshot_date, "%Y-%m-%d")
        except ValueError:
            parser.error("--snapshot-date must be YYYY-MM-DD")
        stem = args.snapshot_date
    else:
        stem = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    registry = load_registry(pathlib.Path(args.registry))
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    parts = [
        "# ForecastLedger — calibration ledger snapshot",
        "",
        f"Snapshot: {stem}",
        f"generated_ts: {registry.get('generated_ts')}",
        f"registry_start: {registry.get('registry_start')}",
        "",
        "This dataset provides critical decision-making data points; it is not advice.",
        "Exchange-internal signals (perp funding, open interest) are internal analysis"
        " inputs and are not part of this dataset.",
        "",
    ]
    for h in HORIZONS:
        entry = registry.get("horizons", {}).get(h)
        if isinstance(entry, dict):
            parts.append(ledger_entry(h, entry))
            parts.append("")
    (out_dir / f"{stem}.md").write_text("\n".join(parts), encoding="utf-8")

    score_dir = out_dir / "scorecards"
    score_dir.mkdir(exist_ok=True)
    for h in HORIZONS:
        entry = registry.get("horizons", {}).get(h)
        if isinstance(entry, dict):
            (score_dir / f"{h}.md").write_text(scorecard(h, entry), encoding="utf-8")

    mcp_dir = out_dir / "mcp"
    mcp_dir.mkdir(exist_ok=True)
    (mcp_dir / "release.json").write_text(mcp_manifest(registry), encoding="utf-8")

    print(f"artifacts written under {out_dir}")


if __name__ == "__main__":
    main()