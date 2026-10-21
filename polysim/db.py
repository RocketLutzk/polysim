"""SQLite-журнал віртуальних ставок."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mode          TEXT NOT NULL,          -- 'paper' (наживо) або 'backtest'
    run_id        TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    market_id     TEXT NOT NULL,
    question      TEXT,
    url           TEXT,
    outcome       TEXT,
    outcome_index INTEGER,
    entry_price   REAL NOT NULL,
    stake         REAL NOT NULL,
    shares        REAL NOT NULL,
    reason        TEXT,
    placed_at     TEXT NOT NULL,
    end_date      TEXT,
    status        TEXT NOT NULL DEFAULT 'open',   -- open / won / lost / void
    payout        REAL,
    pnl           REAL,
    settled_at    TEXT,
    UNIQUE (mode, run_id, strategy, market_id)
);
-- «Фотографії» ринків кожні N хвилин: з них потім можна перевіряти
-- нові стратегії на власних даних, з реальними bid/ask.
CREATE TABLE IF NOT EXISTS snapshots (
    ts          TEXT NOT NULL,
    market_id   TEXT NOT NULL,
    price_yes   REAL,
    best_bid    REAL,
    best_ask    REAL,
    volume      REAL,
    liquidity   REAL,
    change_24h  REAL,
    PRIMARY KEY (ts, market_id)
);
CREATE TABLE IF NOT EXISTS markets (
    market_id   TEXT PRIMARY KEY,
    question    TEXT,
    url         TEXT,
    category    TEXT,
    outcomes    TEXT,
    end_date    TEXT,
    first_seen  TEXT,
    closed      INTEGER DEFAULT 0,
    winner      INTEGER
);
CREATE TABLE IF NOT EXISTS runs (
    ts          TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    markets     INTEGER,
    new_bets    INTEGER,
    settled     INTEGER,
    message     TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BetDB:
    def __init__(self, path: str = "bets.sqlite"):
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")  # веб-сторінка читає, поки бот пише
        self.conn.executescript(SCHEMA)

    def add_bet(self, **b) -> bool:
        """True — ставку записано; False — така вже є (одна ставка стратегії на ринок)."""
        cols = ", ".join(b)
        q = ", ".join("?" for _ in b)
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO bets ({cols}) VALUES ({q})", list(b.values()))
        self.conn.commit()
        return cur.rowcount == 1

    def open_bets(self, mode: str = "paper"):
        return self.conn.execute(
            "SELECT * FROM bets WHERE status='open' AND mode=?", (mode,)).fetchall()

    def settle(self, bet_id: int, status: str, payout: float, pnl: float,
               settled_at: str | None = None):
        self.conn.execute(
            "UPDATE bets SET status=?, payout=?, pnl=?, settled_at=? WHERE id=?",
            (status, payout, pnl, settled_at or now_iso(), bet_id))
        self.conn.commit()

    def bets(self, mode: str, run_id: str | None = None):
        if run_id:
            return self.conn.execute(
                "SELECT * FROM bets WHERE mode=? AND run_id=? ORDER BY placed_at",
                (mode, run_id)).fetchall()
        return self.conn.execute(
            "SELECT * FROM bets WHERE mode=? ORDER BY placed_at", (mode,)).fetchall()

    def add_snapshots(self, ts: str, markets) -> int:
        rows, meta = [], []
        for m in markets:
            rows.append((ts, m.id, m.prices[0] if m.prices else None, m.best_bid,
                         m.best_ask, m.volume, m.liquidity, m.one_day_change))
            meta.append((m.id, m.question, m.url, m.category,
                         ",".join(map(str, m.outcomes)),
                         m.end_date.isoformat() if m.end_date else None, ts))
        self.conn.executemany(
            "INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?,?,?)", rows)
        self.conn.executemany(
            "INSERT OR IGNORE INTO markets (market_id, question, url, category, outcomes,"
            " end_date, first_seen) VALUES (?,?,?,?,?,?,?)", meta)
        self.conn.commit()
        return len(rows)

    def log_run(self, ok: bool, markets=0, new_bets=0, settled=0, message=""):
        self.conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                          (now_iso(), int(ok), markets, new_bets, settled, message[:500]))
        self.conn.commit()

    def status(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        last = self.conn.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        return {
            "snapshots": q("SELECT COUNT(*) FROM snapshots"),
            "markets_seen": q("SELECT COUNT(*) FROM markets"),
            "first_snapshot": q("SELECT MIN(ts) FROM snapshots"),
            "last_snapshot": q("SELECT MAX(ts) FROM snapshots"),
            "cycles": q("SELECT COUNT(*) FROM runs"),
            "failed_cycles": q("SELECT COUNT(*) FROM runs WHERE ok=0"),
            "paper_bets": q("SELECT COUNT(*) FROM bets WHERE mode='paper'"),
            "paper_open": q("SELECT COUNT(*) FROM bets WHERE mode='paper' AND status='open'"),
            "last_run": dict(last) if last else None,
        }

    def last_run(self, mode: str) -> str | None:
        r = self.conn.execute(
            "SELECT run_id FROM bets WHERE mode=? ORDER BY id DESC LIMIT 1", (mode,)).fetchone()
        return r["run_id"] if r else None
