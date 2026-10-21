"""Логіка: сканування ринків (наживо), розрахунок ставок, бектест."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .api import PolyClient, price_at
from .db import BetDB, now_iso
from .models import Market, Snapshot, parse_dt
from .strategies import Strategy

Log = Callable[[str], None]


def bet_math(stake: float, price: float, fee: float) -> tuple[float, float]:
    """Повертає (ціна входу, кількість акцій). Акція виграшу коштує $1."""
    price = min(max(price, 0.001), 0.999)
    shares = stake * (1 - fee) / price
    return price, shares


def settle_amounts(stake: float, shares: float, won: Optional[bool]):
    if won is None:                # анульовано — гроші повертаються
        return "void", stake, 0.0
    payout = shares if won else 0.0
    return ("won" if won else "lost"), payout, payout - stake


# ---------------------------------------------------------------- наживо
def live_snapshot(m: Market, now: datetime) -> Snapshot:
    asks = None
    if m.is_binary:
        a0 = m.best_ask if m.best_ask and 0 < m.best_ask < 1 else m.prices[0]
        a1 = (1 - m.best_bid) if m.best_bid and 0 < m.best_bid < 1 else m.prices[1]
        asks = [a0, a1]
    return Snapshot(market=m, now=now, prices=m.prices, change_24h=m.one_day_change,
                    ask_prices=asks)


def fetch_open(client: PolyClient, max_markets: int, min_volume: float = 1000,
               horizon_days: float | None = None) -> list[Market]:
    """Відкриті бінарні ринки. horizon_days — лише ті, що закриваються в цей термін
    (стратегії дивляться максимум на 30 днів уперед, довші ринки не потрібні)."""
    now = datetime.now(timezone.utc)
    end_max = now + timedelta(days=horizon_days) if horizon_days else None
    return [m for m in client.iter_markets(closed=False, max_markets=max_markets,
                                           end_date_min=now, end_date_max=end_max,
                                           min_volume=min_volume)
            if m.is_binary]


def scan(client: PolyClient, db: BetDB, strategies: list[Strategy], *,
         stake: float, fee: float, slippage: float, max_markets: int,
         log: Log = print, markets: Optional[list[Market]] = None) -> int:
    now = datetime.now(timezone.utc)
    placed = 0
    if markets is None:
        markets = fetch_open(client, max_markets)
    for m in markets:
        if not m.is_binary:
            continue
        snap = live_snapshot(m, now)
        for st in strategies:
            pick = st.decide(snap)
            if not pick:
                continue
            raw_price = (snap.ask_prices or snap.prices)[pick.outcome_index]
            price, shares = bet_math(stake, raw_price + slippage, fee)
            if db.add_bet(mode="paper", run_id="live", strategy=st.name,
                          market_id=m.id, question=m.question, url=m.url,
                          outcome=str(m.outcomes[pick.outcome_index]),
                          outcome_index=pick.outcome_index, entry_price=price,
                          stake=stake, shares=shares, reason=pick.reason,
                          placed_at=now_iso(),
                          end_date=m.end_date.isoformat() if m.end_date else None):
                placed += 1
                log(f"  + [{st.name}] {m.outcomes[pick.outcome_index]} @ {price:.3f} — {m.question[:70]}")
    return placed


def settle_open(client: PolyClient, db: BetDB, log: Log = print,
                still_open: Optional[set] = None) -> int:
    """Розрахунок ставок. still_open — id ринків, які щойно прийшли як відкриті:
    їх не перевіряємо, щоб не робити зайвих запитів (ставок можуть бути тисячі)."""
    done = 0
    cache: dict[str, Market] = {}
    for b in db.open_bets("paper"):
        if still_open and b["market_id"] in still_open:
            continue
        m = cache.get(b["market_id"])
        if m is None:
            try:
                m = cache[b["market_id"]] = client.get_market(b["market_id"])
            except Exception as e:  # noqa: BLE001
                log(f"  ! не вдалося перевірити ринок {b['market_id']}: {e}")
                continue
        if not m.closed:
            continue
        w = m.winner_index
        db.conn.execute("UPDATE markets SET closed=1, winner=? WHERE market_id=?", (w, m.id))
        won = None if w is None else (w == b["outcome_index"])
        status, payout, pnl = settle_amounts(b["stake"], b["shares"], won)
        db.settle(b["id"], status, payout, pnl)
        done += 1
        log(f"  = [{b['strategy']}] {status.upper():4} {pnl:+.2f}$ — {b['question'][:70]}")
    return done


def cycle(client: PolyClient, db: BetDB, strategies: list[Strategy], *,
          stake: float, fee: float, slippage: float, max_markets: int,
          log: Log = print) -> dict:
    """Один цикл для сервера: розрахунок → знімок цін → нові ставки.
    Помилка в циклі не зупиняє бота — записується в журнал і чекаємо наступного."""
    res = {"markets": 0, "new_bets": 0, "settled": 0}
    try:
        # знімаємо лише ринки, на які стратегії взагалі можуть поставити —
        # дрібні й далекі ринки тільки роздувають дані
        min_vol = min((st.min_volume for st in strategies), default=1000)
        horizon = max((getattr(st, "max_days", 30) for st in strategies), default=30) + 1
        markets = fetch_open(client, max_markets, min_volume=min_vol, horizon_days=horizon)
        res["markets"] = len(markets)
        res["settled"] = settle_open(client, db, log=log,
                                     still_open={m.id for m in markets})
        db.add_snapshots(now_iso(), markets)
        res["new_bets"] = scan(client, db, strategies, stake=stake, fee=fee,
                               slippage=slippage, max_markets=max_markets,
                               log=log, markets=markets)
        db.log_run(True, **res)
        log(f"  ✓ ринків: {res['markets']}, нових ставок: {res['new_bets']}, "
            f"розраховано: {res['settled']}")
    except Exception as e:  # noqa: BLE001
        db.log_run(False, **res, message=f"{type(e).__name__}: {e}")
        log(f"  ✗ помилка циклу: {e}")
        res["error"] = str(e)
    return res


# ---------------------------------------------------------------- бектест
def real_end(m: Market) -> Optional[datetime]:
    """Ринки часто закриваються раніше endDate — беремо ранішу з дат,
    інакше бектест «знав би» результат наперед."""
    closed_at = parse_dt(m.raw.get("closedTime"))
    ends = [d for d in (m.end_date, closed_at) if d]
    return min(ends) if ends else None


def backtest(client: PolyClient, db: BetDB, strategies: list[Strategy], *,
             start: datetime, end: datetime, days_before: list[float],
             stake: float, fee: float, slippage: float, max_markets: int,
             min_volume: float, run_id: str, log: Log = print) -> dict:
    stats = {"markets": 0, "skipped": 0, "bets": 0}
    for m in client.iter_markets(closed=True, max_markets=max_markets,
                                 end_date_min=start, end_date_max=end,
                                 min_volume=min_volume):
        stats["markets"] += 1
        w = m.winner_index
        t_end = real_end(m)
        if not m.is_binary or w is None or not m.token_ids or not t_end:
            stats["skipped"] += 1
            continue
        taken = set()
        for d in days_before:
            t = t_end - timedelta(days=d)
            ts = int(t.timestamp())
            try:
                hist = client.price_history(m.token_ids[0], ts - 26 * 3600, ts + 3600)
            except Exception as e:  # noqa: BLE001
                log(f"  ! історія цін недоступна для {m.id}: {e}")
                break
            p0 = price_at(hist, ts)
            if p0 is None or not 0.01 < p0 < 0.99:
                continue
            p_prev = price_at(hist, ts - 24 * 3600)
            snap = Snapshot(market=m, now=t, prices=[p0, 1 - p0],
                            change_24h=(p0 - p_prev) if p_prev is not None else None)
            for st in strategies:
                if st.name in taken:
                    continue
                pick = st.decide(snap)
                if not pick:
                    continue
                taken.add(st.name)
                price, shares = bet_math(stake, snap.prices[pick.outcome_index] + slippage, fee)
                status, payout, pnl = settle_amounts(stake, shares, w == pick.outcome_index)
                db.add_bet(mode="backtest", run_id=run_id, strategy=st.name,
                           market_id=m.id, question=m.question, url=m.url,
                           outcome=str(m.outcomes[pick.outcome_index]),
                           outcome_index=pick.outcome_index, entry_price=price,
                           stake=stake, shares=shares,
                           reason=f"{pick.reason} (за {d:g} дн.)",
                           placed_at=t.isoformat(timespec="seconds"),
                           end_date=t_end.isoformat(timespec="seconds"),
                           status=status, payout=payout, pnl=pnl,
                           settled_at=t_end.isoformat(timespec="seconds"))
                stats["bets"] += 1
        if stats["markets"] % 50 == 0:
            log(f"  … переглянуто {stats['markets']} ринків, ставок: {stats['bets']}")
    return stats
