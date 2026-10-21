"""Структури даних: ринок, знімок ціни, вибір стратегії."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _parse_list(value) -> list:
    """Gamma API віддає списки як JSON-рядки: '["Yes","No"]'."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return []


def parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if s.endswith("+00"):
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _f(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Market:
    id: str
    question: str
    slug: str
    end_date: Optional[datetime]
    outcomes: list
    prices: list            # поточні ціни (0..1) по кожному варіанту
    token_ids: list
    closed: bool
    active: bool
    volume: float = 0.0
    liquidity: float = 0.0
    best_bid: Optional[float] = None   # для першого варіанту (зазвичай "Yes")
    best_ask: Optional[float] = None
    one_day_change: Optional[float] = None
    category: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "Market":
        return cls(
            id=str(d.get("id")),
            question=d.get("question") or "",
            slug=d.get("slug") or "",
            end_date=parse_dt(d.get("endDate") or d.get("endDateIso")),
            outcomes=_parse_list(d.get("outcomes")),
            prices=[_f(p, 0.0) for p in _parse_list(d.get("outcomePrices"))],
            token_ids=[str(t) for t in _parse_list(d.get("clobTokenIds"))],
            closed=bool(d.get("closed")),
            active=bool(d.get("active")),
            volume=_f(d.get("volumeNum"), _f(d.get("volume"), 0.0)) or 0.0,
            liquidity=_f(d.get("liquidityNum"), _f(d.get("liquidity"), 0.0)) or 0.0,
            best_bid=_f(d.get("bestBid")),
            best_ask=_f(d.get("bestAsk")),
            one_day_change=_f(d.get("oneDayPriceChange")),
            category=d.get("category") or "",
            raw=d,
        )

    @property
    def is_binary(self) -> bool:
        return len(self.outcomes) == 2 and len(self.prices) == 2

    @property
    def winner_index(self) -> Optional[int]:
        """Індекс варіанту, що виграв. None — ще не вирішено, або результат
        неоднозначний (50/50, 0/0) — такі ставки вважаємо анульованими."""
        if not self.closed or not self.prices:
            return None
        top = max(self.prices)
        if top < 0.99:
            return None
        winners = [i for i, p in enumerate(self.prices) if p >= 0.99]
        return winners[0] if len(winners) == 1 else None

    @property
    def url(self) -> str:
        return f"https://polymarket.com/market/{self.slug}" if self.slug else ""


@dataclass
class Snapshot:
    """Що стратегія «бачить» у момент рішення. Однаково для живого режиму
    і для бектесту — тому одна й та сама стратегія працює в обох."""
    market: Market
    now: datetime
    prices: list                 # ціна кожного варіанту зараз
    change_24h: Optional[float]  # зміна ціни першого варіанту за 24 год
    ask_prices: Optional[list] = None  # реальна ціна купівлі, якщо відома

    @property
    def days_to_end(self) -> Optional[float]:
        if not self.market.end_date:
            return None
        return (self.market.end_date - self.now).total_seconds() / 86400


@dataclass
class Pick:
    outcome_index: int
    reason: str
