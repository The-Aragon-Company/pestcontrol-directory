"""Extract the authored guide pages into template-ready content.

The guides arrive as 19 standalone HTML files, each carrying its own <head>,
CSS, masthead, footer and sticky call bar. The site renders them through
templates/guide.html on base.html instead, so this strips the per-file chrome
and splits what's left into two pieces per guide:

    web/templates/guides/<slug>.html   the prose (h2/p/ul/tables/CTAs/FAQ)
    web/guides/<slug>.json             title, dek, answer, related, JSON-LD

Plus web/guides/_hub.json for the /guides hub (hero + the three card sections).

The prose keeps the authored markup but lands in templates/ because it is
included as a Jinja partial: the phone number the authored CTAs hardcode is
swapped for {{ CTA_PHONE }}, so changing the number in config changes it
everywhere instead of leaving 36 stale copies.

Things the template now owns, so they are dropped here: the breadcrumb, the
on-this-page nav (regenerated from the prose's <h2 id>), the BreadcrumbList
JSON-LD, and the shared CSS (web/static/css/guides.css). Absolute
nationalpestdirectory.com URLs inside the remaining JSON-LD become __DOMAIN__,
which app.py swaps for the configured DOMAIN at render time.

    python scripts/import_guides.py ~/Downloads/guides

Re-run whenever the authored guides change — it rewrites web/guides/.
"""
import argparse
import html as htmllib
import json
import re
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
DEST = WEB / "guides"                      # metadata (JSON)
PROSE = WEB / "templates" / "guides"       # prose partials (Jinja)

AUTHORED_HOST = "https://nationalpestdirectory.com"
# What the authored CTAs hardcode, swapped for the app's CTA_PHONE config.
AUTHORED_TEL = "+18663380533"
AUTHORED_TEL_DISPLAY = "(866) 338-0533"

# Guides link to each other by filename; the template needs live routes.
REL_LINK = re.compile(r'href="([a-z0-9][a-z0-9-]*)\.html"')


def _route(m):
    slug = m.group(1)
    return 'href="/guides"' if slug == "index" else f'href="/guides/{slug}"'


def _phone(html):
    """Hardcoded number -> config, so one setting drives every CTA."""
    n = html.count(AUTHORED_TEL) + html.count(AUTHORED_TEL_DISPLAY)
    html = html.replace(f"tel:{AUTHORED_TEL}", "tel:{{ CTA_PHONE }}")
    return html.replace(AUTHORED_TEL_DISPLAY, "{{ CTA_PHONE_DISPLAY }}"), n


def _one(pattern, html, what, src, flags=re.S):
    m = re.search(pattern, html, flags)
    if not m:
        sys.exit(f"{src}: could not find {what}")
    return m.group(1).strip()


def _jsonld(html, src):
    """Every ld+json block, flattened out of @graph, minus BreadcrumbList."""
    out = []
    for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                          html, re.S):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.exit(f"{src}: invalid JSON-LD ({e})")
        for node in obj.pop("@graph", [obj]):
            if node.get("@type") == "BreadcrumbList":
                continue      # template emits this, with the real DOMAIN
            node.pop("@context", None)
            out.append(node)
    return json.loads(json.dumps(out).replace(AUTHORED_HOST, "__DOMAIN__"))


def _text(s):
    """Entities -> characters. Fields the template prints as text (title, meta
    description, eyebrow) get escaped by Jinja on the way out, so leaving the
    authored &amp;/&#x27; in place would render them doubled."""
    return htmllib.unescape(s)


def _head(html, src):
    return {
        "title": _text(_one(r"<title>(.*?)</title>", html, "<title>", src)),
        "description": _text(_one(r'<meta name="description" content="(.*?)">',
                                  html, "meta description", src)),
    }


def guide(src):
    html = REL_LINK.sub(_route, src.read_bytes().decode("utf-8"))
    meta = _one(r'<div class="meta">(.*?)</div>', html, "meta line", src)
    prose = _one(r'<main class="body" id="main">(.*?)</main>', html,
                 "main.body", src)
    # "Keep reading" is navigation — kept as data, rebuilt by the template.
    related = [
        {"slug": s, "kind": _text(k), "title": t}
        for s, k, t in re.findall(
            r'<a class="rcard" href="/guides/([^"]+)">'
            r'<span class="k">(.*?)</span><span class="t">(.*?)</span></a>',
            prose)
    ]
    prose = re.sub(r'\s*<section class="related">.*?</section>\s*$', "\n",
                   prose, flags=re.S)
    prose, phones = _phone(prose)

    data = _head(html, src) | {
        "slug": src.stem,
        "kind": _text(_one(r'<span class="eyebrow">(.*?)</span>', html,
                           "eyebrow", src)),
        "h1": _one(r'<h1 class="title[^"]*">(.*?)</h1>', html, "h1", src),
        "long_title": 'class="title long"' in html,
        "dek": _one(r'<p class="dek">(.*?)</p>', html, "dek", src),
        "read_time": _one(r"<b>(.*?)</b>", meta, "read time", src),
        "words": _one(r"<span>([\d,]+) words</span>", meta, "word count", src),
        "answer": _one(r'<div class="answer">.*?<p>(.*?)</p>', html,
                       "answer box", src),
        # h2s carry ids already; regenerating beats hand-maintaining the nav
        "toc": [{"id": i, "label": re.sub(r"<[^>]+>", "", lbl).strip()}
                for i, lbl in re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', prose,
                                         re.S)],
        "related": related,
        "jsonld": _jsonld(html, src),
    }
    if not data["toc"]:
        sys.exit(f"{src}: no <h2 id> found — nothing to build the TOC from")
    return data, prose.strip() + "\n", phones


def hub(src):
    html = REL_LINK.sub(_route, src.read_bytes().decode("utf-8"))
    sections = []
    for block in re.findall(r'<section class="sec">(.*?)</section>', html, re.S):
        cards = [
            {"slug": s, "featured": "feat" in cls, "kind": _text(k),
             "title": t, "desc": d, "read_time": rt}
            for cls, s, k, t, d, rt in re.findall(
                r'<a class="ccard([^"]*)" href="/guides/([^"]+)">'
                r'<span class="k">(.*?)</span><h3>(.*?)</h3><p>(.*?)</p>'
                r'<span class="rt">(.*?)</span></a>', block, re.S)
        ]
        sections.append({
            "h2": _one(r"<h2>(.*?)</h2>", block, "section heading", src),
            "note": _one(r'<p class="n">(.*?)</p>', block, "section note", src),
            "cards": cards,
        })
    if not sections:
        sys.exit(f"{src}: no section.sec card groups found")
    return _head(html, src) | {
        "h1": _one(r"<section class=\"hero\">\s*<h1>(.*?)</h1>", html,
                   "hero h1", src),
        "dek": " ".join(_one(r'<section class="hero">.*?<p>(.*?)</p>', html,
                             "hero copy", src).split()),
        "sections": sections,
        "jsonld": _jsonld(html, src),
    }


def run(src):
    files = sorted(src.glob("*.html"))
    if not files:
        sys.exit(f"no .html files in {src}")
    if not (src / "index.html").exists():
        sys.exit(f"{src} has no index.html (the /guides hub page)")

    for d in (DEST, PROSE):
        d.mkdir(parents=True, exist_ok=True)
        for stale in list(d.glob("*.html")) + list(d.glob("*.json")):
            stale.unlink()

    def write_json(name, obj):
        (DEST / name).write_bytes(
            (json.dumps(obj, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))

    slugs = set()
    for f in files:
        if f.name == "index.html":
            continue
        data, prose, phones = guide(f)
        write_json(f"{f.stem}.json", data)
        (PROSE / f.name).write_bytes(prose.encode("utf-8"))
        slugs.add(f.stem)
        print(f"  {f.stem:<45} {len(data['toc']):>2} sections  "
              f"{len(data['related'])} related  {len(data['jsonld'])} JSON-LD  "
              f"{phones} phone")

    h = hub(src / "index.html")
    write_json("_hub.json", h)
    carded = {c["slug"] for s in h["sections"] for c in s["cards"]}
    print(f"\n  _hub.json  {len(h['sections'])} sections, {len(carded)} cards")

    # A card or "keep reading" link pointing at a slug we did not write would
    # 404 in production, so fail the import instead.
    linked = carded | {r["slug"] for f in DEST.glob("*.json")
                       if f.stem != "_hub"
                       for r in json.loads(f.read_text("utf-8"))["related"]}
    if linked - slugs:
        sys.exit(f"\nlinks to missing guides: {sorted(linked - slugs)}")
    if slugs - carded:
        print(f"  !! not linked from the hub: {sorted(slugs - carded)}")
    print(f"\n{len(slugs)} guides\n  metadata -> {DEST}\n  prose    -> {PROSE}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path, help="folder of authored guide .html files")
    run(ap.parse_args().src.expanduser())
