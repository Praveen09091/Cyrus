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
        books[name] = BookConfig(
            name=name,
            enabled=bool(spec.get("enabled", False)),
            instruments=list(spec.get("instruments") or []),
            style=str(spec.get("style", "")),
            timeframe=str(spec.get("timeframe", "")),
            session=str(spec.get("session", "always")),
            max_risk_pct=float(spec.get("max_risk_pct", 0.5)),
            params=dict(spec.get("params") or {}),
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
    "DeskConfig",
    "load_desk_config",
    "load_agent_config",
]
