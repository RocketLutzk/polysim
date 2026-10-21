"""Клієнт лише для ЧИТАННЯ публічних даних Polymarket.
Жодних гаманців, ключів чи ордерів — бот нічого не купує насправді."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

import requests

from .models import Market

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


class ApiError(RuntimeError):
    pass


class PolyClient:
    def __init__(self, gamma: str = GAMMA, clob: str = CLOB,
                 cache_dir: Optional[str] = ".cache", pause: float = 0.15,
                 session: Optional[requests.Session] = None):
        self.gamma = gamma.rstrip("/")
        self.clob = clob.rstrip("/")
        self.pause = pause
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = "polysim-paper-trader/1.0"
        self.cache = Path(cache_dir) if cache_dir else None
        if self.cache:
            self.cache.mkdir(parents=True, exist_ok=True)

    # ---------- HTTP ----------
    def _get(self, url: str, params: dict | None = None):
        last = None
        for attempt in range(4):
            try:
                r = self.s.get(url, params=params, timeout=20)
                if r.status_code == 429 or r.status_code >= 500:
                    last = ApiError(f"HTTP {r.status_code} для {r.url}")
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise ApiError(f"HTTP {r.status_code} для {r.url}: {r.text[:200]}")
                time.sleep(self.pause)
                return r.json()
            except requests.RequestException as e:
                last = ApiError(f"Немає з'єднання з {url}: {e}")
                time.sleep(1.5 * (attempt + 1))
        raise last

    # ---------- Ринки ----------
    def iter_markets(self, *, closed: bool, max_markets: int = 2000,
                     end_date_min: datetime | None = None,
                     end_date_max: datetime | None = None,
                     min_volume: float = 0) -> Iterator[Market]:
        # Polymarket віддає максимум 100 ринків за запит (docs: limit max 100)
        page, offset, seen = 100, 0, 0
        while seen < max_markets and offset < 50_000:
            params = {"closed": str(closed).lower(), "limit": page, "offset": offset}
            if not closed:
                params["active"] = "true"
            if end_date_min:
                params["end_date_min"] = end_date_min.strftime("%Y-%m-%dT%H:%M:%SZ")
            if end_date_max:
                params["end_date_max"] = end_date_max.strftime("%Y-%m-%dT%H:%M:%SZ")
            if min_volume:
                params["volume_num_min"] = min_volume
            data = self._get(f"{self.gamma}/markets", params)
            if not data:
                return
            for d in data:
                m = Market.from_api(d)
                # фільтруємо й локально — на випадок, якщо API ігнорує параметр
                if min_volume and m.volume < min_volume:
                    continue
                if end_date_min and m.end_date and m.end_date < end_date_min:
                    continue
                if end_date_max and m.end_date and m.end_date > end_date_max:
                    continue
                yield m
                seen += 1
                if seen >= max_markets:
                    return
            # не довіряємо розміру сторінки: йдемо далі, поки API не поверне порожній список
            offset += len(data)

    def get_market(self, market_id: str) -> Market:
        return Market.from_api(self._get(f"{self.gamma}/markets/{market_id}"))

    # ---------- Історія цін ----------
    def price_history(self, token_id: str, start_ts: int, end_ts: int,
                      fidelity: int = 60) -> list[tuple[int, float]]:
        """[(unix_time, price)] для одного варіанту. Кешується на диск."""
        key = f"ph_{token_id[-24:]}_{start_ts}_{end_ts}_{fidelity}.json"
        if self.cache and (self.cache / key).exists():
            return [tuple(x) for x in json.loads((self.cache / key).read_text())]
        data = self._get(f"{self.clob}/prices-history", {
            "market": token_id, "startTs": start_ts, "endTs": end_ts,
            "fidelity": fidelity,
        })
        hist = sorted((int(p["t"]), float(p["p"])) for p in (data or {}).get("history", []))
        if self.cache:
            (self.cache / key).write_text(json.dumps(hist))
        return hist


def price_at(history: list[tuple[int, float]], ts: int,
             max_age: int = 6 * 3600) -> Optional[float]:
    """Остання відома ціна НЕ ПІЗНІШЕ ts (щоб не «підглядати в майбутнє»)."""
    best = None
    for t, p in history:
        if t <= ts:
            best = (t, p)
        else:
            break
    if best is None or ts - best[0] > max_age:
        return None
    return best[1]
