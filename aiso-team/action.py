#!/usr/bin/env python3
"""
AISO Action — generate the fixes, then publish them only with human approval.

  python action.py generate     # drafts every artifact from the latest analysis
  python action.py review       # approve / skip each draft (human in the loop)
  python action.py fill         # fill [CONFIRM] gaps with facts from cafeathena.com itself
  python action.py undo <file>  # unpublish one artifact (every action is reversible)

drafts/  → generated, waiting for approval
site/    → approved + "published": a preview site with the schema embedded
actions_log.csv → who approved what, when (audit trail for the demo)
Needs: ANTHROPIC_API_KEY
"""
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
import csv, glob, html, json, os, re, shutil, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import anthropic

DATA, DRAFTS, SITE = Path("data"), Path("drafts"), Path("site")
LOG = Path("actions_log.csv")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
client = anthropic.Anthropic()

FACTS = """VERIFIED FACTS — Café Athena (use only these; anything else = [CONFIRM: ...] placeholder):
- Greek and Mediterranean restaurant and bar, 1846 Garnet Ave, San Diego, CA 92109 (Pacific Beach). Website cafeathena.com
- Open since 1990
- Hours: Sun–Thu 11:00 AM–9:00 PM; Fri–Sat 11:00 AM–9:30 PM
- Daily happy hour 3–6 PM with tapas-style small plates
- Vegan, vegetarian and gluten-free options
- Full bar with Greek-mythology-themed cocktails
- Catering, group dining and private events"""

STYLE = """Write so AI assistants can quote and cite it: plain factual sentences, specific nouns, the
restaurant name + "Pacific Beach, San Diego" stated explicitly, one idea per sentence, headings that
mirror how people ask (e.g. "Where can I get gluten-free Greek food in Pacific Beach?"). Warm but not
salesy. Never invent prices, phone numbers, dish names, capacities, awards or reviews — write
[CONFIRM: what's needed] instead. Output only the artifact, no preamble."""

ARTIFACTS = {
    "schema.jsonld": "schema.org JSON-LD for cafeathena.com. Return ONLY valid JSON: an object with "
        '"@context":"https://schema.org" and "@graph" containing (1) a Restaurant with name, url, address '
        "(PostalAddress), servesCuisine, openingHoursSpecification for the exact hours, acceptsReservations, "
        "hasMenu, amenityFeature entries (happy hour 3–6 PM, private events, catering, gluten-free/vegan/vegetarian "
        "options, full bar), foundingDate 1990, and string placeholders like \"[CONFIRM: phone]\" where unknown; "
        "(2) a FAQPage with 8 Question/Answer pairs targeting the lost queries.",
    "faq.md": "An FAQ page (markdown, 12–15 questions). Each question is phrased the way a San Diego customer "
        "would ask an AI assistant and maps to one of the LOST QUERIES; each answer is 2–3 sentences.",
    "about.md": "A rewritten 'About' page (markdown, ~250 words) that states, in the first two sentences, what "
        "Café Athena is, where it is and what it's known for, and covers happy hour, dietary options, cocktails, "
        "catering and private events.",
    "catering.md": "A catering landing page (markdown, ~350 words) targeting 'Greek catering in San Diego' and "
        "'Mediterranean catering for an office lunch in San Diego', with an FAQ section of 4 questions.",
    "private-events.md": "A private events & group dining landing page (markdown, ~350 words) targeting rehearsal "
        "dinners, birthdays, team dinners, baby showers and graduations in Pacific Beach, with a 4-question FAQ.",
    "gluten-free-vegan.md": "A dietary landing page (markdown, ~350 words): 'Gluten-free, vegan & vegetarian Greek "
        "food in Pacific Beach', explaining how Greek/Mediterranean cuisine suits these diets, with a 4-question FAQ.",
    "happy-hour.md": "A happy hour landing page (markdown, ~300 words): 'Happy hour in Pacific Beach: 3–6 PM daily, "
        "tapas-style Greek small plates and mythology-themed cocktails near Garnet Ave', with a 3-question FAQ.",
    "outreach.md": "Ready-to-send outreach (markdown), one section per item, each with To / Subject / Body: "
        "(1) listing request to findmeglutenfree.com, (2) venue listing for Peerspace and Tagvenue (one shared draft), "
        "(3) Yelp + Google Business Profile update checklist (attributes, categories, Q&A, photos — a checklist, not an email), "
        "(4) pitch to a San Diego food writer for a 'best Greek in San Diego' or 'best happy hours in PB' roundup. "
        "Keep emails under 150 words. Use the TOP CITED SOURCES to justify priorities.",
}


def text_of(r):
    return "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", "") == "text")

def latest(pattern):
    files = sorted(glob.glob(str(DATA / pattern)), key=os.path.getmtime)
    return json.load(open(files[-1])) if files else None

def log(action, name, note=""):
    new = not LOG.exists()
    with open(LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "action", "artifact", "note"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), action, name, note])


# ---------- generate ----------
def context():
    an, sc = latest("analysis_*.json"), latest("scorecard_*.json")
    if not sc:
        sys.exit("Run discovery.py first.")
    ctx = {"lost_queries": [f"{q['query']} (winners: {', '.join(q['top_winners'])})" for q in sc["lost_queries"][:25]],
           "top_cited_sources": sc["top_cited_sources"][:10],
           "mention_rate_by_category": {c: s["mention_rate"] for c, s in sc["by_category"].items()}}
    if an:
        ctx["gaps"] = an["diagnosis"].get("gaps", [])
        tgt = next((a for a in an["audits"] if a.get("is_target")), {})
        ctx["current_site"] = {k: tgt.get("onsite", {}).get(k) for k in
                               ("title", "meta_description", "schema_types", "signals", "pages_found")}
    return ctx

def generate_one(name, brief, ctx):
    prompt = (f"{FACTS}\n\n{STYLE}\n\nCONTEXT FROM OUR AI-SEARCH AUDIT:\n{json.dumps(ctx, ensure_ascii=False)}"
              f"\n\nTASK: {brief}")
    r = client.messages.create(model=MODEL, max_tokens=5000, messages=[{"role": "user", "content": prompt}])
    out = text_of(r).strip()
    if name.endswith(".jsonld"):
        m = re.search(r"\{.*\}", out, re.S)
        out = json.dumps(json.loads(m.group(0)), indent=2, ensure_ascii=False)  # validate
    else:
        out = re.sub(r"^```\w*\n|\n```$", "", out)
    (DRAFTS / name).write_text(out)
    return name, len(out)

def cmd_generate():
    DRAFTS.mkdir(exist_ok=True)
    ctx = context()
    print(f"Generating {len(ARTIFACTS)} artifacts…")
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(generate_one, n, b, ctx) for n, b in ARTIFACTS.items()]
        for f in futs:
            try:
                n, size = f.result()
                print(f"  ✓ drafts/{n} ({size:,} chars)")
                log("generated", n)
            except Exception as e:
                print(f"  ✗ failed: {e.__class__.__name__}: {str(e)[:150]}")
    print("\nNext: open the drafts/ folder to read them, then run:  python3 action.py review")


# ---------- fill placeholders from the owner's own website ----------
FILL_PROMPT = """Below is the current text of cafeathena.com, then a page we drafted for the restaurant.
Replace each [CONFIRM: ...] placeholder ONLY with facts the website text actually states (dish names,
drinks, menu sections, phone, prices, policies). Rewrite the sentence naturally around the fact.
If the website does not state it, keep the [CONFIRM: ...] placeholder exactly as it is.
Never invent anything. Keep everything else unchanged. Return only the full updated page.

WEBSITE TEXT:
{site}

DRAFT PAGE ({name}):
{page}"""

def cmd_fill():
    from analysis import fetch, visible_text
    site = []
    for p in ("", "/about", "/menu", "/happy-hour", "/catering", "/events"):
        h = fetch("https://www.cafeathena.com" + p)
        if h:
            site.append(visible_text(h))
    site = " ".join(site)[:60000]
    if len(site) < 300:
        sys.exit("Couldn't read cafeathena.com.")
    pages = [p for p in sorted(DRAFTS.glob("*")) if p.name != "outreach.md"]
    def one(p):
        before = p.read_text().count("[CONFIRM")
        r = client.messages.create(model=MODEL, max_tokens=8000, messages=[{"role": "user",
            "content": FILL_PROMPT.format(site=site, name=p.name, page=p.read_text())}])
        out = re.sub(r"^```\w*\n|\n```$", "", text_of(r).strip())
        if p.suffix == ".jsonld":
            out = json.dumps(json.loads(re.search(r"\{.*\}", out, re.S).group(0)), indent=2, ensure_ascii=False)
        p.write_text(out)
        if (SITE / p.name).exists():  # already approved → update the published copy too
            (SITE / p.name).write_text(out)
        after = out.count("[CONFIRM")
        log("filled-from-own-website", p.name, f"placeholders {before}->{after}")
        return p.name, before, after
    print("Filling placeholders with facts from cafeathena.com…")
    with ThreadPoolExecutor(max_workers=4) as ex:
        for f in [ex.submit(one, p) for p in pages]:
            try:
                n, b, a = f.result()
                print(f"  ✓ {n}: {b} → {a} placeholders left")
            except Exception as e:
                print(f"  ✗ {e.__class__.__name__}: {str(e)[:120]}")
    if SITE.exists():
        build_site()
    print("\nRemaining [CONFIRM] items are things only the owner can answer.")


# ---------- review / publish ----------
def md_to_html(md):
    out, in_list = [], False
    for line in md.splitlines():
        s = html.escape(line.strip())
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        if re.match(r"[-*] ", line.strip()):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{s[2:]}</li>"); continue
        if in_list:
            out.append("</ul>"); in_list = False
        m = re.match(r"(#{1,4}) (.*)", s)
        out.append(f"<h{len(m.group(1))}>{m.group(2)}</h{len(m.group(1))}>" if m else (f"<p>{s}</p>" if s else ""))
    if in_list:
        out.append("</ul>")
    return "\n".join(out)

def build_site():
    pages = sorted(p for p in SITE.glob("*.md"))
    schema = SITE / "schema.jsonld"
    ld = f'<script type="application/ld+json">\n{schema.read_text()}\n</script>' if schema.exists() else ""
    nav = " · ".join(f'<a href="#{p.stem}">{p.stem.replace("-", " ").title()}</a>' for p in pages)
    body = "\n".join(f'<section id="{p.stem}">{md_to_html(p.read_text())}</section><hr>' for p in pages)
    (SITE / "index.html").write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Café Athena — Greek Restaurant & Bar in Pacific Beach, San Diego</title>
<meta name="description" content="Greek and Mediterranean restaurant and bar on Garnet Ave in Pacific Beach since 1990. Daily 3–6 PM happy hour, gluten-free and vegan options, catering and private events.">
{ld}
<style>body{{font:16px/1.6 Georgia,serif;max-width:760px;margin:40px auto;padding:0 16px;color:#222}}
h1,h2,h3{{font-family:Helvetica,sans-serif}}nav{{margin-bottom:24px}}a{{color:#1f4e79}}
.badge{{background:#e8f0e8;padding:6px 10px;border-radius:6px;font:13px Helvetica,sans-serif}}</style></head>
<body><p class="badge">Preview of approved AISO changes — {len(pages)} pages{', schema.org markup embedded' if ld else ''}</p>
<nav>{nav}</nav>{body}</body></html>""")

def cmd_review():
    SITE.mkdir(exist_ok=True)
    drafts = sorted(DRAFTS.glob("*"))
    if not drafts:
        sys.exit("No drafts — run: python3 action.py generate")
    for d in drafts:
        if (SITE / d.name).exists():
            continue
        lines = d.read_text().splitlines()
        print("\n" + "=" * 70 + f"\n{d.name}  ({len(lines)} lines)\n" + "=" * 70)
        print("\n".join(lines[:25]) + ("\n…" if len(lines) > 25 else ""))
        placeholders = re.findall(r"\[CONFIRM:[^\]]*\]", d.read_text())
        if placeholders:
            print(f"\n⚠ {len(placeholders)} item(s) need the owner to confirm: {', '.join(sorted(set(placeholders))[:5])}")
        ans = input("\nPublish this? [y]es / [n]o / [q]uit: ").strip().lower()
        if ans == "q":
            break
        if ans == "y":
            shutil.copy(d, SITE / d.name)
            log("approved+published", d.name, f"{len(placeholders)} placeholders")
            print(f"  ✓ published → site/{d.name}")
        else:
            log("skipped", d.name)
    build_site()
    print("\nPreview site built → site/index.html  (open it in your browser)")

def cmd_undo(name):
    p = SITE / name
    if p.exists():
        p.unlink(); log("unpublished", name); build_site()
        print(f"Removed site/{name}; preview rebuilt.")
    else:
        print(f"site/{name} isn't published.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "generate":
        cmd_generate()
    elif cmd == "review":
        cmd_review()
    elif cmd == "fill":
        cmd_fill()
    elif cmd == "undo" and len(sys.argv) > 2:
        cmd_undo(sys.argv[2])
    else:
        print(__doc__)
