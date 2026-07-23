"""Validation report entry point (CLAUDE.md section 8, phase 3).

Runs the funding z-score backtest for the tracked assets and prints a report. The point
is an **honest** read of whether the signal has an edge — including "no edge" and
"insufficient data" — not a number to brag about.

Usage:
    python run_validation.py [--z 1.5] [--horizon 7]
"""

from __future__ import annotations

import argparse
import sys

from core.config import load_settings
from core.logging_setup import configure_logging, get_logger
from db.loader import init_db
from validation.backtest import funding_zscore_backtest

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="cryptodash signal validation")
    parser.add_argument("--z", type=float, default=1.5, help="Funding z-score threshold.")
    parser.add_argument("--horizon", type=int, default=7, help="Forward-return horizon (days).")
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings.log_level)
    conn = init_db(settings.db_path)
    try:
        horizon = args.horizon
        print(f"\nFunding z-score >= {args.z} -> forward {horizon}d return (perp close)")
        print(f"{'asset':6} {'n_sig':>5} {'signal%':>9} {'base%':>8} {'edge_pp':>8} {'pvalue':>7}")
        any_rows = False
        for asset in [a["symbol"] for a in settings.assets]:
            res = funding_zscore_backtest(conn, settings, asset, z_threshold=args.z, horizons=(horizon,))
            if res is None:
                continue
            stats = res[horizon]
            if stats["n_signal"] == 0:
                continue
            any_rows = True

            def fmt(x: float | None) -> str:
                return "—" if x is None else f"{x:+.2f}"

            pval = "—" if stats["pvalue"] is None else f"{stats['pvalue']:.3f}"
            print(
                f"{asset:6} {stats['n_signal']:>5} {fmt(stats['mean_signal']):>9} "
                f"{fmt(stats['mean_baseline']):>8} {fmt(stats['edge']):>8} {pval:>7}"
            )
        if not any_rows:
            print("  (no asset produced signal dates — insufficient funding/close history yet)")
        print(
            "\nNota honesta: con pocas semanas de historial (close del perp ~30 d, funding ~90 d) "
            "las muestras son pequeñas y los p-valores poco potentes. Interpretar como preliminar; "
            "reejecutar al acumular datos. Documentar el resultado aunque no haya edge (§8)."
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
