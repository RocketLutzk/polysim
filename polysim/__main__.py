"""Polysim — віртуальна торгівля на даних Polymarket (без реальних грошей).

  python -m polysim backtest --from 2026-06-01 --to 2026-09-30
  python -m polysim scan            # записати віртуальні ставки зараз
  python -m polysim settle          # розрахувати ставки на закритих ринках
  python -m polysim run             # збір даних + ставки по колу (для сервера)
  python -m polysim serve           # веб-сторінка зі звітом (пароль)
  python -m polysim check           # чи доступні дані Polymarket
  python -m polysim report          # підсумки + report.html
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import engine, report
from .api import ApiError, PolyClient
from .db import BetDB
from .strategies import ALL, by_names


def _date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="polysim", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="bets.sqlite", help="файл бази ставок")
    p.add_argument("--strategies", help="через кому, напр. favorite,random (за замовч. усі)")
    p.add_argument("--stake", type=float, default=10.0, help="розмір віртуальної ставки, $")
    p.add_argument("--fee", type=float, default=0.0, help="комісія як частка ставки, напр. 0.01")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("strategies", help="список стратегій")

    sc = sub.add_parser("scan", help="записати віртуальні ставки на відкритих ринках")
    sc.add_argument("--max-markets", type=int, default=10000)
    sc.add_argument("--slippage", type=float, default=0.0)

    sub.add_parser("settle", help="розрахувати ставки, чиї ринки закрились")

    sub.add_parser("check", help="перевірити доступ до API Polymarket")

    gh = sub.add_parser("gh-cycle", help="один цикл для GitHub Actions (дані у файлах)")
    gh.add_argument("--data", default="data")
    gh.add_argument("--site", default="docs")
    gh.add_argument("--max-markets", type=int, default=10000)
    gh.add_argument("--slippage", type=float, default=0.0)

    gi = sub.add_parser("gh-import", help="зібрати всі дані з data/ в одну базу SQLite")
    gi.add_argument("--data", default="data")
    gi.add_argument("--out", default="polysim_all.sqlite")

    sv = sub.add_parser("serve", help="веб-сторінка зі звітом")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)

    rn = sub.add_parser("run", help="збір даних + віртуальні ставки по колу")
    rn.add_argument("--every", type=int, default=15, help="хвилин між циклами")
    rn.add_argument("--max-markets", type=int, default=10000)
    rn.add_argument("--slippage", type=float, default=0.0)

    bt = sub.add_parser("backtest", help="перевірка на закритих ринках з минулого")
    today = datetime.now(timezone.utc).date()
    bt.add_argument("--from", dest="start", type=_date, default=_date(str(today - timedelta(days=120))))
    bt.add_argument("--to", dest="end", type=_date, default=_date(str(today - timedelta(days=1))))
    bt.add_argument("--days-before", default="1,3,7",
                    help="за скільки днів до закриття «бот дивиться» на ринок")
    bt.add_argument("--max-markets", type=int, default=800)
    bt.add_argument("--min-volume", type=float, default=10000)
    bt.add_argument("--slippage", type=float, default=0.01,
                    help="штраф до ціни входу (спред), за замовч. 1 цент")

    rp = sub.add_parser("report", help="підсумки + HTML")
    rp.add_argument("--mode", choices=["paper", "backtest"], default=None)
    rp.add_argument("--out", default="report.html")
    rp.add_argument("--no-open", action="store_true")

    a = p.parse_args(argv)
    db = None if a.cmd in ("gh-cycle", "gh-import", "strategies", "check") else BetDB(a.db)
    client = PolyClient()
    strategies = by_names(a.strategies)

    try:
        if a.cmd == "strategies":
            for s in ALL:
                print(f"  {s.name:<10} {s.description}")

        elif a.cmd == "scan":
            n = engine.scan(client, db, strategies, stake=a.stake, fee=a.fee,
                            slippage=a.slippage, max_markets=a.max_markets)
            print(f"Нових віртуальних ставок: {n}")

        elif a.cmd == "settle":
            n = engine.settle_open(client, db)
            print(f"Розраховано ставок: {n}")
            _report(db, "paper", "report.html", open_browser=False)

        elif a.cmd == "gh-cycle":
            from . import gitstore
            client.cache = None
            res = gitstore.gh_cycle(client, strategies, data_dir=a.data, site_dir=a.site,
                                    stake=a.stake, fee=a.fee, slippage=a.slippage,
                                    max_markets=a.max_markets,
                                    work_db=os.path.join(a.data, ".work.sqlite"))
            # помилка мережі не «валить» запуск: вона видна на сторінці звіту
            return 0

        elif a.cmd == "gh-import":
            from . import gitstore
            st = gitstore.import_all(a.data, a.out)
            print(f"Готово: {a.out}\n  знімків цін: {st['snapshot_rows']}, ринків: {st['markets_seen']}, "
                  f"ставок: {st['paper_bets']}")

        elif a.cmd == "check":
            return _check(client)

        elif a.cmd == "serve":
            from .web import serve
            serve(a.db, a.host, a.port, os.environ.get("POLYSIM_PASSWORD", ""))

        elif a.cmd == "run":
            log = lambda m: print(m, flush=True)  # noqa: E731
            while True:
                started = time.time()
                log(f"[{datetime.now():%Y-%m-%d %H:%M}] цикл")
                engine.cycle(client, db, strategies, stake=a.stake, fee=a.fee,
                             slippage=a.slippage, max_markets=a.max_markets, log=log)
                time.sleep(max(30, a.every * 60 - (time.time() - started)))

        elif a.cmd == "backtest":
            run_id = datetime.now().strftime("bt-%Y%m%d-%H%M%S")
            days = [float(x) for x in a.days_before.split(",") if x.strip()]
            print(f"Бектест {a.start:%Y-%m-%d} → {a.end:%Y-%m-%d}, точки: {days} дн. до кінця")
            st = engine.backtest(client, db, strategies, start=a.start, end=a.end,
                                 days_before=sorted(days, reverse=True), stake=a.stake,
                                 fee=a.fee, slippage=a.slippage, max_markets=a.max_markets,
                                 min_volume=a.min_volume, run_id=run_id)
            print(f"Ринків: {st['markets']}, пропущено: {st['skipped']}, ставок: {st['bets']}")
            _report(db, "backtest", "report.html", run_id=run_id)

        elif a.cmd == "report":
            mode = a.mode or ("paper" if db.bets("paper") else "backtest")
            _report(db, mode, a.out, open_browser=not a.no_open,
                    run_id=db.last_run("backtest") if mode == "backtest" else None)

    except ApiError as e:
        print(f"\nПомилка API Polymarket: {e}\n"
              "Перевір, чи відкривається gamma-api.polymarket.com з цього комп'ютера/сервера.")
        return 2
    except KeyboardInterrupt:
        print("\nЗупинено.")
    return 0


def _check(client) -> int:
    ok = True
    try:
        ms = list(client.iter_markets(closed=False, max_markets=3))
        print(f"✓ gamma-api: працює, приклад ринку: {ms[0].question[:60] if ms else '—'}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"✗ gamma-api недоступний: {e}")
    try:
        client._get(f"{client.clob}/time")
        print("✓ clob (історія цін): працює")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"✗ clob недоступний: {e}")
    print("Готово до роботи." if ok else "Дані Polymarket з цього сервера недоступні.")
    return 0 if ok else 2


def _report(db, mode, out, open_browser=True, run_id=None, quiet=False):
    rows = db.bets(mode, run_id)
    stats = report.summarize(rows)
    title = "Бектест стратегій Polymarket" if mode == "backtest" else "Віртуальна торгівля Polymarket"
    sub = (f"Прогін {run_id}" if run_id else "Ставки з живих даних") + \
          f" · згенеровано {datetime.now():%Y-%m-%d %H:%M} · гроші віртуальні"
    if not quiet:
        report.print_table(stats, title)
    report.write_html(rows, stats, out, title, sub)
    if not quiet:
        print(f"HTML-звіт: {Path(out).resolve()}")
    if open_browser:
        try:
            webbrowser.open(Path(out).resolve().as_uri())
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
