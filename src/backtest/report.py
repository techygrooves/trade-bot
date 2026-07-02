"""Phase B report writer: turn an ExperimentResult into markdown + CSVs.

Outputs (under the chosen --out directory):
  * report.md      — human-readable summary with the decision and caveats.
  * baseline.csv   — default-parameter scheme comparison, all windows.
  * sweep.csv      — every sweep combo's pooled train metrics.
  * champions.csv  — best combo per scheme, train + validation rows.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .experiment import ExperimentResult

_METRIC_COLS = [
    "scheme", "combo", "window", "n_trades", "expectancy_r", "profit_factor",
    "win_rate", "worst_max_dd_pct", "mean_sharpe", "mean_return_pct",
]


def _fmt(v) -> str:
    if isinstance(v, float):
        if v == float("inf"):
            return "inf"
        return f"{v:.2f}"
    return str(v)


def md_table(df: pd.DataFrame, cols: list[str] | None = None) -> str:
    """Render a DataFrame as a GitHub-markdown table (no tabulate dependency)."""
    if df.empty:
        return "_(no rows)_"
    cols = [c for c in (cols or list(df.columns)) if c in df.columns]
    lines = [
        "| " + " | ".join(cols) + " |",
        "|" + "|".join("---" for _ in cols) + "|",
    ]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(_fmt(row[c]) for c in cols) + " |")
    return "\n".join(lines)


def write_report(
    result: ExperimentResult,
    out_dir: str | Path,
    meta: dict,
) -> Path:
    """Write report.md + CSVs; returns the report path.

    `meta` keys used: symbols, interval, trend_interval, windows, risk,
    data_source (set data_source to something explicit like "SYNTHETIC" when
    the run is not on real exchange data, so nobody mistakes it for evidence).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    result.baseline.to_csv(out / "baseline.csv", index=False)
    if not result.sweep.empty:
        result.sweep.to_csv(out / "sweep.csv", index=False)
    if not result.champions.empty:
        result.champions.to_csv(out / "champions.csv", index=False)

    w = meta["windows"]
    lines = [
        "# Phase B — exit-scheme validation report",
        "",
        f"- **Data source:** {meta.get('data_source', 'unknown')}",
        f"- **Symbols:** {', '.join(meta['symbols'])}",
        f"- **Timeframes:** signal {meta['interval']}, trend {meta['trend_interval']}",
        f"- **Train window:** {w.train_start} .. {w.train_end}",
        f"- **Validation window:** {w.val_start} .. {w.val_end}",
        f"- **Friction:** taker fee {meta['risk'].taker_fee_pct}% per fill, "
        f"slippage {meta['risk'].slippage_bps} bps on market fills",
        "",
        "Metrics are pooled across symbols: expectancy / profit factor / win rate "
        "from the pooled trade list; `worst_max_dd_pct` is the worst single-symbol "
        "equity drawdown (no portfolio netting credit). Selection used expectancy "
        "and profit factor under a drawdown cap — win rate is reported but never "
        "selected on.",
        "",
        "## Decision",
        "",
    ]
    if result.winner is not None:
        lines.append(f"**Winner: `{result.winner['combo']}`**")
    else:
        lines.append("**No winner — no scheme passed the validation gate.**")
    lines.append("")
    for note in result.notes:
        lines.append(f"- {note}")

    lines += [
        "",
        "## Baseline — default parameters, all three schemes",
        "",
        md_table(
            result.baseline.sort_values(["scheme", "window"]), _METRIC_COLS
        ),
    ]

    if not result.champions.empty:
        lines += [
            "",
            "## Champions — best combo per scheme (picked on train, scored on validation)",
            "",
            md_table(result.champions, _METRIC_COLS + ["gate_pass"]),
        ]

    if not result.sweep.empty:
        top = (
            result.sweep.sort_values("expectancy_r", ascending=False)
            .groupby("scheme")
            .head(5)
            .sort_values(["scheme", "expectancy_r"], ascending=[True, False])
        )
        lines += [
            "",
            "## Sweep — top 5 combos per scheme by train expectancy",
            "",
            f"_Full grid ({len(result.sweep)} combos) in `sweep.csv`._",
            "",
            md_table(top, _METRIC_COLS + ["gate_pass"]),
        ]

    lines += [
        "",
        "## Caveats",
        "",
        "- Single train/validation split, not a rolling walk-forward; the "
        "validation window is one market regime.",
        "- Per-symbol equity is independent (no shared capital, no "
        "`max_open_positions` constraint) — portfolio effects land in Phase D.",
        "- Positions open at a window edge are force-closed at the last close "
        "(`end_of_data`), slightly penalizing slow schemes at boundaries.",
        "",
    ]
    report = out / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report
