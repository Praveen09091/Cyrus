#!/usr/bin/env python3
"""Walk-forward a book's strategy and file the result as evidence.

    python3 run_backtest.py --book us_index
    python3 run_backtest.py --book crypto --instrument BTC-USD --folds 5
    python3 run_backtest.py --book commodity --allow-synthetic   # plumbing only

What this script will not do is let a synthetic run masquerade as evidence.
The synthetic feed exists to prove the harness runs without a network; its
reports are written with their real data label, and ``store.is_evidence``
refuses them at the point where Oracle would otherwise size from them.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from cyrus.backtest.store import is_evidence, save_report
from cyrus.backtest.walkforward import WalkForward, WalkForwardReport
from cyrus.config import load_desk_config
from cyrus.data.sources import build_default_registry
from cyrus.indicators.core import Bar
from cyrus.signals import REGISTRY


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward backtest one book.")
    parser.add_argument("--book", required=True, help="book name from config/desk.yaml")
    parser.add_argument("--instrument", help="single instrument; default is all in the book")
    parser.add_argument("--folds", type=int, default=4, help="walk-forward folds (min 2)")
    parser.add_argument("--bars", type=int, default=2000, help="bars to request per instrument")
    parser.add_argument("--feed", help="force a feed by name, e.g. yahoo or synthetic")
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="run on the synthetic feed to exercise the harness. The report is "
        "written but will never qualify as evidence for sizing.",
    )
    parser.add_argument("--no-save", action="store_true", help="print only, write nothing")
    parser.add_argument(
        "--selection-metric",
        default="expectancy",
        choices=["expectancy", "profit_factor", "net_pnl", "sharpe"],
        help="what training folds are scored on",
    )
    args = parser.parse_args(argv)

    config = load_desk_config()
    book = config.book(args.book)
    if book is None:
        print("No such book: %s" % args.book)
        print("Books: %s" % ", ".join(sorted(config.books)))
        return 2
    if book.style not in REGISTRY:
        print("Book %s has style %r with no registered strategy." % (book.name, book.style))
        return 2

    instruments = [args.instrument] if args.instrument else list(book.instruments)
    if not instruments:
        print("Book %s has no instruments configured." % book.name)
        return 2

    registry = build_default_registry(allow_network=True)
    print("Feeds available: %s" % ", ".join(registry.available()))
    if not book.optimize:
        print(
            "WARNING: book %s has no optimize grid, so nothing can be fitted on "
            "training folds. The report will be labelled in-sample and cannot "
            "inform position size." % book.name
        )

    walk = WalkForward(
        config=config,
        folds=args.folds,
        selection_metric=args.selection_metric,
    )

    exit_code = 0
    for instrument in instruments:
        bars: List[Bar] = registry.fetch(
            instrument, book.timeframe, limit=args.bars, prefer=args.feed
        )
        provenance = registry.provenance(instrument)

        if not bars:
            print("\n%s: no bars from any feed. Nothing tested." % instrument)
            exit_code = 1
            continue

        synthetic = "synthetic" in provenance.lower()
        if synthetic and not args.allow_synthetic:
            print(
                "\n%s: only the synthetic feed answered (%s). Refusing to backtest.\n"
                "  Synthetic bars are not a market. Install yfinance for real data:\n"
                "    python3 -m pip install yfinance\n"
                "  Or pass --allow-synthetic to exercise the harness on fake bars."
                % (instrument, provenance)
            )
            exit_code = 1
            continue

        print(
            "\n%s %s: %d bars from %s (%s .. %s)"
            % (
                instrument,
                book.timeframe,
                len(bars),
                provenance,
                _stamp(bars[0].ts),
                _stamp(bars[-1].ts),
            )
        )

        report = walk.run(
            strategy_factory=REGISTRY[book.style],
            book=book,
            instrument=instrument,
            bars=bars,
            grid=book.optimize,
            data_label=provenance,
        )
        _print(report)

        if not args.no_save:
            path = save_report(report)
            print("  filed: %s" % path)

    return exit_code


def _print(report: WalkForwardReport) -> None:
    print("  %s" % report.summary())
    print("  %s" % report.evidence_note)
    for fold in report.folds:
        params = ", ".join("%s=%s" % (k, v) for k, v in sorted(fold.chosen_params.items())
                           if k in ("entry_z", "atr_stop_mult", "min_edge_atr",
                                    "breakout_lookback", "volume_mult",
                                    "fast_ema", "slow_ema"))
        print("    %s  [%s]" % (fold.describe(), params or "config defaults"))

    # Admissibility and edge are separate questions. A losing strategy can be
    # perfectly good evidence; it is evidence of a loss.
    admissible, reason = is_evidence(report.to_dict())
    print("  admissible as evidence: %s (%s)" % ("yes" if admissible else "no", reason))
    if admissible and report.metrics.has_edge:
        print("  shows a positive out-of-sample edge, so Kelly can be non-zero")
    elif admissible:
        print(
            "  no out-of-sample edge: expectancy $%.2f. Kelly resolves to zero and "
            "the desk will not size this." % report.metrics.expectancy
        )

    if report.metrics.exit_reasons:
        breakdown = ", ".join(
            "%s=%d" % (k, v) for k, v in sorted(report.metrics.exit_reasons.items())
        )
        print("  exits: %s" % breakdown)


def _stamp(ts: float) -> str:
    import datetime

    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    sys.exit(main())
