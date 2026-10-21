"""Дані для веб-додатку: app-data.json (ставки, запуски, стан) і series.json
(історія ціни «Yes» для ринків, на які бот поставив)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .db import BetDB
from .strategies import ALL

APP_FILE = Path(__file__).with_name("webapp.html")

BET_COLS = ["id", "strategy", "market_id", "question", "url", "outcome", "outcome_index",
            "entry_price", "stake", "shares", "reason", "placed_at", "end_date",
            "status", "payout", "pnl", "settled_at"]


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


def build(db: BetDB, status: dict, links: list, *, demo: bool = False,
          generated: str | None = None) -> dict:
    bets = [{c: r[c] for c in BET_COLS}
            for r in db.conn.execute(
                f"SELECT {','.join(BET_COLS)} FROM bets WHERE mode='paper' ORDER BY placed_at")]
    runs = [dict(r) for r in db.conn.execute(
        "SELECT ts, ok, markets, new_bets, settled, message FROM runs ORDER BY ts DESC LIMIT 3000")]
    runs.reverse()
    st = {k: status.get(k) for k in ("snapshots", "markets_seen", "first_snapshot",
                                     "last_snapshot", "cycles", "failed_cycles")}
    return {
        "generated": generated or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "demo": demo,
        "strategies": [{"name": s.name, "description": s.description} for s in ALL],
        "status": st,
        "runs": runs,
        "bets": bets,
        "links": links,
    }


def bet_market_ids(db: BetDB) -> list[str]:
    return [r[0] for r in db.conn.execute(
        "SELECT DISTINCT market_id FROM bets WHERE mode='paper'")]


def series_from_db(db: BetDB) -> dict:
    """Сервер: уся історія цін з таблиці snapshots."""
    ids = bet_market_ids(db)
    out: dict[str, list] = {i: [] for i in ids}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for ts, mid, p in db.conn.execute(
                f"SELECT ts, market_id, price_yes FROM snapshots WHERE market_id IN "
                f"({','.join('?' * len(chunk))}) AND price_yes IS NOT NULL ORDER BY ts", chunk):
            out[mid].append([_epoch(ts), round(p, 4)])
    return out


def series_update(path: str, db: BetDB, run_ts: str | None) -> dict:
    """GitHub: докладаємо точки поточного запуску до вже збереженого series.json."""
    series: dict[str, list] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                series = json.load(f)
        except (OSError, ValueError):
            series = {}
    ids = set(bet_market_ids(db))
    for mid in ids:
        series.setdefault(mid, [])
    if run_ts:
        t = _epoch(run_ts)
        for mid, p in db.conn.execute(
                "SELECT market_id, price_yes FROM snapshots WHERE ts=? AND price_yes IS NOT NULL",
                (run_ts,)):
            if mid in ids:
                pts = series[mid]
                if not pts or pts[-1][0] < t:
                    pts.append([t, round(p, 4)])
    return series


def dump(obj, path: str):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def page(title: str = "Polysim") -> str:
    """Повна HTML-сторінка для GitHub Pages / сервера."""
    body = APP_FILE.read_text(encoding="utf-8")
    return ("<!doctype html>\n<html lang=\"uk\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">"
            "<meta name=\"robots\" content=\"noindex\"></head><body>\n" + body + "\n</body></html>")
