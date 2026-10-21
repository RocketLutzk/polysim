"""Стратегії. Кожна отримує Snapshot і повертає Pick (на що ставити) або None.

Щоб додати свою стратегію: скопіюй будь-який клас, зміни name і логіку
в decide(), і додай екземпляр у список ALL внизу файлу.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from .models import Pick, Snapshot


class Strategy:
    name = "base"
    description = ""
    min_volume = 5_000      # ігноруємо «мертві» ринки

    def decide(self, s: Snapshot) -> Optional[Pick]:
        raise NotImplementedError

    def _ok(self, s: Snapshot, max_days: float, min_days: float = 0.05) -> bool:
        d = s.days_to_end
        return (s.market.is_binary and d is not None and min_days <= d <= max_days
                and s.market.volume >= self.min_volume)


class Favorite(Strategy):
    """Ставимо на явного фаворита незадовго до закриття.
    Гіпотеза: ринок трохи недооцінює майже-певні результати."""
    name = "favorite"
    description = "Фаворит 80–95% за 1–7 днів до кінця"
    lo, hi, max_days = 0.80, 0.95, 7

    def decide(self, s):
        if not self._ok(s, self.max_days, min_days=1):
            return None
        for i, p in enumerate(s.prices):
            if self.lo <= p <= self.hi:
                return Pick(i, f"ціна {p:.2f}, {s.days_to_end:.1f} дн. до кінця")
        return None


class Longshot(Strategy):
    """Ставимо на аутсайдера (3–15%). Класично такі ставки ПЕРЕОЦІНЕНІ
    (longshot bias) — корисно бачити, як вона програє."""
    name = "longshot"
    description = "Аутсайдер 3–15% за ≤14 днів"
    lo, hi, max_days = 0.03, 0.15, 14

    def decide(self, s):
        if not self._ok(s, self.max_days):
            return None
        for i, p in enumerate(s.prices):
            if self.lo <= p <= self.hi:
                return Pick(i, f"ціна {p:.2f}")
        return None


class Momentum(Strategy):
    """Якщо ціна за добу різко виросла — купуємо в тому ж напрямку."""
    name = "momentum"
    description = "Рух ціни ≥5 п.п. за добу → у той же бік"
    threshold, max_days = 0.05, 30

    def decide(self, s):
        if not self._ok(s, self.max_days) or s.change_24h is None:
            return None
        ch = s.change_24h
        if ch >= self.threshold and s.prices[0] < 0.90:
            return Pick(0, f"+{ch*100:.1f} п.п. за добу")
        if ch <= -self.threshold and s.prices[1] < 0.90:
            return Pick(1, f"{ch*100:.1f} п.п. за добу")
        return None


class RandomBaseline(Strategy):
    """Монетка. Еталон: якщо стратегія не краща за монетку — вона нічого не варта."""
    name = "random"
    description = "Випадкова сторона (еталон)"
    max_days = 14

    def decide(self, s):
        if not self._ok(s, self.max_days):
            return None
        h = int(hashlib.md5(s.market.id.encode()).hexdigest(), 16)
        side = h % 2
        if not 0.05 <= s.prices[side] <= 0.95:
            return None
        return Pick(side, "випадковий вибір")


ALL = [Favorite(), Longshot(), Momentum(), RandomBaseline()]


def by_names(names: Optional[str]) -> list[Strategy]:
    if not names:
        return ALL
    wanted = {n.strip() for n in names.split(",") if n.strip()}
    found = [s for s in ALL if s.name in wanted]
    missing = wanted - {s.name for s in found}
    if missing:
        raise SystemExit(f"Невідомі стратегії: {', '.join(sorted(missing))}. "
                         f"Є: {', '.join(s.name for s in ALL)}")
    return found
