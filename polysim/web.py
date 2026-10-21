"""Мінімальний веб-сервер зі звітом. Захищений паролем (HTTP Basic Auth).

  POLYSIM_PASSWORD=секрет python -m polysim serve --port 8080

Сторінки:
  /            — звіт (статус бота, графік, ставки)
  /bets.csv    — усі ставки у CSV (відкривається в Excel)
  /data.sqlite — уся база (ставки + знімки цін) для аналізу
  /status.json — стан бота
"""
from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import os
import sqlite3
import tempfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import appdata
from .db import BetDB


SERVER_LINKS = [("/bets.csv", "Ставки (CSV для Excel)"),
                ("/data.sqlite", "Уся база (SQLite)"),
                ("/status.json", "status.json")]


def make_handler(db_path: str, password: str):
    want = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()

    class H(BaseHTTPRequestHandler):
        server_version = "polysim"

        def log_message(self, fmt, *args):  # тихіше в журналі
            pass

        def _auth(self) -> bool:
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got, want):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="polysim"')
            self.end_headers()
            return False

        def _send(self, body: bytes, ctype: str, name: str | None = None):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if name:
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if not self._auth():
                return
            path = self.path.split("?")[0]
            db = BetDB(db_path)
            try:
                if path in ("/", "/index.html"):
                    self._send(appdata.page().encode(), "text/html; charset=utf-8")
                elif path == "/app-data.json":
                    links = [[u.lstrip("/"), t] for u, t in SERVER_LINKS]
                    body = json.dumps(appdata.build(db, db.status(), links), ensure_ascii=False)
                    self._send(body.encode(), "application/json; charset=utf-8")
                elif path == "/series.json":
                    body = json.dumps(appdata.series_from_db(db), separators=(",", ":"))
                    self._send(body.encode(), "application/json")
                elif path == "/status.json":
                    self._send(json.dumps(db.status(), ensure_ascii=False, indent=2).encode(),
                               "application/json; charset=utf-8")
                elif path == "/bets.csv":
                    rows = db.bets("paper")
                    buf = io.StringIO()
                    w = csv.writer(buf)
                    if rows:
                        w.writerow(rows[0].keys())
                        w.writerows([tuple(r) for r in rows])
                    # BOM — щоб Excel правильно показав кирилицю
                    self._send(("﻿" + buf.getvalue()).encode(), "text/csv; charset=utf-8",
                               "polysim_bets.csv")
                elif path == "/data.sqlite":
                    with tempfile.TemporaryDirectory() as d:
                        out = os.path.join(d, "copy.sqlite")
                        dst = sqlite3.connect(out)
                        db.conn.backup(dst)  # цілісна копія, навіть поки бот пише
                        dst.close()
                        with open(out, "rb") as f:
                            body = f.read()
                    self._send(body, "application/octet-stream",
                               f"polysim_{datetime.now():%Y%m%d_%H%M}.sqlite")
                else:
                    self.send_error(404)
            finally:
                db.conn.close()

    return H


def serve(db_path: str, host: str, port: int, password: str):
    if not password or len(password) < 8:
        raise SystemExit("Задай пароль від 8 символів: POLYSIM_PASSWORD=... "
                         "(логін завжди admin)")
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path, password))
    print(f"Звіт: http://{host}:{port}/  (логін admin)")
    httpd.serve_forever()
