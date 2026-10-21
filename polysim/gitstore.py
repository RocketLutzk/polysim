"""Режим GitHub Actions: кожен запуск — один цикл, а дані живуть у файлах репозиторію.

  data/bets.csv                         — усі віртуальні ставки
  data/markets.csv                      — довідник ринків
  data/runs.csv                         — журнал запусків
  data/snapshots/2026-10-21/091500.csv.gz — знімок цін одного запуску (новий файл щоразу,
                                          старі не переписуються — git так зберігає компактно)
  docs/index.html, docs/bets.csv        — звіт для GitHub Pages
"""
from __future__ import annotations

import csv
import glob
import gzip
import os
import shutil
from datetime import datetime, timezone

from . import appdata, engine
from .api import PolyClient
from .db import BetDB

TABLES = {"bets": "bets.csv", "markets": "markets.csv", "runs": "runs.csv"}
SNAP_COLS = ["ts", "market_id", "price_yes", "best_bid", "best_ask", "volume",
             "liquidity", "change_24h"]


def _cols(db: BetDB, table: str) -> list[str]:
    return [r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")]


def _num(v: str):
    if v == "":
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in v and "e" not in v.lower() else f
    except ValueError:
        return v


def load(db: BetDB, data_dir: str):
    for table, fname in TABLES.items():
        path = os.path.join(data_dir, fname)
        if not os.path.exists(path):
            continue
        cols = _cols(db, table)
        with open(path, encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            keep = [c for c in rd.fieldnames or [] if c in cols]
            rows = []
            for r in rd:
                vals = []
                for c in keep:
                    v = r[c]
                    # текстові колонки лишаємо текстом (питання, id ринку тощо)
                    vals.append(v if c in ("market_id", "question", "url", "outcome",
                                           "reason", "strategy", "mode", "run_id",
                                           "status", "placed_at", "end_date",
                                           "settled_at", "ts", "message", "category",
                                           "outcomes", "first_seen")
                                else _num(v))
                rows.append(vals)
        if rows:
            db.conn.executemany(
                f"INSERT OR IGNORE INTO {table} ({','.join(keep)}) VALUES ({','.join('?' * len(keep))})",
                rows)
    db.conn.commit()


def _write_table(db: BetDB, table: str, path: str, order: str):
    cols = _cols(db, table)
    rows = db.conn.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY {order}").fetchall()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    os.replace(tmp, path)


def save(db: BetDB, data_dir: str, run_ts: str | None):
    os.makedirs(os.path.join(data_dir, "snapshots"), exist_ok=True)
    _write_table(db, "bets", os.path.join(data_dir, "bets.csv"), "id")
    _write_table(db, "markets", os.path.join(data_dir, "markets.csv"), "first_seen, market_id")
    _write_table(db, "runs", os.path.join(data_dir, "runs.csv"), "ts")
    if not run_ts:  # цикл не вдався — знімка немає
        return
    rows = db.conn.execute(
        f"SELECT {','.join(SNAP_COLS)} FROM snapshots WHERE ts=?", (run_ts,)).fetchall()
    if rows:
        dt = datetime.fromisoformat(run_ts)
        d = os.path.join(data_dir, "snapshots", dt.strftime("%Y-%m-%d"))
        os.makedirs(d, exist_ok=True)
        with gzip.open(os.path.join(d, dt.strftime("%H%M%S") + ".csv.gz"), "wt",
                       encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(SNAP_COLS)
            for r in rows:
                w.writerow(["" if v is None else v for v in r])


def gh_status(db: BetDB, data_dir: str) -> dict:
    st = db.status()
    files = sorted(glob.glob(os.path.join(data_dir, "snapshots", "*", "*.csv.gz")))
    ok = db.conn.execute("SELECT COALESCE(SUM(markets),0), MIN(ts), MAX(ts) "
                         "FROM runs WHERE ok=1 AND markets>0").fetchone()
    st.update(snapshots=int(ok[0]), first_snapshot=ok[1], last_snapshot=ok[2],
              snapshot_files=len(files))
    return st


def build_site(db: BetDB, data_dir: str, site_dir: str, run_ts: str | None = None):
    """docs/ для GitHub Pages: веб-додаток + його дані."""
    os.makedirs(site_dir, exist_ok=True)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    links = [["bets.csv", "Ставки (CSV для Excel)"]]
    if repo:
        links += [[f"https://github.com/{repo}/tree/main/{data_dir}", "Усі зібрані дані"],
                  [f"https://github.com/{repo}/actions", "Журнал запусків"],
                  [f"https://github.com/{repo}/archive/refs/heads/main.zip", "Завантажити все (ZIP)"]]
    appdata.dump(appdata.build(db, gh_status(db, data_dir), links),
                 os.path.join(site_dir, "app-data.json"))
    series_path = os.path.join(site_dir, "series.json")
    appdata.dump(appdata.series_update(series_path, db, run_ts), series_path)
    with open(os.path.join(site_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(appdata.page())
    shutil.copyfile(os.path.join(data_dir, "bets.csv"), os.path.join(site_dir, "bets.csv"))
    open(os.path.join(site_dir, ".nojekyll"), "w").close()


def gh_cycle(client: PolyClient, strategies, *, data_dir: str, site_dir: str,
             stake: float, fee: float, slippage: float, max_markets: int,
             work_db: str, log=print) -> dict:
    os.makedirs(data_dir, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(work_db + suffix):
            os.remove(work_db + suffix)
    db = BetDB(work_db)
    try:
        load(db, data_dir)
        res = engine.cycle(client, db, strategies, stake=stake, fee=fee, slippage=slippage,
                           max_markets=max_markets, log=log)
        run_ts = db.conn.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0]
        save(db, data_dir, run_ts)
        build_site(db, data_dir, site_dir, run_ts)
        return res
    finally:
        db.conn.close()


def import_all(data_dir: str, out_db: str) -> dict:
    """Збирає ВСІ дані з папки data/ в одну SQLite-базу для аналізу."""
    if os.path.exists(out_db):
        os.remove(out_db)
    db = BetDB(out_db)
    load(db, data_dir)
    n = 0
    for path in sorted(glob.glob(os.path.join(data_dir, "snapshots", "*", "*.csv.gz"))):
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            rd = csv.reader(f)
            next(rd, None)
            rows = [[r[0], r[1]] + [_num(v) for v in r[2:]] for r in rd]
        db.conn.executemany(
            f"INSERT OR IGNORE INTO snapshots VALUES ({','.join('?' * len(SNAP_COLS))})", rows)
        n += len(rows)
    db.conn.commit()
    st = db.status()
    db.conn.close()
    return {"snapshot_rows": n, **st}
