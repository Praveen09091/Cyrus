"""Backtest report storage.

Reports land under ``ledger/backtests/`` because they are evidence, and
evidence in this repo is append-only: a report is written once under a
timestamped name and never edited. Re-running a backtest produces a new file,
so a strategy's evidence trail shows what was believed and when.

The gate that matters is :func:`strategy_records`. It converts stored reports
into the ``StrategyRecord`` objects Oracle sizes from, and it refuses anything
that is in-sample, synthetic, or too small to mean anything. Loosening this
function is equivalent to letting the desk size on a curve fit.
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, Iterable, List, Optional

from cyrus.agents.oracle import StrategyRecord
from cyrus.backtest.walkforward import WalkForwardReport
from cyrus.config import REPO_ROOT

BACKTEST_DIR = os.path.join(REPO_ROOT, "ledger", "backtests")

# A data label may only count as evidence if the bars came from a real market.
# The synthetic feed exists to exercise plumbing and says so about itself.
REAL_DATA_MARKERS = ("yahoo",)


def save_report(report: WalkForwardReport, root: Optional[str] = None) -> str:
    """Write a report to a fresh timestamped file and return its path."""
    directory = root or BACKTEST_DIR
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
    name = "%s--%s--%s.json" % (
        _slug(report.strategy),
        _slug(report.instrument),
        stamp,
    )
    path = os.path.join(directory, name)
    payload = report.to_dict()
    payload["written_ts"] = time.time()
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return path


def load_reports(root: Optional[str] = None) -> List[Dict[str, object]]:
    directory = root or BACKTEST_DIR
    if not os.path.isdir(directory):
        return []
    reports: List[Dict[str, object]] = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            # A corrupt report is not evidence. Skip it loudly at the caller.
            continue
        payload["_path"] = path
        reports.append(payload)
    return reports


def latest_per_series(
    reports: Iterable[Dict[str, object]]
) -> Dict[tuple, Dict[str, object]]:
    """Newest report per (strategy, instrument).

    Keyed on both because one strategy runs across several instruments. Keying
    on the strategy alone would let the last instrument backtested silently
    erase every other one's evidence.
    """
    newest: Dict[tuple, Dict[str, object]] = {}
    for payload in reports:
        strategy = str(payload.get("strategy", ""))
        instrument = str(payload.get("instrument", ""))
        if not strategy:
            continue
        key = (strategy, instrument)
        current = newest.get(key)
        if current is None or float(payload.get("written_ts", 0.0)) > float(
            current.get("written_ts", 0.0)
        ):
            newest[key] = payload
    return newest


def is_admissible(payload: Dict[str, object]) -> tuple:
    """Whether a report is the right *kind* of evidence, ignoring its size.

    This checks provenance only: was it scored out-of-sample, and did the bars
    come from a real market. Sample size is a separate question, answered after
    a strategy's instruments are pooled.
    """
    if not payload.get("out_of_sample"):
        return False, "report is in-sample; parameters were not selected on unseen data"

    label = str(payload.get("data_label", "unknown")).lower()
    if not any(marker in label for marker in REAL_DATA_MARKERS):
        return False, "data label %r is not a real market feed" % label

    return True, "out-of-sample on %s" % label


def is_evidence(payload: Dict[str, object], min_trades: int = 30) -> tuple:
    """Whether a single report could stand on its own as evidence.

    Used for reporting one backtest at a time. Sizing uses
    :func:`strategy_records`, which pools a strategy's instruments first.
    """
    admissible, reason = is_admissible(payload)
    if not admissible:
        return False, reason

    metrics = payload.get("metrics") or {}
    count = int(metrics.get("trade_count", 0))
    if count < min_trades:
        return False, "only %d out-of-sample trades; %d is the floor" % (count, min_trades)

    label = str(payload.get("data_label", "unknown")).lower()
    return True, "%d out-of-sample trades on %s" % (count, label)


def strategy_records(
    root: Optional[str] = None,
    min_trades: int = 30,
    reports: Optional[Iterable[Dict[str, object]]] = None,
) -> Dict[str, StrategyRecord]:
    """Build Oracle-shaped records from reports that qualify as evidence.

    All of a strategy's instruments are pooled into one record, because the
    edge belongs to the strategy and the desk runs it on every name in the
    book. Pooling is also the conservative choice: a good SPY result cannot be
    presented on its own while a bad QQQ result sits in the same folder.

    Records are rebuilt from individual trade PnLs rather than summary metrics,
    so the win rate Oracle sizes from is the one the trades actually produced.
    """
    payloads = list(reports) if reports is not None else load_reports(root)
    pooled: Dict[str, StrategyRecord] = {}

    for (strategy, _instrument), payload in sorted(
        latest_per_series(payloads).items()
    ):
        admissible, _reason = is_admissible(payload)
        if not admissible:
            continue
        record = pooled.setdefault(strategy, StrategyRecord(strategy))
        for trade in payload.get("trades") or []:
            record.record(float(trade.get("net_pnl", 0.0)))

    # The size floor applies to the pooled sample, not to each instrument.
    qualified: Dict[str, StrategyRecord] = {}
    for strategy, record in pooled.items():
        if record.sample_size < min_trades:
            continue
        record.out_of_sample = True
        qualified[strategy] = record
    return qualified


def refusals(
    root: Optional[str] = None,
    min_trades: int = 30,
    reports: Optional[Iterable[Dict[str, object]]] = None,
) -> Dict[str, str]:
    """Why each strategy's evidence was refused. Used by the HUD and post-mortems."""
    payloads = list(reports) if reports is not None else load_reports(root)
    series = latest_per_series(payloads)

    counts: Dict[str, int] = {}
    reasons: Dict[str, str] = {}
    for (strategy, instrument), payload in sorted(series.items()):
        admissible, reason = is_admissible(payload)
        if not admissible:
            reasons.setdefault(strategy, "%s: %s" % (instrument, reason))
            continue
        trades = len(payload.get("trades") or [])
        counts[strategy] = counts.get(strategy, 0) + trades

    out: Dict[str, str] = {}
    for strategy in sorted(set(list(counts) + list(reasons))):
        pooled = counts.get(strategy, 0)
        if pooled >= min_trades:
            continue
        if pooled == 0 and strategy in reasons:
            out[strategy] = reasons[strategy]
        else:
            out[strategy] = (
                "only %d admissible out-of-sample trades pooled across instruments; "
                "%d is the floor" % (pooled, min_trades)
            )
    return out


def _slug(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text]
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "unknown"


__all__ = [
    "BACKTEST_DIR",
    "save_report",
    "load_reports",
    "latest_per_series",
    "is_admissible",
    "is_evidence",
    "strategy_records",
    "refusals",
]
