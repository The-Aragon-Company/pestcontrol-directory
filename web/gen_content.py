"""Generate unique SEO copy with an LLM, cached in the `content` table.

Run ONCE per page (cached forever). Avoids thin/duplicate content across the
thousands of city/category pages — the thing that makes or breaks a directory
in Google.

Provider: Groq (fast, generous free tier) by default; falls back to Gemini.
Setup:
    set GROQ_API_KEY=your_key         (Windows)   /   export on mac+linux
    python web/gen_content.py --kind category          # all categories
    python web/gen_content.py --kind city --limit 50   # first 50 cities
    python web/gen_content.py --kind city --only austin-tx

Never commits the key.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
import db as dbmod  # noqa: E402

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

SYS = ("You write concise, genuinely useful, non-spammy copy for a pest-control "
       "directory website. Plain, trustworthy tone. No emojis, no hype, no made-up "
       "statistics or fake guarantees. Return ONLY valid minified JSON.")


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # model sometimes wraps/append stray text — grab the first JSON object
        obj, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
        return obj


def _groq(prompt: str) -> dict:
    body = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": prompt}],
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {GROQ_KEY}",
                 "User-Agent": "pestcontrol-directory/1.0"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return _extract_json(data["choices"][0]["message"]["content"])


def _gemini(prompt: str) -> dict:
    endpoint = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")
    body = {
        "system_instruction": {"parts": [{"text": SYS}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7,
                             "response_mime_type": "application/json"},
    }
    req = urllib.request.Request(
        endpoint, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return _extract_json(data["candidates"][0]["content"]["parts"][0]["text"])


def gemini(prompt: str) -> dict:
    """Provider-agnostic generate with 429 backoff: Groq first, else Gemini."""
    fn = _groq if GROQ_KEY else _gemini
    if not (GROQ_KEY or GEMINI_KEY):
        raise RuntimeError("No GROQ_API_KEY or GEMINI_API_KEY set")
    for attempt in range(5):
        try:
            return fn(prompt)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                wait = int(e.headers.get("retry-after", 0)) or (10 * (attempt + 1))
                print(f"    429 rate limit — waiting {wait}s")
                time.sleep(wait)
                continue
            raise


def prompt_category(name, n):
    return (f"Service: '{name}' (pest control). {n} providers listed. "
            "Return JSON {\"intro\": <120-180 word paragraph explaining what this "
            "service covers, typical signs you need it, and what to look for in a "
            "provider>, \"faq\": [3 items of {\"q\":..., \"a\":<40-70 words>}]}.")


def prompt_city(city, state, n, cats):
    return (f"City: {city}, {state}. {n} pest-control businesses listed "
            f"(services: {', '.join(cats)}). Return JSON {{\"intro\": <120-180 word "
            "paragraph about choosing pest control in this specific city — mention "
            "common local pests/climate factors generally, what reviews to check, "
            "and getting quotes>, \"faq\": [3 items of {\"q\":..., \"a\":<40-70 words>}]}.")


def save(conn, key, kind, data):
    conn.execute(
        "INSERT OR REPLACE INTO content(key, kind, intro, faq, generated_at) "
        "VALUES (?,?,?,?,datetime('now'))",
        (key, kind, data.get("intro", ""), json.dumps(data.get("faq", []))))
    conn.commit()


def already(conn, key):
    return conn.execute("SELECT 1 FROM content WHERE key=?", (key,)).fetchone()


def run(kind, limit, only, force):
    if not (GROQ_KEY or GEMINI_KEY):
        print("ERROR: set GROQ_API_KEY (or GEMINI_API_KEY) env var first.")
        sys.exit(2)
    print(f"provider: {'Groq/'+GROQ_MODEL if GROQ_KEY else 'Gemini/'+GEMINI_MODEL}")
    conn = dbmod.connect()
    done = 0

    if kind == "category":
        rows = conn.execute("SELECT category, COUNT(*) n FROM listings "
                            "WHERE category IS NOT NULL GROUP BY category").fetchall()
        for name, n in rows:
            key = f"category:{dbmod.slugify(name)}"
            if only and dbmod.slugify(name) != only:
                continue
            if already(conn, key) and not force:
                print("skip", key); continue
            try:
                save(conn, key, "category", gemini(prompt_category(name, n)))
                done += 1; print("ok  ", key)
            except Exception as e:
                print("FAIL", key, e)
            time.sleep(1.2)

    elif kind == "city":
        rows = conn.execute(
            "SELECT city, state, COUNT(*) n, GROUP_CONCAT(DISTINCT category) "
            "FROM listings GROUP BY city, state ORDER BY n DESC").fetchall()
        for city, state, n, cats in rows:
            key = f"city:{dbmod.slugify(city)}-{state.lower()}"
            if only and key.split(':')[1] != only:
                continue
            if already(conn, key) and not force:
                print("skip", key); continue
            cl = [c for c in (cats or "").split(",") if c][:6]
            try:
                save(conn, key, "city", gemini(prompt_city(city, state, n, cl)))
                done += 1; print("ok  ", key)
            except Exception as e:
                print("FAIL", key, e)
            if limit and done >= limit:
                break
            time.sleep(1.2)

    print(f"\nGenerated {done} content blocks.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["city", "category"], required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default=None, help="single slug, e.g. austin-tx")
    ap.add_argument("--force", action="store_true", help="regenerate even if cached")
    a = ap.parse_args()
    run(a.kind, a.limit, a.only, a.force)
