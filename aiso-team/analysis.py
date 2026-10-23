#!/usr/bin/env python3
"""
AISO Analysis — why competitors beat Café Athena.

Reads the latest Discovery scorecard, then for Café Athena + the top competitors:
  1. On-site audit  — fetches homepage/menu/about/faq/events pages, parses
     schema.org JSON-LD, checks for FAQ content and key selling-point keywords.
  2. Off-site audit — Claude with live web search reports reviews, press,
     "best of" lists and directory listings (Yelp, TripAdvisor, Reddit...).
  3. Diagnosis      — Claude turns all evidence into a ranked list of gaps.

  python analysis.py                 # latest discovery run, top 5 competitors
  python analysis.py --top 4 --run <run_id>

Outputs: data/analysis_<run>.json  and  data/report_<run>.md
Needs: ANTHROPIC_API_KEY
"""
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
import argparse, glob, html, json, os, re, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic

DATA = Path("data")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
WEB_TOOL = os.getenv("CLAUDE_WEB_TOOL", "web_search_20250305")
TARGET = {"name": "Café Athena", "url": "https://www.cafeathena.com"}
UA = "Mozilla/5.0 (AISO-hackathon-audit; NYU SPS x Pulse Foundry)"
PAGES = ["", "/menu", "/about", "/about-us", "/faq", "/events", "/private-events",
         "/catering", "/happy-hour"]

# Café Athena's selling points — does each site's copy say these out loud?
SIGNALS = {
    "gluten_free": r"gluten[\s-]?free|celiac",
    "vegan": r"\bvegan\b",
    "vegetarian": r"vegetarian",
    "happy_hour": r"happy\s*hour",
    "private_events": r"private (event|dining|part)|rehearsal|banquet|event space|large group",
    "catering": r"\bcater",
    "cocktails": r"cocktail",
    "neighborhood": r"pacific beach|garnet|mission bay|\bPB\b",
    "heritage": r"since 19\d\d|since 20\d\d|family[- ]owned|established",
}
client = anthropic.Anthropic()


# ---------- helpers ----------
def text_of(resp):
    return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")

def parse_json(s):
    m = re.search(r"\{.*\}", s, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None

import ssl
try:
    import certifi  # Mac python.org builds ship without CA certs; certifi comes with the anthropic SDK
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
FETCH_LOG = {}

def fetch(url):
    """Try as a bot first (what AI crawlers see), then as a browser. Records the outcome."""
    for label, ua in (("bot", UA), ("browser", BROWSER_UA)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,*/*"})
            with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
                body = r.read(2_000_000).decode("utf-8", "ignore")
                FETCH_LOG[url] = f"200 as {label}"
                return body
        except Exception as e:
            FETCH_LOG[url] = f"{label}: {e.__class__.__name__} {str(e)[:80]}"
    return None

def visible_text(h):
    h = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h))).strip()

def schema_types(obj, out):
    if isinstance(obj, dict):
        t = obj.get("@type")
        for x in (t if isinstance(t, list) else [t]):
            if isinstance(x, str):
                out.add(x)
        for v in obj.values():
            schema_types(v, out)
    elif isinstance(obj, list):
        for v in obj:
            schema_types(v, out)


# ---------- 1. on-site audit ----------
def find_website(name):
    r = client.messages.create(
        model=MODEL, max_tokens=600,
        tools=[{"type": WEB_TOOL, "name": "web_search", "max_uses": 2}],
        messages=[{"role": "user", "content":
                   f'Find the official website of the restaurant "{name}" in San Diego, CA. '
                   'Reply with ONLY the homepage URL, or NONE if it has no own website.'}])
    m = re.search(r"https?://[^\s)\"'>\]]+", text_of(r))
    return m.group(0).rstrip(".,/") if m else None

def onsite_audit(base):
    if not base:
        return {"website": None, "reachable": False}
    root = re.match(r"https?://[^/]+", base).group(0)
    host = re.sub(r"^https?://(www\.)?", "", root)
    for cand in (root, f"https://www.{host}", f"https://{host}", f"http://www.{host}"):
        if fetch(cand):
            root = cand
            break
    pages, types, texts, title, meta = [], set(), [], "", ""
    for p in PAGES:
        h = fetch(root + p)
        if not h:
            continue
        pages.append(p or "/")
        if not title:
            t = re.search(r"<title[^>]*>(.*?)</title>", h, re.S | re.I)
            title = html.unescape(t.group(1)).strip() if t else ""
            d = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', h, re.I)
            meta = html.unescape(d.group(1)).strip() if d else ""
        for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', h, re.S | re.I):
            try:
                schema_types(json.loads(block.strip()), types)
            except json.JSONDecodeError:
                pass
        texts.append(visible_text(h))
    full = " ".join(texts)
    return {
        "website": root, "reachable": bool(pages), "pages_found": pages,
        "homepage_fetch": FETCH_LOG.get(root, "not tried"),
        "note": "" if pages else ("Homepage could not be fetched by a simple crawler. Treat as 'invisible to "
                                  "non-JavaScript AI crawlers', NOT as proof the site is down."),
        "title": title[:150], "meta_description": meta[:300],
        "schema_types": sorted(types),
        "has_restaurant_schema": bool(types & {"Restaurant", "FoodEstablishment", "BarOrPub"}),
        "has_faq": "FAQPage" in types or bool(re.search(r"frequently asked|\bFAQ", full, re.I)),
        "has_menu_schema": bool(types & {"Menu", "MenuItem", "MenuSection"}),
        "has_rating_schema": bool(types & {"AggregateRating", "Review"}),
        "word_count": len(full.split()),
        "signals": {k: bool(re.search(v, full, re.I)) for k, v in SIGNALS.items()},
    }


# ---------- 2. off-site audit ----------
OFFSITE_PROMPT = """Research the San Diego restaurant "{name}" using web search. Report only what you find; never guess.
Return ONLY JSON with these keys (null / [] when unknown):
{{"google_rating": num, "google_reviews": int, "yelp_rating": num, "yelp_reviews": int,
 "tripadvisor_rating": num, "tripadvisor_reviews": int,
 "best_of_lists": [{{"outlet": str, "title": str, "year": int}}],
 "recent_press": [{{"outlet": str, "title": str, "year": int}}],
 "reddit_mentions": "short summary of how it comes up on Reddit/forums",
 "directories": ["sites it is listed on, e.g. OpenTable, Resy, findmeglutenfree, Peerspace, HappyHourFinder"],
 "known_for": "what reviewers/press say it is known for, one sentence"}}
Max 5 items per list."""

def offsite_audit(name):
    try:
        r = client.messages.create(
            model=MODEL, max_tokens=2500,
            tools=[{"type": WEB_TOOL, "name": "web_search", "max_uses": 6}],
            messages=[{"role": "user", "content": OFFSITE_PROMPT.format(name=name)}])
        return parse_json(text_of(r)) or {"error": "unparseable"}
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {str(e)[:150]}"}


def audit(entry):
    name = entry["restaurant"]
    url = TARGET["url"] if entry.get("is_target") else None
    try:
        url = url or find_website(name)
    except Exception:
        url = None
    print(f"  auditing {name} ({url or 'no site found'})")
    return {**entry, "onsite": onsite_audit(url), "offsite": offsite_audit(name)}


# ---------- 3. diagnosis ----------
DIAG_PROMPT = """You are an AI-search-optimization analyst. Café Athena (Greek/Mediterranean restaurant & bar, Pacific Beach, San Diego, open since 1990; daily 3–6 PM happy hour with tapas-style small plates; vegan, vegetarian and gluten-free options; Greek-mythology-themed cocktails; catering, group dining and private events) wants to be recommended by AI assistants.

Below is evidence: its AI-search visibility scorecard, the queries it loses and who wins them, the sources AI assistants cited, and on-site + off-site audits of Café Athena and its top competitors.

Diagnose WHY competitors win. Every gap must cite specific evidence from the data (numbers, missing schema, missing keywords, review counts, missing listings, which queries are lost). No generic SEO advice.

Return ONLY JSON:
{{"headline": "one-sentence diagnosis for the owner",
 "gaps": [{{"rank": 1, "gap": "short title", "evidence": "specific data points", "competitor_example": "who does it better and how",
   "queries_affected": ["query ids or categories"], "impact": "high|medium|low", "effort": "low|medium|high",
   "fix": "concrete action"}}]}}
Give 5–8 gaps ranked by impact ÷ effort.

EVIDENCE:
{evidence}"""

def diagnose(scorecard, audits):
    o = scorecard["overall"]
    evidence = {
        "visibility": {k: o[k] for k in ("answers", "mention_rate", "avg_position", "top3_rate",
                                         "share_of_voice", "sov_rank", "restaurants_seen")},
        "by_category": scorecard["by_category"], "by_platform": scorecard["by_platform"],
        "lost_queries": scorecard["lost_queries"][:25],
        "top_cited_sources": scorecard["top_cited_sources"],
        "audits": audits,
    }
    msgs = [{"role": "user", "content": DIAG_PROMPT.format(
        evidence=json.dumps(evidence, ensure_ascii=False)[:150000])}]
    raw = ""
    for attempt in range(2):
        r = client.messages.create(model=MODEL, max_tokens=12000, messages=msgs)
        raw = text_of(r)
        d = parse_json(raw)
        if d and d.get("gaps"):
            return d
        print(f"  diagnosis attempt {attempt + 1} unparseable (stop_reason={r.stop_reason}) — retrying shorter…")
        msgs = msgs[:1] + [{"role": "assistant", "content": raw or "(empty)"},
                           {"role": "user", "content": "That was not complete valid JSON. Return ONLY the JSON object "
                            "again: max 7 gaps, every field under 50 words, no markdown fences."}]
    return {"headline": "diagnosis failed", "gaps": [], "raw": raw[:3000]}


# ---------- report ----------
def yn(b):
    return "✅" if b else "❌"

def build_report(run_id, sc, audits, diag):
    o = sc["overall"]
    L = [f"# Café Athena — AI Search Visibility Report", f"_Discovery run {run_id} · {o['answers']} AI answers_", "",
         f"**{diag.get('headline', '')}**", "",
         "## Where Café Athena ranks",
         f"- Mentioned in **{o['mention_rate']:.0%}** of AI answers (top-3 in {o['top3_rate']:.0%})",
         f"- Average position when mentioned: **{o['avg_position'] or '—'}**",
         f"- Share of voice: **{o['share_of_voice']:.1%}** — #{o['sov_rank'] or '—'} of {o['restaurants_seen']} restaurants named", "",
         "| Category | Mention rate |", "|---|---|"]
    L += [f"| {c} | {s['mention_rate']:.0%} |" for c, s in sc["by_category"].items()]
    L += ["", "## Head-to-head", "",
          "| Restaurant | AI mention rate | Restaurant schema | FAQ | Menu schema | Selling points on site | Google reviews | Yelp reviews |",
          "|---|---|---|---|---|---|---|---|"]
    for a in audits:
        on, off = a["onsite"], a["offsite"]
        sig = f"{sum(on.get('signals', {}).values())}/{len(SIGNALS)}" if on.get("reachable") else "site n/a"
        L.append(f"| {'**' + a['restaurant'] + '**' if a.get('is_target') else a['restaurant']} | {a['mention_rate']:.0%} | "
                 f"{yn(on.get('has_restaurant_schema'))} | {yn(on.get('has_faq'))} | {yn(on.get('has_menu_schema'))} | {sig} | "
                 f"{off.get('google_reviews') or '—'} | {off.get('yelp_reviews') or '—'} |")
    tgt = next((a for a in audits if a.get("is_target")), None)
    if tgt and tgt["onsite"].get("reachable"):
        missing = [k for k, v in tgt["onsite"]["signals"].items() if not v]
        L += ["", f"**cafeathena.com never mentions:** {', '.join(missing) or 'nothing missing'}",
              f"**Schema found:** {', '.join(tgt['onsite']['schema_types']) or 'none'}"]
    L += ["", "## Why competitors win — prioritized gaps", ""]
    for g in diag.get("gaps", []):
        L += [f"### {g.get('rank')}. {g.get('gap')}  ·  impact {g.get('impact')} / effort {g.get('effort')}",
              f"- **Evidence:** {g.get('evidence')}",
              f"- **Competitor example:** {g.get('competitor_example')}",
              f"- **Queries affected:** {', '.join(map(str, g.get('queries_affected', [])))}",
              f"- **Fix:** {g.get('fix')}", ""]
    L += ["## Queries Café Athena loses", "", "| Query | Miss rate | Who wins |", "|---|---|---|"]
    L += [f"| {q['query']} | {q['miss_rate']:.0%} | {', '.join(q['top_winners'])} |" for q in sc["lost_queries"][:20]]
    if sc["top_cited_sources"]:
        L += ["", "## Sources AI assistants cite", ""] + [f"- {d} ({n})" for d, n in sc["top_cited_sources"][:12]]
    return "\n".join(L)


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="latest")
    ap.add_argument("--top", type=int, default=5, help="competitors to audit")
    ap.add_argument("--check-site", help="just audit one website (free, no API calls) and exit")
    ap.add_argument("--rediagnose", action="store_true",
                    help="reuse the saved audits and only redo the gap diagnosis + report")
    a = ap.parse_args()
    if a.check_site:
        print(json.dumps(onsite_audit(a.check_site), indent=2))
        return

    files = sorted(glob.glob(str(DATA / "scorecard_*.json")), key=os.path.getmtime)
    if not files:
        sys.exit("No scorecard found — run discovery.py first.")
    path = files[-1] if a.run == "latest" else str(DATA / f"scorecard_{a.run}.json")
    sc = json.load(open(path))
    run_id = sc["run_id"]
    board = sc["overall"]["leaderboard"]

    saved = DATA / f"analysis_{run_id}.json"
    if a.rediagnose and saved.exists():
        audits = json.load(open(saved))["audits"]
        print(f"Re-diagnosing run {run_id} from saved audits ({len(audits)} restaurants)…")
        finish(run_id, sc, audits)
        return

    entries = [b for b in board if not b["is_target"]][:a.top]
    tgt = next((b for b in board if b["is_target"]), None) or {
        "restaurant": TARGET["name"], "is_target": True, "mention_rate": 0.0, "answers": 0}
    entries.insert(0, tgt)
    print(f"Analysis for run {run_id}: auditing Café Athena + {len(entries) - 1} competitors…")
    with ThreadPoolExecutor(max_workers=3) as ex:
        audits = list(ex.map(audit, entries))
    finish(run_id, sc, audits)


def finish(run_id, sc, audits):
    print("  diagnosing gaps…")
    diag = diagnose(sc, audits)
    out = {"run_id": run_id, "audits": audits, "diagnosis": diag}
    (DATA / f"analysis_{run_id}.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    report = build_report(run_id, sc, audits, diag)
    (DATA / f"report_{run_id}.md").write_text(report)

    print(f"\n=== {diag.get('headline', '')} ===\n")
    for g in diag.get("gaps", []):
        print(f" {g.get('rank')}. [{g.get('impact')} impact / {g.get('effort')} effort] {g.get('gap')}")
        print(f"    evidence: {str(g.get('evidence'))[:180]}")
        print(f"    fix:      {str(g.get('fix'))[:180]}\n")
    print(f"saved → data/report_{run_id}.md  (open it — that's your visibility report)")


if __name__ == "__main__":
    main()
