#!/usr/bin/env python3
"""
Build ONE clear AI-visibility dashboard from the saved results (no API calls, free).

  python build_dashboard.py        ->  ai_visibility_dashboard.html

Sections
  1. Where Café Athena ranks today   (latest real discovery run: data/scorecard_*.json)
  2. Why it loses                     (head-to-head test from measure.py: data/latest_sim.json)
  3. Projected impact of the fixes    (retrieval simulation: data/projection.json)
  4. What the agent published         (actions_log.csv)
  5. Weekly tracking                  (every real discovery run in data/results.csv)
Re-run discovery.py weekly, then this script, and section 5 grows a trend line.
"""
import csv, glob, html, json, os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

csv.field_size_limit(10**8)
DATA = Path("data")
OUT = Path("ai_visibility_dashboard.html")
E = html.escape


def latest(pattern):
    files = sorted(glob.glob(str(DATA / pattern)), key=os.path.getmtime)
    return json.load(open(files[-1], encoding="utf-8")) if files else None


def real_runs(rows):
    by = defaultdict(list)
    for r in rows:
        if not r["error"] and not r["run_id"].startswith("sim-") and r["platform"] != "mock":
            by[r["run_id"]].append(r)
    out = []
    for rid, rs in by.items():
        if len(rs) >= 20:
            out.append((min(x["timestamp"] for x in rs)[:10], rid,
                        round(100 * sum(x["mentioned"] == "1" for x in rs) / len(rs), 1), len(rs)))
    return sorted(out)


def main():
    sc = latest("scorecard_*.json")
    # newest scorecard with a meaningful number of answers
    cands = [json.load(open(f, encoding="utf-8")) for f in glob.glob(str(DATA / "scorecard_*.json"))]
    cands = [c for c in cands if c["overall"].get("answers", 0) >= 20] or [sc]
    sc = max(cands, key=lambda c: c["run_id"])
    o = sc["overall"]
    rows = list(csv.DictReader(open(DATA / "results.csv", encoding="utf-8")))
    sim = json.load(open(DATA / "latest_sim.json")) if (DATA / "latest_sim.json").exists() else None
    proj = json.load(open(DATA / "projection.json", encoding="utf-8")) if (DATA / "projection.json").exists() else None

    cats = sorted(sc["by_category"].items(), key=lambda kv: -kv[1]["mention_rate"])
    board = o["leaderboard"][:10]
    sources = sc.get("top_cited_sources", [])[:8]
    lost = sc.get("lost_queries", [])[:10]

    h2h = None
    if sim:
        ids = set(sim["query_ids"])
        def stats(rid):
            rs = [r for r in rows if r["run_id"] == rid and r["query_id"] in ids and not r["error"]]
            n = len(rs) or 1
            return dict(n=len(rs), mention=round(100 * sum(r["mentioned"] == "1" for r in rs) / n),
                        top1=round(100 * sum(r["position"] == "1" for r in rs) / n))
        h2h = dict(real=stats(sim["baseline"]), seen=stats(sim["before"]), seen_after=stats(sim["after"]))

    log = list(csv.DictReader(open("actions_log.csv", encoding="utf-8"))) if Path("actions_log.csv").exists() else []
    published = {}
    for r in log:
        if r["action"] in ("approved+published", "unpublished", "skipped"):
            published[r["artifact"]] = (r["action"], r["timestamp"])
    history = real_runs(rows)

    def pct(x):
        return f"{x * 100:.0f}%" if x <= 1 else f"{x:.0f}%"

    cat_rows = "".join(
        f'<div class="bar"><span class="lbl">{E(c.replace("_", " "))}</span><span class="track">'
        f'<span class="fill{" zero" if s["mention_rate"] == 0 else ""}" style="width:{max(s["mention_rate"] * 100, 0.8)}%"></span></span>'
        f'<span class="val">{pct(s["mention_rate"])} <small>of {s["answers"]}</small></span></div>' for c, s in cats)
    top = max(b["answers"] for b in board) or 1
    board_rows = "".join(
        f'<div class="bar{" me" if b["is_target"] else ""}"><span class="lbl">{E(b["restaurant"])}</span><span class="track">'
        f'<span class="fill" style="width:{b["answers"] / top * 100}%"></span></span><span class="val">{b["answers"]}</span></div>'
        for b in board)
    src_rows = "".join(f"<tr><td>{E(d)}</td><td class=n>{n}</td></tr>" for d, n in sources)
    lost_rows = "".join(f"<tr><td>{E(q['query'])}</td><td>{E(', '.join(q['top_winners']))}</td></tr>" for q in lost)
    pub_rows = "".join(f"<tr><td>{E(a)}</td><td>{'Published' if s == 'approved+published' else E(s.title())}</td>"
                       f"<td>{E(t.replace('T', ' ')[:16])}</td></tr>" for a, (s, t) in sorted(published.items()))

    proj_html = ""
    if proj:
        b, a = proj["before"], proj["after"]
        rows_p = "".join(f"<tr><td>{E(r['query'])}</td><td class=n>{r['before'] or '—'}</td><td class=n><b>{r['after'] or '—'}</b></td></tr>"
                         for r in proj["changes"])
        proj_html = f"""
<section class="card"><h2>3 · Projected impact of the fixes</h2>
<p class="lead">If AI search can <b>find</b> the new pages, Café Athena’s mention rate rises from <b>{b}%</b> to <b>{a}%</b>.</p>
<div class="duo"><div class="col"><div class="pair">
<div class="vbar"><b>{b}%</b><span style="height:{b * 2.4}px" class="g"></span><em>Before</em></div>
<div class="vbar"><b class="acc">{a}%</b><span style="height:{a * 2.4}px" class="o"></span><em>After</em></div></div></div>
<div class="col"><h3>Queries that gain a mention (rank)</h3><table><tr><th>Query</th><th class=n>Before</th><th class=n>After</th></tr>{rows_p}</table></div></div>
<p class="note">{E(proj['method'])}</p></section>"""

    h2h_html = ""
    if h2h:
        h2h_html = f"""
<section class="card dark"><h2>2 · Why it loses: AI never finds the page</h2>
<div class="kpis three">
<div class="kpi"><div class="v">{h2h['real']['mention']}%</div><div class="l">named on its {h2h['real']['n']} weakest queries in <b>real</b> AI search</div></div>
<div class="kpi"><div class="v acc">{h2h['seen']['mention']}%</div><div class="l">named when its page is placed beside the winners’ pages (#1 pick in {h2h['seen']['top1']}%)</div></div>
<div class="kpi"><div class="v">{h2h['seen_after']['mention']}%</div><div class="l">same test with the new pages (already at the ceiling, so no room to rise)</div></div></div>
<p class="lead light">The content wins once the AI reads it. The gap is <b>discoverability</b>: schema, dedicated pages, directories and reviews decide whether the page is retrieved at all.</p>
<p class="note light">Controlled test (measure.py): Claude reads Café Athena’s page plus the real sites of the restaurants that beat it, equal length, shuffled order.</p></section>"""

    hist_html = ""
    if history:
        pts = "".join(f"<tr><td>{d}</td><td>{E(r)}</td><td class=n>{m}%</td><td class=n>{n}</td></tr>" for d, r, m, n in history)
        hist_html = f"""
<section class="card"><h2>5 · Weekly tracking (real AI answers)</h2>
<table><tr><th>Date</th><th>Run</th><th class=n>Mention rate</th><th class=n>Answers</th></tr>{pts}</table>
<p class="note">Re-run <code>discovery.py run --platforms claude --web</code> weekly, then <code>build_dashboard.py</code>. Each run adds a row, so real impact shows up here as AI indexes the new pages.</p></section>"""

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Café Athena AI Visibility</title>
<style>
:root{{--ink:#13294B;--mut:#5B6472;--bg:#F6F1E7;--card:#fff;--line:#E2D9C6;--acc:#D46A3A;--blue:#2E6DA4;--dark:#13294B}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:32px 16px 64px}}h1{{font:600 40px/1.1 Georgia,serif;margin:0 0 6px}}
.sub{{color:var(--mut);margin-bottom:24px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px;margin-bottom:20px}}
.card.dark{{background:var(--dark);color:#F6F1E7;border:none}}h2{{font:600 22px/1.2 Georgia,serif;margin:0 0 16px}}h3{{font-size:15px;margin:0 0 10px;color:var(--mut)}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}}.kpis.three{{grid-template-columns:repeat(3,1fr)}}
.kpi{{border:1px solid var(--line);border-radius:12px;padding:16px}}.dark .kpi{{border-color:#2F4A75}}
.v{{font:700 38px/1 Georgia,serif}}.v.acc,.acc{{color:var(--acc)}}.dark .v.acc{{color:#F0A35E}}.l{{color:var(--mut);font-size:14px;margin-top:6px}}.dark .l{{color:#B8C4D6}}
.duo{{display:grid;grid-template-columns:1fr 1fr;gap:28px}}.bar{{display:grid;grid-template-columns:170px 1fr 70px;gap:10px;align-items:center;margin:7px 0;font-size:14px}}
.track{{background:#EFE7D6;border-radius:5px;height:18px;overflow:hidden}}.fill{{display:block;height:100%;background:var(--blue);border-radius:5px}}
.fill.zero{{background:#C9CFD8}}.bar.me .lbl,.bar.me .val{{color:var(--acc);font-weight:700}}.bar.me .fill{{background:var(--acc)}}.val{{text-align:right}}
small{{color:var(--mut)}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}}
th{{color:var(--mut);font-weight:600}}.n{{text-align:right}}.lead{{font-size:18px;margin:4px 0 16px}}.lead.light{{color:#DCE4EE;margin-top:16px}}
.note{{font-size:13px;color:var(--mut);margin:14px 0 0}}.note.light{{color:#B8C4D6}}
.pair{{display:flex;gap:36px;align-items:flex-end;height:290px;justify-content:center}}.vbar{{display:flex;flex-direction:column;align-items:center;gap:8px}}
.vbar b{{font:700 26px Georgia,serif}}.vbar span{{width:110px;border-radius:8px 8px 0 0;display:block}}.vbar .g{{background:#B8C4D6}}.vbar .o{{background:var(--acc)}}
.vbar em{{font-style:normal;color:var(--mut)}}code{{background:#EFE7D6;padding:1px 5px;border-radius:4px}}
@media(max-width:760px){{.kpis,.kpis.three,.duo{{grid-template-columns:1fr}}.bar{{grid-template-columns:120px 1fr 60px}}}}
</style></head><body><main>
<h1>Café Athena: AI search visibility</h1>
<div class="sub">1846 Garnet Ave, Pacific Beach, San Diego · run {E(sc['run_id'])} · built {datetime.now():%b %d, %Y %H:%M}</div>

<section class="card"><h2>1 · Where Café Athena ranks today</h2>
<div class="kpis">
<div class="kpi"><div class="v acc">{pct(o['mention_rate'])}</div><div class="l">of {o['answers']} real AI answers name Café Athena</div></div>
<div class="kpi"><div class="v">{pct(o['top3_rate'])}</div><div class="l">put it in the top 3</div></div>
<div class="kpi"><div class="v">{o['avg_position'] or '—'}</div><div class="l">average position when named</div></div>
<div class="kpi"><div class="v">{o['share_of_voice'] * 100:.1f}%</div><div class="l">share of voice, #{o['sov_rank']} of {o['restaurants_seen']} restaurants</div></div></div>
<div class="duo"><div class="col"><h3>Mention rate by category</h3>{cat_rows}</div>
<div class="col"><h3>Most-named restaurants (answers out of {o['answers']})</h3>{board_rows}</div></div>
<div class="duo" style="margin-top:22px"><div class="col"><h3>Queries it loses, and who wins</h3><table><tr><th>Query</th><th>Winners</th></tr>{lost_rows}</table></div>
<div class="col"><h3>Sources the AI cited most</h3><table><tr><th>Site</th><th class=n>Citations</th></tr>{src_rows}</table></div></div>
<p class="note">Claude API with live web search, one answer per query. Restaurants extracted in order from each answer.</p></section>
{h2h_html}{proj_html}
<section class="card"><h2>4 · What the agent published (with human approval)</h2>
<table><tr><th>Asset</th><th>Status</th><th>When</th></tr>{pub_rows}</table>
<p class="note">Every draft was reviewed and approved by a person before publishing (actions_log.csv). Preview: <code>site/index.html</code>. Undo any page with <code>action.py undo &lt;file&gt;</code>.</p></section>
{hist_html}
</main></body></html>"""
    OUT.write_text(page, encoding="utf-8")
    print(f"Dashboard -> {OUT.resolve()}")


if __name__ == "__main__":
    main()
