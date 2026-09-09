# Cyrus

An accountable trading desk. Ten seats, one brain, one veto.

Specialists propose. Cyrus decides. Sentinel vetoes. Pilot executes. Nothing
else touches money.

**Status: paper only.** The shipped config runs in `paper` mode against a paper
broker, and there is no code path that makes live execution the accidental
default. See [AGENTS.md](AGENTS.md) for the full mandate and hard rules.

## Why it is built this way

A language model asked the same question twice gives different answers. That is
fine for research and fatal for execution. So the desk is split:

- **The brain proposes.** Cyrus and the analysis seats generate ideas, argue
  both sides, and write down what would prove them wrong.
- **The kernel decides what is allowed.** `cyrus/risk/` is arithmetic. No model
  call lives there. When the LLM is offline, jailbroken, or confidently wrong,
  the stops and the flatten rules still hold.

Authority is structural rather than advisory. A proposing seat raises
`PermissionError` if it tries to emit a `Verdict`, and Pilot refuses any order
without a live, single-use authorisation from the kernel.

## Quick start

```bash
python3 -m unittest discover -s tests      # 67 tests, no network needed
python3 run_desk.py                        # one pass, offline synthetic feed
python3 run_desk.py --network              # use Yahoo instead (DELAYED data)
python3 run_desk.py --ask "where do we stand"
```

Optional, for real market data:

```bash
python3 -m pip install yfinance
```

## The seats

| Seat | Job | Can move money |
| --- | --- | --- |
| `cyrus` | Routes, synthesises, decides, answers, owns the outcome | No |
| `atlas` | Macro regime: rates, energy, gold, risk-on/off | No |
| `scout` | Crypto and social recon, liquidity screening | No |
| `athena` | News, filings, event calendar | No |
| `chartist` | Price structure, indicators, proposals with stops | No |
| `oracle` | Backtests, Kelly estimate, spectral diagnostics | No |
| `sentinel` | Veto. Deterministic. Reports to you, not to Cyrus | Veto only |
| `pilot` | Submits authorised orders | Yes, post-stamp |
| `ledger` | Append-only journal, grades old theses | No |
| `quartermaster` | Burn, runway, self-pay hurdle | No |

## The loop

```
scan → research → debate → size → veto → execute → journal
```

Debate runs `consensus_runs` times. An idea must appear in **every** run and
clear `consensus_quorum` approving seats, with zero specialist rejections. When
the desk disagrees with itself, the output is cash.

## Survival rules

The objective is `time_alive × equity_above_burn`, not return.

| Rule | Default |
| --- | --- |
| Kelly fraction | 0.25, hard-capped at 0.5 by config validation |
| Risk per trade | 1% of equity, sized from the stop |
| Daily loss halt | 3%, clears the next day |
| Drawdown flatten | 9%, does **not** clear overnight |
| Reward/risk floor | 2.0 |
| Correlated books | SPY, QQQ, and BTC share one `risk_on` factor budget |
| Burn buffer | 6 months of operating cost, untouchable |

A flatten needs a human to clear. A desk that cannot cover its burn shrinks or
idles, because trading harder to make it back is how accounts die.

## Layout

```
AGENTS.md            desk constitution + llm-wiki schema
config/
  desk.yaml          books, risk limits, burn (validated on load)
  agents.yaml        roster, hierarchy, per-seat data sources
cyrus/
  bus/               typed envelopes + journaled pub/sub
  agents/            the ten seats
  signals/           mean reversion, momentum, trend
  indicators/        core math + spectral diagnostics
  risk/              Kelly, sizing, state, kernel   (no model calls)
  data/              source registry, Yahoo, synthetic
  execution/         paper broker
  brain/             llm-wiki ingest / query / lint
raw/                 immutable sources
wiki/                Cyrus-owned synthesis
ledger/              append-only trade record
docs/HUD.md          interface hierarchy, motion, signal vocabulary
tests/               the gate tests
```

## The brain

Three layers, after [Karpathy's llm-wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f):
`raw/` holds immutable sources, `wiki/` is Cyrus-owned markdown, and the schema
lives in `AGENTS.md`. Operations are ingest, query, and lint.

One deliberate deviation: **the ledger is not part of the wiki.** A model that
can revise its own trade record cannot be trusted to report on its own
performance. Numbers live in `ledger/`, append-only, and the wiki is only ever
allowed to interpret them.

Open the repo root as an Obsidian vault for the graph view. Obsidian is a
viewer, not a dependency.

## Honest limits

- No proven edge. The strategies are starting points, and Oracle grants zero
  size to any strategy with no realised out-of-sample record. That is why a
  fresh desk trades nothing.
- Yahoo data is delayed. Acceptable for research and the slow books, not for
  reacting to a 15-minute close.
- The synthetic feed is plumbing, not a market model. Anything built on it is a
  wiring test.
- A stop is not a guarantee. Gaps, halts, and weekend crypto moves can jump it.
  Position sizing makes ruin unlikely, never impossible.

## Not wired, on purpose

No live broker adapter. No third-party service that wants a broker master
password. No copy-trade feed with authority to size. Live keys, when they exist,
must have withdrawals disabled.
