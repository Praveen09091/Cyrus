# Backtest reports

Walk-forward reports written by `run_backtest.py`, one JSON file per
`(strategy, instrument)` run, named with the UTC timestamp of the run.

These are evidence, so they follow the same rule as the rest of `ledger/`:
**append-only.** A file is written once and never edited. Re-running a backtest
produces a new file, which is what makes the evidence trail readable — you can
see what the desk believed and when it believed it.

`build_desk()` loads whatever qualifies and hands it to Oracle. Qualifying is
narrow on purpose (see `cyrus/backtest/store.py`):

- scored out-of-sample, meaning parameters were chosen on training folds only
- bars came from a real market feed, never the synthetic one
- at least 30 trades once every instrument for that strategy is pooled

A report that fails any of these still lands here. It is refused at the sizing
gate rather than deleted, and `Oracle.why_no_size(strategy)` will quote the
reason. A negative result is evidence too; it is evidence of a loss.

The contents are gitignored because they are regenerated from the data and the
config. This README is tracked.
