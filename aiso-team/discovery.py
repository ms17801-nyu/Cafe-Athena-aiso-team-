#!/usr/bin/env python3
"""
AISO Discovery — Café Athena
Runs a query bank against AI assistants, extracts the restaurants each answer
recommends (in order), and logs whether Café Athena appears and at what rank.

  python discovery.py run --platforms claude,openai,perplexity,gemini --samples 2
  python discovery.py run --platforms claude,gemini --web     # live-web grounding
  python discovery.py run --platforms mock                    # test pipeline, no keys
  python discovery.py score                                   # scorecard, latest run
  python discovery.py score --run <run_id>

Keys (set what you have): ANTHROPIC_API_KEY, OPENAI_API_KEY,
PERPLEXITY_API_KEY, GEMINI_API_KEY. Model names are overridable via env vars.
"""
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
import argparse, csv, json, os, re, random, sys, time, unicodedata, uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

csv.field_size_limit(10**8)
DATA = Path("data")
RESULTS = DATA / "results.csv"

MODELS = {
    "claude": os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"),
    "openai": os.getenv("OPENAI_MODEL", "gpt-4o"),
    "perplexity": os.getenv("PPLX_MODEL", "sonar"),
    "gemini": os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
    "mock": "mock",
}
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "claude-haiku-5-5")
EXTRACT_MODEL_OPENAI = os.getenv("EXTRACT_MODEL_OPENAI", "gpt-4o-mini")
CLAUDE_WEB_TOOL = os.getenv("CLAUDE_WEB_TOOL", "web_search_20250305")

FIELDS = ["run_id", "timestamp", "platform", "model", "web", "query_id", "query",
          "category", "geo", "sample", "mentioned", "position", "n_restaurants",
          "restaurants", "competitors", "citations", "response_text", "error"]

# ---------- target matching ----------
TARGET = "Café Athena"
TARGET_TEXT_RE = re.compile(r"caf[eé]\s+athena", re.I)

def ascii_lower(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()

def is_target(name):
    n = ascii_lower(name)
    return "cafe athena" in n or "athena cafe" in n or n in ("athena", "athena restaurant")

def canon(name):
    """Key for aggregating the same competitor across differently-worded answers."""
    if is_target(name):
        return "cafe athena"
    n = re.sub(r"[^a-z0-9 ]+", " ", ascii_lower(name))
    n = re.sub(r"^the ", "", n)
    n = re.sub(r"\b(restaurant|san diego|pacific beach|pb|la jolla)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()

# ---------- platform clients ----------
_clients = {}

def init_client(p):
    if p in _clients or p == "mock":
        return
    if p == "claude":
        import anthropic
        _clients[p] = anthropic.Anthropic()
    elif p == "openai":
        from openai import OpenAI
        _clients[p] = OpenAI()
    elif p == "perplexity":
        from openai import OpenAI
        _clients[p] = OpenAI(api_key=os.environ["PERPLEXITY_API_KEY"],
                             base_url="https://api.perplexity.ai")
    elif p == "gemini":
        from google import genai
        _clients[p] = genai.Client()
    else:
        raise ValueError(f"unknown platform {p}")

MOCK_POOL = ["Café Athena", "Pita Pit", "Olive Café", "Zorba's", "Taverna Blu",
             "Saffron Thai", "The Fishery", "Kono's Café", "Draft Republic",
             "Mezé Greek Fusion", "Costa Brava", "Isola Pizza Bar"]

def ask(p, q, web):
    """Send the query as a consumer would (no system prompt). Returns (text, citations)."""
    if p == "mock":
        picks = random.sample(MOCK_POOL, random.randint(3, 6))
        return "Here are some options:\n" + "\n".join(
            f"{i+1}. **{n}** – a solid pick." for i, n in enumerate(picks)), []

    if p == "claude":
        kw = dict(model=MODELS[p], max_tokens=1500,
                  messages=[{"role": "user", "content": q}])
        if web:
            kw["tools"] = [{"type": CLAUDE_WEB_TOOL, "name": "web_search", "max_uses": 3}]
        r = _clients[p].messages.create(**kw)
        text = "\n".join(b.text for b in r.content if b.type == "text")
        cites = []
        for b in r.content:
            for c in (getattr(b, "citations", None) or []):
                if getattr(c, "url", None):
                    cites.append(c.url)
        return text, sorted(set(cites))

    if p in ("openai", "perplexity"):
        r = _clients[p].chat.completions.create(
            model=MODELS[p], messages=[{"role": "user", "content": q}])
        text = r.choices[0].message.content or ""
        extra = getattr(r, "model_extra", None) or {}
        cites = getattr(r, "citations", None) or extra.get("citations") or []
        if not cites and extra.get("search_results"):
            cites = [s.get("url") for s in extra["search_results"] if s.get("url")]
        return text, list(cites)

    if p == "gemini":
        from google.genai import types
        cfg = (types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])
               if web else None)
        r = _clients[p].models.generate_content(model=MODELS[p], contents=q, config=cfg)
        cites = []
        try:
            for ch in r.candidates[0].grounding_metadata.grounding_chunks or []:
                if ch.web:
                    cites.append(ch.web.title or ch.web.uri)
        except Exception:
            pass
        return r.text or "", cites

# ---------- extraction ----------
EXTRACT_PROMPT = """Below is an AI assistant's answer to the question: "{q}"

List every specific, named restaurant, bar, café or caterer the answer recommends or names, in the order they first appear. Exclude neighborhoods, dishes, cuisines and websites.
Return ONLY JSON: {{"restaurants": ["Name 1", "Name 2"]}}

ANSWER:
{a}"""

def regex_extract(text):
    names = re.findall(r"\*\*([^*\n]{2,60})\*\*", text)
    names += re.findall(r"^\s*\d+[.)]\s+([A-Z][^\n:–—\-(]{1,50})", text, re.M)
    out, seen = [], set()
    for n in names:
        n = n.strip(" :.-")
        if n and canon(n) not in seen:
            seen.add(canon(n)); out.append(n)
    return out

def parse_json_list(s):
    m = re.search(r"\{.*\}", s, re.S)
    return json.loads(m.group(0))["restaurants"] if m else None

def extract(q, text, platforms):
    if not text.strip():
        return []
    if "mock" in platforms:
        return regex_extract(text)
    prompt = EXTRACT_PROMPT.format(q=q, a=text[:12000])
    try:
        if os.getenv("ANTHROPIC_API_KEY"):
            import anthropic
            c = _clients.get("claude") or anthropic.Anthropic()
            r = c.messages.create(model=EXTRACT_MODEL, max_tokens=800,
                                  messages=[{"role": "user", "content": prompt}])
            names = parse_json_list("".join(
                getattr(b, "text", "") for b in r.content if getattr(b, "type", "") == "text"))
        else:
            from openai import OpenAI
            c = _clients.get("openai") or OpenAI()
            r = c.chat.completions.create(model=EXTRACT_MODEL_OPENAI,
                                          messages=[{"role": "user", "content": prompt}],
                                          response_format={"type": "json_object"})
            names = parse_json_list(r.choices[0].message.content)
        if names is not None:
            return [n for n in names if isinstance(n, str)]
    except Exception as e:
        print(f"  extract fallback ({e.__class__.__name__}: {str(e)[:120]})", file=sys.stderr)
    return regex_extract(text)

# ---------- run ----------
def run_one(p, row, sample, web, run_id, platforms):
    rec = dict(run_id=run_id, platform=p, model=MODELS[p], web=web,
               query_id=row["id"], query=row["query"], category=row["category"],
               geo=row["geo"], sample=sample, error="")
    text, cites, err = "", [], ""
    for attempt in range(3):
        try:
            text, cites = ask(p, row["query"], web)
            err = ""
            break
        except Exception as e:
            err = f"{e.__class__.__name__}: {e}"[:300]
            time.sleep(2 * 2 ** attempt)
    names = extract(row["query"], text, platforms) if not err else []
    pos = next((i + 1 for i, n in enumerate(names) if is_target(n)), None)
    mentioned = pos is not None or bool(TARGET_TEXT_RE.search(text))
    rec.update(timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
               mentioned=int(mentioned), position=pos or "",
               n_restaurants=len(names), restaurants=json.dumps(names),
               competitors=json.dumps([n for n in names if not is_target(n)]),
               citations=json.dumps(cites), response_text=text, error=err)
    return rec

def cmd_run(a):
    platforms = [p.strip() for p in a.platforms.split(",") if p.strip()]
    for p in platforms:
        init_client(p)
    rows = list(csv.DictReader(open(a.queries, encoding="utf-8")))
    if a.limit:
        rows = rows[:a.limit]
    run_id = a.run_id or datetime.now().strftime("%Y%m%d-%H%M") + "-" + uuid.uuid4().hex[:4]
    jobs = [(p, r, s) for p in platforms for r in rows for s in range(1, a.samples + 1)]
    print(f"run {run_id}: {len(jobs)} calls ({len(rows)} queries × {len(platforms)} platforms × {a.samples} samples)")

    DATA.mkdir(exist_ok=True)
    new = not RESULTS.exists()
    with open(RESULTS, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = [ex.submit(run_one, p, r, s, a.web, run_id, platforms) for p, r, s in jobs]
            for i, fut in enumerate(as_completed(futs), 1):
                rec = fut.result()
                w.writerow(rec); f.flush()
                flag = "ERR" if rec["error"] else ("✓ #" + str(rec["position"]) if rec["mentioned"] else "·")
                print(f"[{i}/{len(jobs)}] {rec['platform']:<10} {rec['query_id']} {flag}")
                if rec["error"] and i == 1:
                    print("   ↳", rec["error"][:200])
    print(f"\nsaved → {RESULTS}\nnext: python discovery.py score --run {run_id}")
    if not a.no_score:
        cmd_score(argparse.Namespace(run=run_id))

# ---------- score ----------
def load_run(run_id=None):
    rows = list(csv.DictReader(open(RESULTS, encoding="utf-8")))
    if not rows:
        sys.exit("no results yet")
    if not run_id or run_id == "latest":
        run_id = max(rows, key=lambda r: r["timestamp"])["run_id"]
    return run_id, [r for r in rows if r["run_id"] == run_id and not r["error"]]

def scorecard(rows):
    n = len(rows)
    if not n:
        return {}
    pos = [int(r["position"]) for r in rows if r["position"]]
    total_mentions, comp = 0, defaultdict(lambda: {"names": Counter(), "answers": 0, "positions": []})
    for r in rows:
        seen = set()
        for i, name in enumerate(json.loads(r["restaurants"]), 1):
            k = canon(name)
            if not k or k in seen:
                continue
            seen.add(k)
            comp[k]["names"][name] += 1
            comp[k]["answers"] += 1
            comp[k]["positions"].append(i)
            total_mentions += 1
        if r["mentioned"] == "1" and "cafe athena" not in seen:   # caught by text match only
            comp["cafe athena"]["names"][TARGET] += 1
            comp["cafe athena"]["answers"] += 1
            total_mentions += 1
    board = []
    for k, v in comp.items():
        board.append(dict(
            restaurant=TARGET if k == "cafe athena" else v["names"].most_common(1)[0][0],
            is_target=k == "cafe athena", answers=v["answers"],
            mention_rate=round(v["answers"] / n, 3),
            avg_position=round(sum(v["positions"]) / len(v["positions"]), 2) if v["positions"] else None,
            share_of_voice=round(v["answers"] / total_mentions, 3) if total_mentions else 0))
    board.sort(key=lambda x: -x["answers"])
    for i, b in enumerate(board, 1):
        b["sov_rank"] = i
    return dict(
        answers=n,
        mention_rate=round(sum(r["mentioned"] == "1" for r in rows) / n, 3),
        avg_position=round(sum(pos) / len(pos), 2) if pos else None,
        top3_rate=round(sum(p <= 3 for p in pos) / n, 3),
        share_of_voice=next((b["share_of_voice"] for b in board if b["is_target"]), 0),
        sov_rank=next((b["sov_rank"] for b in board if b["is_target"]), None),
        restaurants_seen=len(board), leaderboard=board)

def cmd_score(a):
    run_id, rows = load_run(a.run)
    if not rows:
        errs = [r["error"] for r in csv.DictReader(open(RESULTS, encoding="utf-8"))
                if r["run_id"] == run_id and r["error"]]
        print(f"\nAll calls in run {run_id} failed — nothing to score.")
        if errs:
            print("Error:", errs[0])
            if "auth" in errs[0].lower() or "api_key" in errs[0].lower() or "401" in errs[0]:
                print("→ Your API key is missing or wrong. Set a real one with export ... and rerun.")
        return
    out = {"run_id": run_id, "overall": scorecard(rows), "by_platform": {}, "by_category": {}}
    for key, field in (("by_platform", "platform"), ("by_category", "category")):
        groups = defaultdict(list)
        for r in rows:
            groups[r[field]].append(r)
        for g, rs in sorted(groups.items()):
            s = scorecard(rs)
            s.pop("leaderboard", None)
            out[key][g] = s
    # queries where Athena is missing — the input list for the Analysis stage
    lost = defaultdict(lambda: {"query": "", "category": "", "misses": 0, "answers": 0, "winners": Counter()})
    for r in rows:
        L = lost[r["query_id"]]
        L.update(query=r["query"], category=r["category"])
        L["answers"] += 1
        if r["mentioned"] != "1":
            L["misses"] += 1
            for c in json.loads(r["competitors"])[:3]:
                L["winners"][c] += 1
    out["lost_queries"] = sorted(
        [dict(query_id=q, query=v["query"], category=v["category"],
              miss_rate=round(v["misses"] / v["answers"], 2),
              top_winners=[w for w, _ in v["winners"].most_common(3)])
         for q, v in lost.items() if v["misses"]], key=lambda x: -x["miss_rate"])
    # most-cited sources (web-grounded platforms only)
    src = Counter()
    for r in rows:
        for c in json.loads(r["citations"] or "[]"):
            if c:
                src[re.sub(r"^https?://(www\.)?", "", c).split("/")[0]] += 1
    out["top_cited_sources"] = src.most_common(15)

    DATA.mkdir(exist_ok=True)
    (DATA / f"scorecard_{run_id}.json").write_text(json.dumps(out, indent=2))
    with open(DATA / f"leaderboard_{run_id}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out["overall"]["leaderboard"][0].keys()))
        w.writeheader(); w.writerows(out["overall"]["leaderboard"])

    o = out["overall"]
    print(f"\n=== Café Athena visibility — run {run_id} ({o['answers']} answers) ===")
    print(f"Mention rate {o['mention_rate']:.0%} | avg rank {o['avg_position']} | "
          f"top-3 {o['top3_rate']:.0%} | share of voice {o['share_of_voice']:.1%} "
          f"(#{o['sov_rank'] or '—'} of {o['restaurants_seen']})")
    print("\nBy platform:")
    for p, s in out["by_platform"].items():
        print(f"  {p:<11} mention {s['mention_rate']:.0%}  avg rank {s['avg_position']}")
    print("\nBy category:")
    for c, s in out["by_category"].items():
        print(f"  {c:<13} mention {s['mention_rate']:.0%}")
    print("\nTop competitors (share of voice):")
    for b in o["leaderboard"][:10]:
        tag = "  ← TARGET" if b["is_target"] else ""
        print(f"  {b['sov_rank']:>2}. {b['restaurant']:<28} {b['mention_rate']:.0%} of answers, "
              f"avg rank {b['avg_position']}{tag}")
    if out["top_cited_sources"]:
        print("\nMost-cited sources:", ", ".join(f"{d} ({n})" for d, n in out["top_cited_sources"][:8]))
    print(f"\nsaved → data/scorecard_{run_id}.json, data/leaderboard_{run_id}.csv")

# ---------- cli ----------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("--platforms", default="claude,openai,perplexity,gemini")
    r.add_argument("--queries", default="queries.csv")
    r.add_argument("--samples", type=int, default=1, help="repeats per query (answers vary)")
    r.add_argument("--web", action="store_true", help="live web search for Claude/Gemini")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--limit", type=int, default=0, help="only first N queries (smoke test)")
    r.add_argument("--run-id")
    r.add_argument("--no-score", action="store_true")
    s = sp.add_parser("score")
    s.add_argument("--run", default="latest")
    a = ap.parse_args()
    cmd_run(a) if a.cmd == "run" else cmd_score(a)
