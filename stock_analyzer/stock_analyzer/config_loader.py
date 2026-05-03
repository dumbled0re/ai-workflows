from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class Holding:
    ticker: str
    name: str
    shares: int
    avg_cost: float | None = None


@dataclass
class Settings:
    history_days: int = 90
    discovery_top_n: int = 5
    screener_pool_size: int = 50
    claude_model: str = "claude-sonnet-4-5-20250929"


@dataclass
class Config:
    holdings: list[Holding] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)


def _validate_ticker(ticker: str) -> None:
    if not ticker.endswith(".T"):
        raise ValueError(
            f"Invalid ticker format: '{ticker}'. Japanese tickers must end with '.T' (e.g. '7203.T')"
        )


_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "stocks.yml"


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path) if path is not None else _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw:
        raise ValueError("Config file is empty")

    holdings: list[Holding] = []
    for item in raw.get("holdings", []):
        ticker = item.get("ticker", "")
        name = item.get("name", "")
        if not ticker or not name:
            logger.warning("Skipping holding with missing ticker or name: %s", item)
            continue
        _validate_ticker(ticker)
        holdings.append(
            Holding(
                ticker=ticker,
                name=name,
                shares=item.get("shares", 0),
                avg_cost=item.get("avg_cost"),
            )
        )

    raw_settings = raw.get("settings", {})
    settings = Settings(
        history_days=raw_settings.get("history_days", 90),
        discovery_top_n=raw_settings.get("discovery_top_n", 5),
        screener_pool_size=raw_settings.get("screener_pool_size", 50),
        claude_model=raw_settings.get("claude_model", "claude-sonnet-4-5-20250929"),
    )

    return Config(holdings=holdings, settings=settings)
