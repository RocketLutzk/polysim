"""Підсумки: таблиця в консолі + HTML-звіт з графіком прибутку."""
from __future__ import annotations

import html
import json
import math
from collections import defaultdict

LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]


def summarize(rows) -> list[dict]:
    by = defaultdict(list)
    for r in rows:
        by[r["strategy"]].append(r)
    out = []
    for name, rs in by.items():
        settled = [r for r in rs if r["status"] in ("won", "lost")]
        open_ = [r for r in rs if r["status"] == "open"]
        wins = sum(r["status"] == "won" for r in settled)
        staked = sum(r["stake"] for r in settled)
        pnl = sum(r["pnl"] for r in settled)
        # Ціна входу = ймовірність, яку закладав ринок. Порівнюємо з фактом.
        exp_wins = sum(r["entry_price"] for r in settled)
        var = sum(r["entry_price"] * (1 - r["entry_price"]) for r in settled)
        z = (wins - exp_wins) / math.sqrt(var) if var > 0 else 0.0
        eq, peak, dd = 0.0, 0.0, 0.0
        for r in sorted(settled, key=lambda r: r["settled_at"] or ""):
            eq += r["pnl"]
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        n = len(settled)
        out.append({
            "strategy": name, "bets": n, "open": len(open_),
            "void": sum(r["status"] == "void" for r in rs),
            "wins": wins, "win_rate": wins / n if n else 0.0,
            "implied": exp_wins / n if n else 0.0,
            "staked": staked, "pnl": pnl, "roi": pnl / staked if staked else 0.0,
            "max_dd": dd, "z": z,
        })
    out.sort(key=lambda s: s["pnl"], reverse=True)
    return out


def verdict(s: dict) -> str:
    if s["bets"] < 30:
        return "замало даних"
    if abs(s["z"]) < 2:
        return "схоже на випадковість"
    return "є перевага" if s["z"] > 0 else "стабільно програє"


def print_table(stats: list[dict], title: str):
    print(f"\n{title}")
    hdr = f"{'стратегія':<10} {'ставок':>6} {'відкр':>5} {'виграш%':>8} {'ринок%':>7} {'P&L $':>9} {'ROI':>7} {'просад.$':>8}  висновок"
    print(hdr)
    print("-" * len(hdr))
    for s in stats:
        print(f"{s['strategy']:<10} {s['bets']:>6} {s['open']:>5} {s['win_rate']*100:>7.1f}% "
              f"{s['implied']*100:>6.1f}% {s['pnl']:>+9.2f} {s['roi']*100:>+6.1f}% "
              f"{s['max_dd']:>8.2f}  {verdict(s)}")
    if not stats:
        print("(ставок поки немає)")
    print("\nвиграш% — як часто ставка заходила; ринок% — як часто мала б заходити за цінами.")
    print("Перевага є лише тоді, коли виграш% стабільно вищий за ринок% на 30+ ставках.\n")


def _curves(rows):
    by = defaultdict(list)
    for r in sorted((r for r in rows if r["status"] in ("won", "lost")),
                    key=lambda r: r["settled_at"] or ""):
        prev = by[r["strategy"]][-1][1] if by[r["strategy"]] else 0.0
        by[r["strategy"]].append([r["settled_at"][:10], round(prev + r["pnl"], 2)])
    return by


def render_html(rows, stats: list[dict], title: str, subtitle: str, extra_html: str = "") -> str:
    names = [s["strategy"] for s in sorted(stats, key=lambda s: s["strategy"])]
    color_idx = {n: i % 8 for i, n in enumerate(names)}  # колір за назвою, не за місцем
    curves = _curves(rows)
    payload = {
        "series": [{"name": n, "c": color_idx[n], "pts": curves.get(n, [])} for n in names],
    }
    css_vars_light = "\n".join(f"  --s{i}: {c};" for i, c in enumerate(LIGHT))
    css_vars_dark = "\n".join(f"  --s{i}: {c};" for i, c in enumerate(DARK))

    def tr(s):
        return (f"<tr><td><span class='sw' style='background:var(--s{color_idx[s['strategy']]})'></span>"
                f"{html.escape(s['strategy'])}</td><td>{s['bets']}</td><td>{s['open']}</td>"
                f"<td>{s['win_rate']*100:.1f}%</td><td>{s['implied']*100:.1f}%</td>"
                f"<td class='num'>{s['pnl']:+.2f}</td><td class='num'>{s['roi']*100:+.1f}%</td>"
                f"<td>{s['max_dd']:.2f}</td><td>{verdict(s)}</td></tr>")

    recent = sorted(rows, key=lambda r: r["placed_at"], reverse=True)[:60]

    def br(r):
        pnl = "" if r["pnl"] is None else f"{r['pnl']:+.2f}"
        q = html.escape(r["question"] or "")
        q = f"<a href='{html.escape(r['url'])}' target='_blank'>{q}</a>" if r["url"] else q
        return (f"<tr><td>{r['placed_at'][:10]}</td><td>{html.escape(r['strategy'])}</td>"
                f"<td class='q'>{q}</td><td>{html.escape(r['outcome'] or '')}</td>"
                f"<td>{r['entry_price']:.3f}</td><td>{r['status']}</td><td class='num'>{pnl}</td></tr>")

    doc = TEMPLATE.replace("{{TITLE}}", html.escape(title)) \
        .replace("{{SUB}}", html.escape(subtitle)) \
        .replace("{{LIGHT}}", css_vars_light).replace("{{DARK}}", css_vars_dark) \
        .replace("{{SUMMARY}}", "".join(tr(s) for s in stats) or "<tr><td colspan=9>Ставок поки немає</td></tr>") \
        .replace("{{RECENT}}", "".join(br(r) for r in recent)) \
        .replace("{{DATA}}", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")) \
        .replace("{{EXTRA}}", extra_html)
    return doc


def write_html(rows, stats: list[dict], path: str, title: str, subtitle: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html(rows, stats, title, subtitle))


TEMPLATE = """<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITLE}}</title>
<style>
:root {
  color-scheme: light;
  --bg: #fcfcfb; --card: #ffffff; --line: #e4e3df; --grid: #eeede9;
  --t1: #0b0b0b; --t2: #52514e; --t3: #7a7974;
{{LIGHT}}
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #1a1a19; --card: #222220; --line: #393936; --grid: #2c2c2a;
    --t1: #f0efec; --t2: #c3c2b7; --t3: #93928a;
{{DARK}}
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #1a1a19; --card: #222220; --line: #393936; --grid: #2c2c2a;
  --t1: #f0efec; --t2: #c3c2b7; --t3: #93928a;
{{DARK}}
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--t1);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 32px 0 10px; }
.sub { color: var(--t2); margin: 0 0 20px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 8px; color: var(--t2); font-size: 13px; }
.sw { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
.chart { position: relative; }
svg { display: block; width: 100%; height: auto; overflow: visible; }
.tip { position: absolute; pointer-events: none; background: var(--card); border: 1px solid var(--line);
  border-radius: 8px; padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 14px rgba(0,0,0,.12);
  display: none; white-space: nowrap; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
td:first-child { white-space: nowrap; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); }
th { color: var(--t2); font-weight: 600; }
td.num { font-variant-numeric: tabular-nums; }
td.q { max-width: 380px; }
a { color: inherit; }
.note { color: var(--t2); font-size: 13px; margin-top: 10px; }
.status { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 16px; }
.status .k { color: var(--t2); font-size: 12px; }
.status .v { font-size: 18px; font-weight: 600; font-variant-numeric: tabular-nums; }
.links { display: flex; flex-wrap: wrap; gap: 8px 16px; margin-top: 12px; font-size: 13px; }
</style></head>
<body><main>
<h1>{{TITLE}}</h1>
<p class="sub">{{SUB}}</p>
{{EXTRA}}

<div class="card">
  <div class="legend" id="legend"></div>
  <div class="chart" id="chart"></div>
  <p class="note">Накопичений віртуальний прибуток, $, після кожної розрахованої ставки.</p>
</div>

<h2>Підсумок по стратегіях</h2>
<div class="card scroll"><table>
<tr><th>Стратегія</th><th>Ставок</th><th>Відкр.</th><th>Виграш</th><th>Ринок очікував</th><th>P&amp;L $</th><th>ROI</th><th>Макс. просідання $</th><th>Висновок</th></tr>
{{SUMMARY}}
</table>
<p class="note">«Ринок очікував» — середня ціна входу, тобто ймовірність, яку закладав ринок. Стратегія має перевагу лише тоді, коли фактичний «Виграш» стабільно вищий за це число на 30+ ставках.</p>
</div>

<h2>Останні ставки</h2>
<div class="card scroll"><table>
<tr><th>Дата</th><th>Стратегія</th><th>Ринок</th><th>Ставка на</th><th>Ціна</th><th>Статус</th><th>P&amp;L $</th></tr>
{{RECENT}}
</table></div>
</main>
<script>
const DATA = {{DATA}};
function draw() {
  const box = document.getElementById('chart'), legend = document.getElementById('legend');
  const series = DATA.series.filter(s => s.pts.length);
  legend.innerHTML = '';
  DATA.series.forEach(s => {
    const el = document.createElement('span');
    el.innerHTML = `<span class="sw" style="background:var(--s${s.c})"></span>${s.name}`;
    legend.appendChild(el);
  });
  if (!series.length) { box.innerHTML = '<p class="note">Ще немає розрахованих ставок.</p>'; return; }
  const dates = [...new Set(series.flatMap(s => s.pts.map(p => p[0])))].sort();
  const W = Math.max(320, box.clientWidth || 900), small = W < 600;
  const H = small ? 260 : 340, L = 48, R = small ? 74 : 86, T = 12, B = 34;
  const x = d => L + (dates.length < 2 ? (W - L - R) / 2 : dates.indexOf(d) / (dates.length - 1) * (W - L - R));
  const vals = series.flatMap(s => s.pts.map(p => p[1])).concat([0]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo || 10) * 0.08; lo -= pad; hi += pad;
  const y = v => T + (hi - v) / (hi - lo) * (H - T - B);
  const step = (() => { const r = (hi - lo) / 5, m = Math.pow(10, Math.floor(Math.log10(r))); return [1, 2, 5, 10].map(k => k * m).find(k => k >= r); })();
  let g = '';
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    g += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/>`;
    g += `<text x="${L - 8}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="var(--t3)">${Math.round(v * 100) / 100}</text>`;
  }
  g += `<line x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" stroke="var(--t3)" stroke-width="1"/>`;
  const ticks = dates.length <= (small ? 3 : 6) ? dates : (small ? [0, .5, 1] : [0, .25, .5, .75, 1]).map(f => dates[Math.round(f * (dates.length - 1))]);
  [...new Set(ticks)].forEach(d => g += `<text x="${x(d)}" y="${H - 10}" text-anchor="middle" font-size="11" fill="var(--t3)">${d}</text>`);
  // одна точка на дату — остання за день
  const daily = series.map(s => { const m = new Map(); s.pts.forEach(p => m.set(p[0], p[1])); return { ...s, d: [...m] }; });
  daily.forEach(s => {
    const path = s.d.map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join('');
    g += `<path d="${path}" fill="none" stroke="var(--s${s.c})" stroke-width="2" stroke-linejoin="round"/>`;
    const last = s.d[s.d.length - 1];
    g += `<circle cx="${x(last[0])}" cy="${y(last[1])}" r="4" fill="var(--s${s.c})" stroke="var(--card)" stroke-width="2"/>`;
    s.ly = y(last[1]); s.lx = x(last[0]);
  });
  // підписи на кінцях ліній не повинні накладатись
  const lab = [...daily].sort((a, b) => a.ly - b.ly);
  for (let i = 1; i < lab.length; i++) if (lab[i].ly - lab[i - 1].ly < 15) lab[i].ly = lab[i - 1].ly + 15;
  lab.forEach(s => g += `<text x="${s.lx + 8}" y="${s.ly + 4}" font-size="12" fill="var(--t2)">${s.name}</text>`);
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Накопичений прибуток по стратегіях">${g}
    <line id="xh" y1="${T}" y2="${H - B}" stroke="var(--t3)" stroke-dasharray="3 3" visibility="hidden"/>
    <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" id="hit"/></svg><div class="tip" id="tip"></div>`;
  const svg = box.querySelector('svg'), tip = box.querySelector('#tip'), xh = box.querySelector('#xh');
  const valueAt = (s, d) => { let v = null; for (const p of s.d) { if (p[0] <= d) v = p[1]; else break; } return v; };
  box.querySelector('#hit').addEventListener('pointermove', e => {
    const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
    const sx = pt.matrixTransform(svg.getScreenCTM().inverse()).x;
    let best = dates[0]; dates.forEach(d => { if (Math.abs(x(d) - sx) < Math.abs(x(best) - sx)) best = d; });
    xh.setAttribute('x1', x(best)); xh.setAttribute('x2', x(best)); xh.setAttribute('visibility', 'visible');
    const rows = daily.map(s => [s, valueAt(s, best)]).filter(r => r[1] !== null).sort((a, b) => b[1] - a[1])
      .map(([s, v]) => `<div><span class="sw" style="background:var(--s${s.c})"></span>${s.name}: <b>${v >= 0 ? '+' : ''}${v.toFixed(2)} $</b></div>`).join('');
    tip.innerHTML = `<div style="color:var(--t2);margin-bottom:4px">${best}</div>${rows}`;
    tip.style.display = 'block';
    const bx = box.getBoundingClientRect(), px = e.clientX - bx.left;
    tip.style.left = Math.min(px + 12, bx.width - tip.offsetWidth - 4) + 'px';
    tip.style.top = '8px';
  });
  box.querySelector('#hit').addEventListener('pointerleave', () => { tip.style.display = 'none'; xh.setAttribute('visibility', 'hidden'); });
}
draw();
let rz; window.addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(draw, 150); });
</script>
</body></html>
"""
