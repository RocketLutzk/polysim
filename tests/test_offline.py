"""Офлайн-тести: підміняємо API синтетичними даними, перевіряємо всю логіку.
Запуск:  python -m unittest discover tests
"""
import json
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from polysim import engine, report
from polysim.api import PolyClient, price_at
from polysim.db import BetDB
from polysim.models import Market
from polysim.strategies import ALL, Favorite

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def mk(i, *, closed, end, prices, vol=50_000, bid=None, ask=None, change=None, closed_time=None):
    return {
        "id": str(i), "question": f"Market {i}?", "slug": f"m-{i}",
        "endDate": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "closedTime": closed_time.strftime("%Y-%m-%d %H:%M:%S+00") if closed_time else None,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps([str(p) for p in prices]),
        "clobTokenIds": json.dumps([f"tok{i}a", f"tok{i}b"]),
        "closed": closed, "active": True, "volumeNum": vol,
        "bestBid": bid, "bestAsk": ask, "oneDayPriceChange": change,
    }


class FakeClient(PolyClient):
    """Синтетичний «Polymarket»: ринок вирішується Yes з імовірністю,
    рівною його ціні (тобто ринок чесний — переваги не існує)."""

    def __init__(self, n_closed=400, seed=7):
        super().__init__(cache_dir=None, pause=0)
        rnd = random.Random(seed)
        self.closed, self.hist, self.open = [], {}, []
        for i in range(n_closed):
            end = NOW - timedelta(days=rnd.uniform(1, 90))
            # погодинна історія за 10 днів до кінця (випадкове блукання ціни)
            pts, p = [], rnd.uniform(0.03, 0.97)
            t0 = int((end - timedelta(days=10)).timestamp())
            for h in range(240):
                p = min(0.995, max(0.005, p + rnd.gauss(0, 0.006)))
                pts.append((t0 + h * 3600, round(p, 4)))
            self.hist[f"tok{i}a"] = pts
            # результат розігрується за фінальною ціною → ринок «чесний»
            yes = rnd.random() < p
            self.closed.append(mk(i, closed=True, end=end, prices=[1, 0] if yes else [0, 1]))
        # кілька неоднозначних (50/50) ринків — мають пропускатись
        self.closed.append(mk(9001, closed=True, end=NOW - timedelta(days=5), prices=[0.5, 0.5]))
        # відкриті ринки для scan
        self.open = [
            mk(5001, closed=False, end=NOW + timedelta(days=3), prices=[0.88, 0.12], bid=0.87, ask=0.89),
            mk(5002, closed=False, end=NOW + timedelta(days=10), prices=[0.08, 0.92], bid=0.07, ask=0.09),
            mk(5003, closed=False, end=NOW + timedelta(days=20), prices=[0.40, 0.60], change=0.07),
            mk(5004, closed=False, end=NOW + timedelta(days=3), prices=[0.88, 0.12], vol=500),  # мертвий
        ]

    def _get(self, url, params=None):
        params = params or {}
        if url.endswith("/markets"):
            src = self.closed if params.get("closed") == "true" else [d for d in self.open if not d["closed"]]
            off, lim = int(params.get("offset", 0)), int(params.get("limit", 500))
            return src[off:off + lim]
        if "/markets/" in url:
            mid = url.rsplit("/", 1)[1]
            for d in self.closed + self.open:
                if d["id"] == mid:
                    return d
        if url.endswith("/prices-history"):
            h = self.hist.get(params["market"], [])
            return {"history": [{"t": t, "p": p} for t, p in h
                                if params["startTs"] <= t <= params["endTs"]]}
        raise AssertionError(url)


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = BetDB(os.path.join(self.tmp.name, "b.sqlite"))

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def test_parse_and_winner(self):
        m = Market.from_api(mk(1, closed=True, end=NOW, prices=[0, 1]))
        self.assertEqual(m.outcomes, ["Yes", "No"])
        self.assertEqual(m.winner_index, 1)
        self.assertIsNone(Market.from_api(mk(2, closed=True, end=NOW, prices=[0.5, 0.5])).winner_index)
        self.assertIsNone(Market.from_api(mk(3, closed=False, end=NOW, prices=[0.99, 0.01])).winner_index)

    def test_bet_math(self):
        price, shares = engine.bet_math(10, 0.80, 0.0)
        self.assertAlmostEqual(shares, 12.5)
        self.assertEqual(engine.settle_amounts(10, shares, True), ("won", 12.5, 2.5))
        self.assertEqual(engine.settle_amounts(10, shares, False), ("lost", 0.0, -10))
        self.assertEqual(engine.settle_amounts(10, shares, None), ("void", 10, 0.0))
        _, s2 = engine.bet_math(10, 0.80, 0.02)
        self.assertAlmostEqual(s2, 12.25)

    def test_no_lookahead(self):
        h = [(100, 0.3), (200, 0.4), (300, 0.9)]
        self.assertEqual(price_at(h, 250), 0.4)
        self.assertIsNone(price_at(h, 50))

    def test_scan_and_settle(self):
        c = FakeClient()
        engine.datetime = _FrozenDT  # «зараз» = NOW
        try:
            n = engine.scan(c, self.db, ALL, stake=10, fee=0, slippage=0,
                            max_markets=100, log=lambda *_: None)
        finally:
            engine.datetime = datetime
        rows = self.db.bets("paper")
        got = {(r["strategy"], r["market_id"]) for r in rows}
        self.assertIn(("favorite", "5001"), got)
        self.assertIn(("longshot", "5002"), got)
        self.assertIn(("momentum", "5003"), got)
        self.assertNotIn(("favorite", "5004"), got)          # низький обсяг
        fav = [r for r in rows if r["strategy"] == "favorite"][0]
        self.assertAlmostEqual(fav["entry_price"], 0.89)      # купуємо по ask
        # повторний scan не дублює
        engine.datetime = _FrozenDT
        try:
            self.assertEqual(engine.scan(c, self.db, ALL, stake=10, fee=0, slippage=0,
                                         max_markets=100, log=lambda *_: None), 0)
        finally:
            engine.datetime = datetime
        self.assertGreater(n, 0)
        # ринок 5001 закрився на Yes
        c.open[0].update(closed=True, outcomePrices=json.dumps(["1", "0"]))
        engine.settle_open(c, self.db, log=lambda *_: None)
        fav = [r for r in self.db.bets("paper") if r["market_id"] == "5001"][0]
        self.assertEqual(fav["status"], "won")
        self.assertAlmostEqual(fav["pnl"], 10 / 0.89 - 10, places=6)

    def test_backtest_fair_market_has_no_edge(self):
        c = FakeClient(n_closed=1500)
        st = engine.backtest(c, self.db, ALL, start=NOW - timedelta(days=120), end=NOW,
                             days_before=[7, 3, 1], stake=10, fee=0, slippage=0.0,
                             max_markets=5000, min_volume=0, run_id="t", log=lambda *_: None)
        self.assertGreater(st["bets"], 300)
        self.assertGreaterEqual(st["skipped"], 1)  # 50/50 ринок пропущено
        stats = {s["strategy"]: s for s in report.summarize(self.db.bets("backtest", "t"))}
        # на «чесному» ринку жодна стратегія не повинна мати статистично значущої переваги
        for s in stats.values():
            self.assertLess(abs(s["z"]), 3.5, s)
            self.assertAlmostEqual(s["win_rate"], s["implied"], delta=0.12)
        # одна ставка на стратегію на ринок
        rows = self.db.bets("backtest", "t")
        self.assertEqual(len(rows), len({(r["strategy"], r["market_id"]) for r in rows}))
        # HTML будується
        out = os.path.join(self.tmp.name, "r.html")
        report.write_html(rows, list(stats.values()), out, "T", "S")
        self.assertIn("const DATA", open(out, encoding="utf-8").read())


class _FrozenDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


if __name__ == "__main__":
    unittest.main()


class TServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "b.sqlite")
        self.db = BetDB(self.path)
        engine.datetime = _FrozenDT

    def tearDown(self):
        engine.datetime = datetime
        self.db.conn.close()
        self.tmp.cleanup()

    def test_cycle_collects_and_survives_errors(self):
        c = FakeClient(n_closed=10)
        res = engine.cycle(c, self.db, ALL, stake=10, fee=0, slippage=0,
                           max_markets=100, log=lambda *_: None)
        self.assertEqual(res["markets"], 3)            # 4 відкритих, 1 з малим обсягом
        st = self.db.status()
        self.assertEqual(st["snapshots"], 3)
        self.assertEqual(st["cycles"], 1)
        # API «впав» — цикл не кидає виняток, а пише помилку в журнал
        class Broken(FakeClient):
            def _get(self, url, params=None):
                raise RuntimeError("мережа впала")
        res = engine.cycle(Broken(n_closed=1), self.db, ALL, stake=10, fee=0, slippage=0,
                           max_markets=100, log=lambda *_: None)
        self.assertIn("error", res)
        st = self.db.status()
        self.assertEqual(st["failed_cycles"], 1)
        self.assertEqual(st["last_run"]["ok"], 0)

    def test_web_auth_and_pages(self):
        import threading, urllib.request, urllib.error, base64
        from http.server import ThreadingHTTPServer
        from polysim.web import make_handler
        engine.cycle(FakeClient(n_closed=5), self.db, ALL, stake=10, fee=0, slippage=0,
                     max_markets=100, log=lambda *_: None)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.path, "secret-pass"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/")
            self.assertEqual(e.exception.code, 401)
            hdr = {"Authorization": "Basic " + base64.b64encode(b"admin:secret-pass").decode()}
            get = lambda p: urllib.request.urlopen(urllib.request.Request(base + p, headers=hdr)).read()
            page = get("/").decode()
            self.assertIn("<title>Polysim</title>", page)
            app = json.loads(get("/app-data.json"))
            self.assertEqual(len(app["bets"]), len(self.db.bets("paper")))
            self.assertEqual(app["status"]["snapshots"], 3)
            series = json.loads(get("/series.json"))
            self.assertEqual(set(series), {b["market_id"] for b in app["bets"]})
            self.assertTrue(get("/bets.csv").decode("utf-8-sig").startswith("id,"))
            self.assertTrue(get("/data.sqlite").startswith(b"SQLite format 3"))
            self.assertEqual(json.loads(get("/status.json"))["snapshots"], 3)
        finally:
            srv.shutdown()
            srv.server_close()


class TGitHub(unittest.TestCase):
    """Імітуємо кілька запусків GitHub Actions: кожен стартує з «чистого» стану,
    маючи лише файли з папки data/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "data")
        self.site = os.path.join(self.tmp.name, "docs")
        engine.datetime = _FrozenDT

    def tearDown(self):
        engine.datetime = datetime
        self.tmp.cleanup()

    def _run(self, client):
        from polysim import gitstore
        return gitstore.gh_cycle(client, ALL, data_dir=self.data, site_dir=self.site,
                                 stake=10, fee=0, slippage=0, max_markets=100,
                                 work_db=os.path.join(self.data, ".work.sqlite"),
                                 log=lambda *_: None)

    def test_runs_persist_through_files(self):
        import glob, time
        from polysim import gitstore
        c = FakeClient(n_closed=5)
        self._run(c)
        bets1 = open(os.path.join(self.data, "bets.csv"), encoding="utf-8").read()
        time.sleep(1.1)                     # інший час знімка → новий файл
        self._run(c)                        # нічого не дублюється
        self.assertEqual(open(os.path.join(self.data, "bets.csv"), encoding="utf-8").read(), bets1)
        # ринок закрився між запусками → наступний запуск розраховує ставки
        c.open[0].update(closed=True, outcomePrices=json.dumps(["1", "0"]))
        time.sleep(1.1)
        self._run(c)
        db = BetDB(os.path.join(self.tmp.name, "check.sqlite"))
        gitstore.load(db, self.data)
        fav = [r for r in db.bets("paper") if r["market_id"] == "5001" and r["strategy"] == "favorite"][0]
        self.assertEqual(fav["status"], "won")
        self.assertAlmostEqual(fav["pnl"], 10 / 0.89 - 10, places=6)
        self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 3)
        db.conn.close()
        snaps = glob.glob(os.path.join(self.data, "snapshots", "*", "*.csv.gz"))
        self.assertEqual(len(snaps), 3)
        # звіт для GitHub Pages
        page = open(os.path.join(self.site, "index.html"), encoding="utf-8").read()
        self.assertIn("<title>Polysim</title>", page)
        self.assertIn("noindex", page)
        app = json.load(open(os.path.join(self.site, "app-data.json"), encoding="utf-8"))
        self.assertEqual(len(app["runs"]), 3)
        self.assertEqual(app["status"]["snapshots"], 8)
        series = json.load(open(os.path.join(self.site, "series.json"), encoding="utf-8"))
        self.assertEqual(len(series["5002"]), 3)   # точка з кожного запуску
        self.assertEqual(len(series["5001"]), 2)   # закрився перед третім
        self.assertTrue(os.path.exists(os.path.join(self.site, "bets.csv")))
        self.assertTrue(os.path.exists(os.path.join(self.site, ".nojekyll")))
        # одна база з усіма даними
        st = gitstore.import_all(self.data, os.path.join(self.tmp.name, "all.sqlite"))
        self.assertEqual(st["snapshot_rows"], 3 + 3 + 2)   # 5001 закрився перед третім

    def test_failed_run_is_recorded(self):
        class Broken(FakeClient):
            def _get(self, url, params=None):
                raise RuntimeError("API недоступний")
        self._run(Broken(n_closed=1))
        runs = open(os.path.join(self.data, "runs.csv"), encoding="utf-8").read()
        self.assertIn("API недоступний", runs)
        app = json.load(open(os.path.join(self.site, "app-data.json"), encoding="utf-8"))
        self.assertEqual(app["runs"][-1]["ok"], 0)
