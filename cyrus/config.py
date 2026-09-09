"""Configuration loading.

Config is data, not code. The risk kernel reads these limits and never edits
them. Loading validates the dangerous fields up front, because a typo in a
risk limit is a silent way to lose the bloodline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")


@dataclass
class BookConfig:
    name: str
    enabled: bool
    instruments: List[str]
    style: str
    timeframe: str
    session: str
    max_risk_pct: float
    params: Dict[str, Any] = field(default_factory=dict)
    # Parameter grid for walk-forward selection. Empty means nothing is fitted,
    # which the walk-forward report must then label as in-sample.
    optimize: Dict[str, List[Any]] = field(default_factory=dict)
    # Venue costs for this book. None falls back to the desk-wide figure, which
    # is set to the most expensive venue so a missing override cannot flatter a
    # result.
    slippage_bps: Optional[float] = None
    commission_bps: Optional[float] = None


@dataclass
class FactorConfig:
    name: str
    budget_pct: float
    books: List[str]


@dataclass
class RiskConfig:
    kelly_fraction: float
    max_risk_per_trade_pct: float
    max_open_positions: int
    max_book_exposure_pct: float
    daily_loss_halt_pct: float
    drawdown_flatten_pct: float
    min_reward_risk: float
    consensus_quorum: int
    consensus_runs: int


@dataclass
class BurnConfig:
    llm_usd: float
    vps_usd: float
    data_usd: float
    hurdle_multiple: float

    @property
    def monthly_total(self) -> float:
        return self.llm_usd + self.vps_usd + self.data_usd


@dataclass
class LiquidityConfig:
    min_avg_dollar_volume: float
    memecoin_max_risk_pct: float
    memecoin_requires_human: bool


@dataclass
class Costs:
    """What one round trip costs on a given book's venue, in basis points."""

    slippage_bps: float
    commission_bps: float

    @property
    def slippage(self) -> float:
        return self.slippage_bps / 10_000.0

    @property
    def commission(self) -> float:
        return self.commission_bps / 10_000.0


@dataclass
class ExecutionConfig:
    venue: str
    slippage_bps: float
    commission_bps: float
    reject_outside_session: bool
    idempotent_client_ids: bool


@dataclass
class DeskConfig:
    mode: str
    base_currency: str
    paper_equity: float
    live_equity: float
    cash_buffer_months: int
    burn: BurnConfig
    risk: RiskConfig
    liquidity: LiquidityConfig
    execution: ExecutionConfig
    books: Dict[str, BookConfig]
    factors: Dict[str, FactorConfig]

    @property
    def is_live(self) -> bool:
        return self.mode in ("live", "micro_live")

    @property
    def equity(self) -> float:
        return self.live_equity if self.is_live else self.paper_equity

    def enabled_books(self) -> List[BookConfig]:
        return [b for b in self.books.values() if b.enabled]

    def book(self, name: str) -> Optional[BookConfig]:
        return self.books.get(name)

    def costs_for(self, book: Optional[BookConfig]) -> Costs:
        """Slippage and commission for a book, in basis points.

        Takes the book object rather than its name so a caller holding a book
        definition gets that book's costs, not whatever the global config
        happens to have registered under the same name.
        """
        slippage = self.execution.slippage_bps
        commission = self.execution.commission_bps
        if book is not None:
            if book.slippage_bps is not None:
                slippage = book.slippage_bps
            if book.commission_bps is not None:
                commission = book.commission_bps
        return Costs(slippage_bps=slippage, commission_bps=commission)

    def costs_for_book(self, name: Optional[str]) -> Costs:
        """Costs by book name, for callers that only have the name on a message.

        An unknown book falls back to the desk-wide figures rather than to
        zero, because a book nobody configured is not a free one.
        """
        return self.costs_for(self.books.get(name or ""))

    def factor_for_book(self, book: str) -> Optional[FactorConfig]:
        """The factor budget a book draws from.

        SPY, QQQ, and BTC longs are usually one risk-on bet. This lookup is
        what stops the desk from funding the same trade three times.
        """
        for factor in self.factors.values():
            if book in factor.books:
                return factor
        return None


def load_desk_config(path: Optional[str] = None) -> DeskConfig:
    path = path or os.path.join(CONFIG_DIR, "desk.yaml")
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    capital = raw.get("capital", {})
    burn = BurnConfig(**_defaults(raw.get("burn", {}), {
        "llm_usd": 0.0, "vps_usd": 0.0, "data_usd": 0.0, "hurdle_multiple": 2.0,
    }))
    risk = RiskConfig(**_defaults(raw.get("risk", {}), {
        "kelly_fraction": 0.25,
        "max_risk_per_trade_pct": 1.0,
        "max_open_positions": 6,
        "max_book_exposure_pct": 30.0,
        "daily_loss_halt_pct": 3.0,
        "drawdown_flatten_pct": 9.0,
        "min_reward_risk": 2.0,
        "consensus_quorum": 3,
        "consensus_runs": 3,
    }))
    liquidity = LiquidityConfig(**_defaults(raw.get("liquidity", {}), {
        "min_avg_dollar_volume": 5_000_000.0,
        "memecoin_max_risk_pct": 0.1,
        "memecoin_requires_human": True,
    }))
    execution = ExecutionConfig(**_defaults(raw.get("execution", {}), {
        "venue": "paper",
        "slippage_bps": 5.0,
        "commission_bps": 0.0,
        "reject_outside_session": True,
        "idempotent_client_ids": True,
    }))

    books: Dict[str, BookConfig] = {}
    for name, spec in (raw.get("books") or {}).items():
        params = dict(spec.get("params") or {})
        # The grid lives beside the params in YAML but must not leak into them,
        # or a strategy would read a list where it expects a number.
        optimize = {
            key: list(values)
            for key, values in (spec.get("optimize") or {}).items()
            if isinstance(values, list) and values
        }
        books[name] = BookConfig(
            name=name,
            enabled=bool(spec.get("enabled", False)),
            instruments=list(spec.get("instruments") or []),
            style=str(spec.get("style", "")),
            timeframe=str(spec.get("timeframe", "")),
            session=str(spec.get("session", "always")),
            max_risk_pct=float(spec.get("max_risk_pct", 0.5)),
            params=params,
            optimize=optimize,
            slippage_bps=_optional_float(spec.get("slippage_bps")),
            commission_bps=_optional_float(spec.get("commission_bps")),
        )

    factors: Dict[str, FactorConfig] = {}
    for name, spec in (raw.get("factors") or {}).items():
        factors[name] = FactorConfig(
            name=name,
            budget_pct=float(spec.get("budget_pct", 1.0)),
            books=list(spec.get("books") or []),
        )

    config = DeskConfig(
        mode=str(raw.get("mode", "paper")),
        base_currency=str(raw.get("base_currency", "USD")),
        paper_equity=float(capital.get("paper_equity", 100_000.0)),
        live_equity=float(capital.get("live_equity", 0.0)),
        cash_buffer_months=int(capital.get("cash_buffer_months", 6)),
        burn=burn,
        risk=risk,
        liquidity=liquidity,
        execution=execution,
        books=books,
        factors=factors,
    )
    _validate(config)
    return config


def load_agent_config(path: Optional[str] = None) -> Dict[str, Any]:
    path = path or os.path.join(CONFIG_DIR, "agents.yaml")
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _optional_float(value: Any) -> Optional[float]:
    """None stays None so the desk-wide fallback applies; 0.0 stays 0.0."""
    if value is None:
        return None
    return float(value)


def _defaults(given: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(defaults)
    for key, value in (given or {}).items():
        if key in merged:
            merged[key] = value
    return merged


def _validate(config: DeskConfig) -> None:
    """Reject configurations that could only lose money faster."""
    problems: List[str] = []
    risk = config.risk

    if not 0.0 < risk.kelly_fraction <= 0.5:
        problems.append(
            "kelly_fraction must be in (0, 0.5]; full Kelly is forbidden (AGENTS.md §2.5)"
        )
    if not 0.0 < risk.max_risk_per_trade_pct <= 2.0:
        problems.append("max_risk_per_trade_pct must be in (0, 2]")
    if risk.drawdown_flatten_pct <= risk.daily_loss_halt_pct:
        problems.append("drawdown_flatten_pct must exceed daily_loss_halt_pct")
    if risk.min_reward_risk < 1.0:
        problems.append("min_reward_risk below 1.0 pays less than it risks")
    if risk.consensus_quorum < 2:
        problems.append("consensus_quorum below 2 is a single opinion, not consensus")
    if config.mode not in ("paper", "micro_live", "live"):
        problems.append("mode must be paper, micro_live, or live")
    if config.is_live and config.live_equity <= 0:
        problems.append("live mode requires live_equity above zero")

    for book in config.books.values():
        if book.enabled and book.max_risk_pct > risk.max_risk_per_trade_pct * 4:
            problems.append(
                "book %s risk cap (%.2f%%) is more than 4x the per-trade cap"
                % (book.name, book.max_risk_pct)
            )
        if book.enabled and not book.instruments:
            problems.append("book %s is enabled with no instruments" % book.name)

        for label, value in (
            ("slippage_bps", book.slippage_bps),
            ("commission_bps", book.commission_bps),
        ):
            if value is not None and value < 0:
                problems.append("book %s has a negative %s" % (book.name, label))

        costs = config.costs_for(book)
        if book.enabled and costs.slippage_bps + costs.commission_bps <= 0:
            problems.append(
                "book %s trades at zero cost; a result without fees and slippage "
                "is not a result (AGENTS.md §9)" % book.name
            )

        # A grid key that does not match a real parameter optimises nothing and
        # fails silently, so a typo here has to be a load-time error.
        for key in book.optimize:
            if key not in book.params:
                problems.append(
                    "book %s optimises %r, which is not one of its params" % (book.name, key)
                )
        combinations = 1
        for values in book.optimize.values():
            combinations *= len(values)
        if combinations > 64:
            problems.append(
                "book %s grid has %d combinations; every extra one is another "
                "chance to fit noise (64 is the ceiling)" % (book.name, combinations)
            )

    if problems:
        raise ValueError("Invalid desk config:\n  - " + "\n  - ".join(problems))


__all__ = [
    "REPO_ROOT",
    "CONFIG_DIR",
    "BookConfig",
    "FactorConfig",
    "RiskConfig",
    "BurnConfig",
    "LiquidityConfig",
    "ExecutionConfig",
    "Costs",
    "DeskConfig",
    "load_desk_config",
    "load_agent_config",
]
