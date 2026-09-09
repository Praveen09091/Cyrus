#!/usr/bin/env python3
"""Run one pass of the desk and print what happened.

Paper only. This script cannot place a real order: the config ships in paper
mode and the paper broker is the only venue wired.

    python3 run_desk.py                 # every enabled book, offline feed
    python3 run_desk.py --network       # try Yahoo first (DELAYED data)
    python3 run_desk.py --book crypto
    python3 run_desk.py --ask "where do we stand"
"""

from __future__ import annotations

import argparse
import sys

from cyrus.config import load_desk_config
from cyrus.desk import build_desk


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Cyrus desk (paper).")
    parser.add_argument("--book", help="Run a single book instead of all enabled books.")
    parser.add_argument("--network", action="store_true", help="Allow the Yahoo feed (delayed).")
    parser.add_argument("--ask", help="Ask Cyrus a question after the cycle.")
    args = parser.parse_args()

    config = load_desk_config()
    desk = build_desk(config=config, allow_network=args.network)

    print("=" * 78)
    print("CYRUS DESK  mode=%s  equity=$%.2f  venue=%s" % (
        config.mode, config.equity, desk.broker.name))
    print("feeds available: %s" % ", ".join(desk.sources.available()))
    print("=" * 78)

    books = [args.book] if args.book else [b.name for b in config.enabled_books()]
    for book_name in books:
        result = desk.run_cycle(book_name)
        print("\n--- %s  [%s]" % (book_name.upper(), result.cycle_id))
        print("regime            : %s" % result.regime.value)
        print("observations      : %d" % result.observations)
        print("proposals raised  : %d" % len(result.proposals))
        print("survived consensus: %d" % len(result.survivors))
        print("verdicts          : %d approved, %d rejected" % (
            result.executed, len(result.verdicts) - result.executed))
        if result.tripped_switches:
            print("kill switches     : %s" % ", ".join(result.tripped_switches))

        for verdict in result.verdicts:
            status = "APPROVED" if verdict.approved else "REJECTED"
            print("  [%s] %s" % (status, "; ".join(verdict.reasons) or "no reason given"))

        for decision in result.decisions:
            print("  decision: %s" % decision.rationale)
            for line in decision.dissent:
                print("    dissent: %s" % line)

    stats = desk.ledger.stats()
    print("\n" + "=" * 78)
    print("LEDGER  proposals_journaled=%d  verdicts=%d  rejections=%d  closed=%d" % (
        len(desk.ledger.proposals), stats["verdicts"], stats["rejections"],
        stats["closed_trades"]))
    if desk.bus.failures:
        print("BUS FAILURES (a seat raised): %d" % len(desk.bus.failures))
        for failure in desk.bus.failures[:5]:
            print("  %s -> %s" % (failure["handler"], failure["error"]))

    if args.ask:
        print("\n" + "=" * 78)
        print(desk.answer(args.ask))

    return 0


if __name__ == "__main__":
    sys.exit(main())
