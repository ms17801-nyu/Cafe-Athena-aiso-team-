#!/usr/bin/env python3
"""
AISO Measurement — did visibility improve?

Real AI assistants take weeks to re-crawl a site, so for the demo we run a controlled
simulation on the queries Café Athena loses, with live web search ON in both arms:
  CONTROL:   the AI also "retrieves" today's cafeathena.com text
  TREATMENT: the AI also "retrieves" the new pages approved in action.py (site/)
Only the Café Athena page differs, so any lift comes from our content.

  python measure.py simulate            # ~20 lost queries × 2 arms (~$2)
  python measure.py simulate --n 30 --samples 2
  python measure.py dashboard           # before/after + history chart → measure_dashboard.html
  python measure.py schedule            # how to re-run the real query bank weekly

Needs: ANTHROPIC_API_KEY, discovery.py + analysis.py in the same folder.
"""
import argparse, csv, json, math, os, random, sys, uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import discovery as D
from analysis import fetch, visible_text

SITE = Path("site")
DOC_CAP = 8000  # same character budget for every restaurant's page — fair fight
SIM_SYSTEM = """You are a helpful assistant answering a San Diego diner's question. A web search returned the
restaurant pages below (in random order). Answer the question naturally, as you would for a real user:
recommend the restaurants that genuinely fit, best first. Use only these pages as your sources.

{docs}"""


def site_text(root):
    parts = []
    for p in ("", "/about", "/menu"):
        h = fetch(root.rstrip("/") + p)
        if h:
            parts.append(visible_text(h))
    return " ".join(parts)[:DOC_CAP]

COMP_CACHE = D.DATA / "sim_competitors.json"

def competitor_docs(k, sc):
    """Real web pages of (a) the restaurants that actually beat Athena on the lost queries and
    (b) the top competitors audited in analysis.py. Websites are cached in data/sim_competitors.json."""
    from analysis import find_website
    files = sorted(D.DATA.glob("analysis_*.json"), key=os.path.getmtime)
    if not files:
        sys.exit("Run analysis.py first.")
    cache = json.load(open(COMP_CACHE)) if COMP_CACHE.exists() else {}
    winners = defaultdict(int)
    for q in sc["lost_queries"]:
        for w in q["top_winners"]:
            winners[w] += 1
    names = [w for w, _ in sorted(winners.items(), key=lambda x: -x[1])][:k]
    for a in json.load(open(files[-1]))["audits"]:
        if not a.get("is_target") and a["restaurant"] not in names:
            names.append(a["restaurant"])
            cache.setdefault(a["restaurant"], a.get("onsite", {}).get("website"))
    docs = []
    for n in names:
        if len(docs) >= k:
            break
        if n not in cache:
            try:
                cache[n] = find_website(n)
            except Exception:
                cache[n] = None
        if not cache[n]:
            continue
        txt = site_text(cache[n])
        if len(txt) > 300:
            docs.append((n, cache[n], txt))
            print(f"  competitor page: {n} ({len(txt):,} chars)")
    json.dump(cache, open(COMP_CACHE, "w"), indent=1)
    return docs

def strip_placeholders(t):
    import re
    return re.sub(r"\s*\[CONFIRM:[^\]]*\]", "", t)

def athena_pages_before():
    txt = site_text("https://www.cafeathena.com")
    if len(txt) < 300:
        sys.exit("Couldn't fetch cafeathena.com for the control arm.")
    return [("https://www.cafeathena.com", txt)]

def athena_pages_after():
    pages = sorted(SITE.glob("*.md"))
    if not pages:
        sys.exit("No approved pages in site/ — run: python3 action.py review")
    # the existing site stays, the new dedicated pages are added (unfilled [CONFIRM] gaps removed,
    # as they would be before going live)
    return athena_pages_before() + [(f"https://www.cafeathena.com/{p.stem}",
                                     strip_placeholders(p.read_text())[:DOC_CAP]) for p in pages]

STOP = set("the a an in of for with and or to near best good where can i get my is are at on what restaurant restaurants san diego".split())

def retrieve(q, pages):
    """Like a search engine: return Athena's single page that best matches the query."""
    terms = [w for w in q.lower().replace("-", " ").split() if w not in STOP and len(w) > 2]
    def score(t):
        low = t.lower()
        coverage = sum(1 for w in terms if w[:5] in low)              # how many query words it covers
        k = max(len(low) / 1000, 0.3)
        focus = sum(math.log1p(low.count(w[:5]) / k) for w in terms)  # how focused it is (per 1k chars)
        return coverage * 1000 + min(focus, 999)
    return max(pages, key=lambda p: score(p[1]))

def make_ask(athena_pages, comps):
    def ask(p, q, web):
        url, txt = retrieve(q, athena_pages)
        order = [(url, txt)] + [(u, t) for _, u, t in comps]
        random.shuffle(order)  # no position advantage
        docs = "\n\n".join(f'<document source="{u}">\n{t}\n</document>' for u, t in order)
        r = D._clients["claude"].messages.create(
            model=D.MODELS["claude"], max_tokens=1200, system=SIM_SYSTEM.format(docs=docs),
            messages=[{"role": "user", "content": q}])
        return "\n".join(b.text for b in r.content if b.type == "text"), []
    return ask


def cmd_simulate(a):
    D.init_client("claude")
    if a.baseline:
        base = a.baseline
    else:  # latest real discovery run = newest scorecard file
        files = sorted(D.DATA.glob("scorecard_*.json"), key=os.path.getmtime)
        if not files:
            sys.exit("Run discovery.py first.")
        base = json.load(open(files[-1]))["run_id"]
    sc = json.load(open(D.DATA / f"scorecard_{base}.json"))
    ids = [q["query_id"] for q in sc["lost_queries"][:a.n]]
    rows = [r for r in csv.DictReader(open(a.queries, encoding="utf-8")) if r["id"] in ids]
    stamp = datetime.now().strftime("%Y%m%d-%H%M") + "-" + uuid.uuid4().hex[:4]
    print("Loading pages (same for both arms except Café Athena's)…")
    comps = competitor_docs(a.competitors, sc)
    if len(comps) < 2:
        sys.exit("Need at least 2 competitor pages — check your internet / re-run analysis.py.")
    arms = {f"sim-before-{stamp}": athena_pages_before(), f"sim-after-{stamp}": athena_pages_after()}
    print(f"Simulating {len(rows)} lost queries × 2 arms × {a.samples} samples, "
          f"Café Athena vs {len(comps)} competitors (baseline run {base})")

    new = not D.RESULTS.exists()
    with open(D.RESULTS, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=D.FIELDS)
        if new:
            w.writeheader()
        for run_id, doc in arms.items():
            D.ask = make_ask(doc, comps)
            jobs = [(r, s) for r in rows for s in range(1, a.samples + 1)]
            with ThreadPoolExecutor(max_workers=a.workers) as ex:
                futs = [ex.submit(D.run_one, "claude", r, s, False, run_id, ["claude"]) for r, s in jobs]
                for i, fut in enumerate(as_completed(futs), 1):
                    rec = fut.result()
                    w.writerow(rec); f.flush()
                    flag = "ERR" if rec["error"] else ("✓ #" + str(rec["position"]) if rec["mentioned"] else "·")
                    print(f"  [{run_id.split('-')[1]} {i}/{len(jobs)}] {rec['query_id']} {flag}")
    json.dump({"baseline": base, "before": list(arms)[0], "after": list(arms)[1], "query_ids": ids},
              open(D.DATA / "latest_sim.json", "w"))
    cmd_dashboard(a)


def summarize(rows):
    s = D.scorecard(rows) or {}
    cats = defaultdict(list)
    for r in rows:
        cats[r["category"]].append(r["mentioned"] == "1")
    top1 = sum(r["position"] == "1" for r in rows) / len(rows) if rows else 0
    return {"mention_rate": s.get("mention_rate", 0), "avg_position": s.get("avg_position"),
            "top1_rate": round(top1, 3),
            "top3_rate": s.get("top3_rate", 0), "share_of_voice": s.get("share_of_voice", 0),
            "answers": s.get("answers", 0),
            "by_category": {c: round(sum(v) / len(v), 3) for c, v in sorted(cats.items())}}

def cmd_dashboard(a):
    allrows = [r for r in csv.DictReader(open(D.RESULTS, encoding="utf-8")) if not r["error"]]
    sim = json.load(open(D.DATA / "latest_sim.json")) if (D.DATA / "latest_sim.json").exists() else None
    out = {}
    if sim:
        ids = set(sim["query_ids"])
        for label, rid in (("Real AI today", sim["baseline"]), ("Simulated: current site", sim["before"]),
                           ("Simulated: with our fixes", sim["after"])):
            out[label] = summarize([r for r in allrows if r["run_id"] == rid and r["query_id"] in ids])
        print("\n=== Before → after on Café Athena's lost queries ===")
        for k, v in out.items():
            print(f"  {k:<28} mention {v['mention_rate']:.0%}  #1 pick {v['top1_rate']:.0%}  "
                  f"avg rank {v['avg_position'] or '—'}  top-3 {v['top3_rate']:.0%}  ({v['answers']} answers)")
        b, f_ = out["Simulated: current site"], out["Simulated: with our fixes"]
        print(f"\n  Effect of our content (simulated): mention {b['mention_rate']:.0%} → {f_['mention_rate']:.0%}, "
              f"#1 pick {b['top1_rate']:.0%} → {f_['top1_rate']:.0%}, "
              f"avg rank {b['avg_position'] or '—'} → {f_['avg_position'] or '—'}")

    # history of real discovery runs (the weekly re-run loop)
    hist = defaultdict(list)
    for r in allrows:
        if not r["run_id"].startswith("sim-") and r["platform"] != "mock":
            hist[r["run_id"]].append(r)
    history = sorted([(min(x["timestamp"] for x in rs)[:16].replace("T", " "),
                       round(sum(x["mentioned"] == "1" for x in rs) / len(rs), 3)) for rid, rs in hist.items()
                      if len(rs) >= 20])

    cats = sorted({c for v in out.values() for c in v["by_category"]}) if out else []
    colors = ["#9aa5b1", "#d8a657", "#2e7d5b"]
    datasets = [{"label": k, "data": [round(v["by_category"].get(c, 0) * 100) for c in cats],
                 "backgroundColor": colors[i]} for i, (k, v) in enumerate(out.items())]
    kpis = "".join(f'<div class="kpi"><div class="v">{v["mention_rate"]:.0%}</div><div class="l">{k}<br>'
                   f'mentioned · #1 pick {v.get("top1_rate", 0):.0%} · avg rank {v["avg_position"] or "—"}</div></div>'
                   for k, v in out.items())
    html = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Café Athena — AI Visibility</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>body{{font:15px/1.5 -apple-system,Helvetica,sans-serif;max-width:980px;margin:32px auto;padding:0 16px;color:#1d2433}}
h1{{margin:0}}.sub{{color:#5b6472;margin-bottom:24px}}.kpis{{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0 28px}}
.kpi{{flex:1;min-width:200px;border:1px solid #e3e6ea;border-radius:10px;padding:16px}}.v{{font-size:34px;font-weight:700}}
.l{{color:#5b6472}}.card{{border:1px solid #e3e6ea;border-radius:10px;padding:16px;margin-bottom:24px}}
.note{{font-size:13px;color:#5b6472}}</style></head><body>
<h1>Café Athena — AI search visibility</h1>
<div class="sub">Mention rate on the queries Café Athena currently loses · generated {datetime.now():%b %d, %Y %H:%M}</div>
<div class="kpis">{kpis}</div>
<div class="card"><h3>Mention rate by query type (%)</h3><canvas id="cat"></canvas>
<p class="note">Controlled simulation: for each query the AI chooses between Café Athena's best-matching page and the real
web pages of the restaurants that beat it (same length cap, shuffled order, no web search). "Before" can only retrieve
today's homepage; "after" can also retrieve the new dedicated pages. Everything else is identical.
Real assistants re-index over weeks, so the real effect is confirmed by the weekly re-run below.</p></div>
<div class="card"><h3>Weekly re-run of the full query bank (real AI answers)</h3><canvas id="hist"></canvas></div>
<script>
new Chart(document.getElementById('cat'),{{type:'bar',data:{{labels:{json.dumps(cats)},datasets:{json.dumps(datasets)}}},
 options:{{scales:{{y:{{beginAtZero:true,max:100}}}}}}}});
new Chart(document.getElementById('hist'),{{type:'line',data:{{labels:{json.dumps([h[0] for h in history])},
 datasets:[{{label:'Mention rate %',data:{json.dumps([round(h[1]*100) for h in history])},borderColor:'#2e7d5b',tension:.2}}]}},
 options:{{scales:{{y:{{beginAtZero:true,max:100}}}}}}}});
</script></body></html>"""
    Path("measure_dashboard.html").write_text(html)
    print("\nDashboard → measure_dashboard.html (open it in your browser)")


def cmd_schedule(a):
    here = os.getcwd()
    print(f"""Real measurement = re-run the same query bank on a schedule and watch the trend line.

Weekly, Monday 9 AM (Mac/Linux):
  1. crontab -e
  2. add this line, save and close:
     0 9 * * 1 cd "{here}" && ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY python3 discovery.py run --platforms claude --web >> cron.log 2>&1 && python3 measure.py dashboard >> cron.log 2>&1

Each run appends to data/results.csv; the dashboard's history chart picks it up automatically.""")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("simulate")
    s.add_argument("--n", type=int, default=20, help="how many lost queries")
    s.add_argument("--competitors", type=int, default=6, help="competitor pages in the pool")
    s.add_argument("--samples", type=int, default=1)
    s.add_argument("--workers", type=int, default=4)
    s.add_argument("--baseline", help="discovery run id (default: latest)")
    s.add_argument("--queries", default="queries.csv")
    sp.add_parser("dashboard")
    sp.add_parser("schedule")
    a = ap.parse_args()
    {"simulate": cmd_simulate, "dashboard": cmd_dashboard, "schedule": cmd_schedule}[a.cmd](a)
