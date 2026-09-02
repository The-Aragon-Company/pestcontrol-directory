"""Google Maps scraper + verifier for pest-control businesses.

Each run makes two passes over Google Maps:

  1. discover — walk a batch of the category x city target list. Every place
     found is either inserted (new) or written back over the row we already
     have, bumping refreshed_at. So crawling for growth doubles as maintenance.
  2. verify   — reopen the least-recently-verified listings by their Google
     CID, whether or not they still rank for any search. This is the only pass
     that can retire a business that quietly closed, because a dead place stops
     showing up in search results at all.

Pass 2 is what scraper/refresh.py used to do on its own; it now lives here.

Usage:
    python scraper/scrape.py                  # discover + verify
    python scraper/scrape.py --limit 5        # only first 5 jobs (smoke test)
    python scraper/scrape.py --verify 0       # discovery only
    python scraper/scrape.py --no-discover    # verification only
    python scraper/scrape.py --headful        # watch the browser
    python scraper/scrape.py --proxy http://user:pass@host:port

Grey-hat notes:
  - Datacenter IPs get blocked fast. Pass --proxy with a residential/mobile
    rotating endpoint for any real volume.
  - Human-ish delays + stealth are on by default. Don't crank concurrency.
  - Data is stored in data/pestcontrol.db (SQLite). Resumable: dedups on CID.
"""
import argparse
import asyncio
import os
import random
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402

from playwright.async_api import async_playwright  # noqa: E402

try:
    from playwright_stealth import stealth_async
except Exception:  # optional dependency
    async def stealth_async(page):  # type: ignore
        return None

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CLOSED_RE = re.compile(r"permanently closed", re.I)
TEMP_CLOSED_RE = re.compile(r"temporarily closed", re.I)


async def human_pause(a=0.8, b=2.2):
    await asyncio.sleep(random.uniform(a, b))


def parse_latlng(url: str):
    # detail urls embed !3d<lat>!4d<lng>
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url or "")
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url or "")
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def parse_cid(url: str):
    m = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", url or "")
    return m.group(1) if m else None


async def scrape_detail(page, category, city, state):
    """Pull fields from the currently-open place detail panel."""
    async def txt(sel):
        el = await page.query_selector(sel)
        return (await el.inner_text()).strip() if el else None

    async def attr(sel, a):
        el = await page.query_selector(sel)
        return await el.get_attribute(a) if el else None

    name = await txt("h1.DUwDvf") or await txt("h1")
    if not name:
        return None

    rating_raw = await txt('div.F7nice span[aria-hidden="true"]')
    reviews_raw = await attr('div.F7nice span[aria-label*="review"]', "aria-label") \
        or await txt('div.F7nice span[aria-label*="review"]')
    address = await attr('button[data-item-id="address"]', "aria-label")
    phone = await attr('button[data-item-id^="phone"]', "aria-label")
    website = await attr('a[data-item-id="authority"]', "href")
    url = page.url
    lat, lng = parse_latlng(url)

    def num(s, cast=float):
        if not s:
            return None
        m = re.search(r"[\d.,]+", s.replace(",", ""))
        try:
            return cast(m.group()) if m else None
        except ValueError:
            return None

    return {
        "name": name,
        "category": category,
        "city": city,
        "state": state,
        "address": (address or "").replace("Address: ", "") or None,
        "phone": (phone or "").replace("Phone: ", "") or None,
        "website": website,
        "lat": lat,
        "lng": lng,
        "rating": num(rating_raw, float),
        "reviews": num(reviews_raw, int),
        "hours": None,
        "maps_url": url,
        "cid": parse_cid(url),
    }


async def trading_status(page) -> str:
    """'closed' | 'temp' | 'open', read off the detail panel's own text.

    A seasonal or renovation pause is not a dead business, so 'temp' listings
    stay visible; only 'permanently closed' retires a row.
    """
    el = await page.query_selector('div[role="main"]')
    if not el:
        return "open"
    try:
        text = await el.inner_text()
    except Exception:
        return "open"
    if CLOSED_RE.search(text):
        return "closed"
    if TEMP_CLOSED_RE.search(text):
        return "temp"
    return "open"


async def dismiss_consent(page):
    """Click through the consent wall Google throws up in some regions."""
    for sel in ['button[aria-label*="Accept"]', 'form[action*="consent"] button']:
        btn = await page.query_selector(sel)
        if btn:
            await btn.click()
            await human_pause()


async def visit_place(page, category, city, state, conn, tally):
    """Record the place whose detail panel is open. Returns the outcome."""
    detail = await scrape_detail(page, category, city, state)
    if not detail:
        tally["skipped"] += 1
        return "skipped"

    if await trading_status(page) == "closed":
        # Retire it if we have it; never bring a dead business in as new.
        outcome = "closed" if db.retire_if_known(conn, detail) else "skipped"
    else:
        outcome = db.upsert_or_refresh(conn, detail)
    tally[outcome] += 1
    return outcome


async def scrape_query(page, query, category, city, state, conn, tally,
                       max_results=20):
    url = "https://www.google.com/maps/search/" + urllib.parse.quote(query)
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await human_pause(2, 4)
    await dismiss_consent(page)

    feed = await page.query_selector('div[role="feed"]')
    if not feed:
        # single result -> Maps jumped straight to a detail panel
        return 1 if await visit_place(
            page, category, city, state, conn, tally) == "new" else 0

    # scroll the results column to load more
    links = []
    for _ in range(12):
        cards = await page.query_selector_all('div[role="feed"] a.hfpxzc')
        found = [l for l in [await c.get_attribute("href") for c in cards] if l]
        prev = len(links)
        links = found
        if len(links) >= max_results or len(links) == prev:
            break
        await page.evaluate(
            'document.querySelector(`div[role="feed"]`).scrollBy(0, 2500)'
        )
        await human_pause(1.0, 2.0)

    added = 0
    for link in links[:max_results]:
        try:
            await page.goto(link, wait_until="domcontentloaded", timeout=45000)
            await human_pause(1.2, 2.6)
            if await visit_place(page, category, city, state, conn,
                                 tally) == "new":
                added += 1
        except Exception as e:
            print(f"    ! detail fail: {e}")
            continue
    return added


# ---------------------------------------------------------------- verify pass
def place_url(row) -> str:
    """Most stable URL for a place we've already seen.

    The `?cid=` form is keyed on Google's own place id and survives renames and
    moves; the stored maps_url is a positional detail URL and can rot. Prefer
    the former, fall back to the latter.
    """
    cid = row["cid"] or ""
    m = re.match(r"0x[0-9a-f]+:0x([0-9a-f]+)$", cid, re.I)
    if m:
        return f"https://maps.google.com/?cid={int(m.group(1), 16)}"
    if row["maps_url"]:
        return row["maps_url"]
    # last resort: search by name + city and take whatever Maps resolves to
    q = " ".join(filter(None, [row["name"], row["city"], row["state"]]))
    return "https://www.google.com/maps/search/" + urllib.parse.quote(q)


async def recheck(page, row):
    """Reopen one known place. Returns (outcome, detail_row_or_None).

    Outcomes: 'ok' | 'temp' | 'closed' | 'fail'.
    """
    await page.goto(place_url(row), wait_until="domcontentloaded", timeout=60000)
    await human_pause(1.5, 3.0)
    await dismiss_consent(page)

    # a dead cid drops us on a search feed rather than a place panel
    if await page.query_selector('div[role="feed"]'):
        return "fail", None

    detail = await scrape_detail(page, row["category"], row["city"], row["state"])
    if not detail:
        return "fail", None

    status = await trading_status(page)
    return ("ok" if status == "open" else status), detail


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="cap number of jobs")
    ap.add_argument("--max-results", type=int, default=20, help="per query")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--proxy", default=None, help="single proxy URL")
    ap.add_argument("--proxy-file", default=None,
                    help="file with one proxy URL per line; rotated per query")
    ap.add_argument("--rotate-every", type=int, default=8,
                    help="new proxy + fresh browser context every N queries")
    ap.add_argument("--allow-bare-ip", action="store_true",
                    help="DANGER: scrape from your real IP with no proxy")
    ap.add_argument("--offset", type=int, default=0,
                    help="skip the first N jobs (for sharding across CI runs)")
    ap.add_argument("--no-discover", action="store_true",
                    help="skip the discovery pass; only re-verify known rows")
    ap.add_argument("--verify", type=int, default=200,
                    help="stalest listings to re-verify after discovery (0=off)")
    ap.add_argument("--verify-offset", type=int, default=0,
                    help="skip the N stalest listings (for sharding)")
    ap.add_argument("--verify-rotate-every", type=int, default=25,
                    help="new proxy + fresh browser context every N listings")
    ap.add_argument("--max-fails", type=int, default=3,
                    help="consecutive failed re-checks before status='gone'")
    args = ap.parse_args()

    # In cloud CI the runner's IP is disposable (not your home IP), so bare-IP
    # is acceptable there. The guard still protects a laptop run.
    cloud_ci = os.environ.get("PCD_CLOUD_CI") == "1"

    # ---- IP-safety guard: never blast Google from the home IP by accident ----
    proxies = []
    if args.proxy_file and Path(args.proxy_file).exists():
        proxies = [l.strip() for l in Path(args.proxy_file).read_text().splitlines()
                   if l.strip() and not l.startswith("#")]
    elif args.proxy:
        proxies = [args.proxy]

    if not proxies and not args.allow_bare_ip and not cloud_ci:
        print(
            "REFUSING TO RUN: no proxy configured.\n"
            "  Scraping Google from your home IP risks a ban on that IP.\n"
            "  Fix one of these ways:\n"
            "    --proxy http://user:pass@host:port        (single rotating endpoint)\n"
            "    --proxy-file proxies.txt                   (pool, rotated per query)\n"
            "  Or, if you truly accept the risk on THIS machine's IP:\n"
            "    --allow-bare-ip\n"
            "  Safest of all: don't run this at all — use Apify's cloud scraper "
            "(your IP never touches Google). See README."
        )
        sys.exit(2)

    import targets
    conn = db.connect()
    job_list = []
    if not args.no_discover:
        job_list = list(targets.jobs())
        total_all = len(job_list)
        if args.offset:
            job_list = job_list[args.offset:]
        if args.limit:
            job_list = job_list[: args.limit]

    print(f"DB: {db.DB_PATH}")
    if job_list:
        print(f"Jobs: {len(job_list)} of {total_all}  "
              f"(offset {args.offset}, start count: {db.count(conn)})")
    else:
        print(f"Discovery: off  (start count: {db.count(conn)})")
    print(f"Verify: {args.verify or 'off'} stalest listings"
          f"{f' (offset {args.verify_offset})' if args.verify_offset else ''}")
    print(f"Proxies: {len(proxies) or 'NONE (bare IP!)'}  "
          f"rotate every {args.rotate_every} queries")

    tally = {"new": 0, "updated": 0, "same": 0, "closed": 0, "junk": 0,
             "skipped": 0, "gone": 0, "fail": 0, "temp": 0}

    async with async_playwright() as p:
        browser = None
        ctx = None
        page = None
        pidx = -1

        async def new_session(force_proxy_advance=True):
            nonlocal browser, ctx, page, pidx
            if ctx:
                await ctx.close()
            launch_kw = {"headless": not args.headful}
            if proxies:
                if force_proxy_advance:
                    pidx = (pidx + 1) % len(proxies)
                launch_kw["proxy"] = {"server": proxies[pidx]}
                print(f"  -> proxy [{pidx}] {proxies[pidx].split('@')[-1]}")
            # proxy is fixed at launch, so a rotation needs a fresh browser
            if browser is not None:
                await browser.close()
            browser = await p.chromium.launch(**launch_kw)
            ctx = await browser.new_context(
                user_agent=UA, viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = await ctx.new_page()
            await stealth_async(page)

        await new_session(force_proxy_advance=True)

        # ---- pass 1: discover (and re-verify everything it walks past) ------
        for i, (query, category, city, state) in enumerate(job_list, 1):
            if i > 1 and (i - 1) % args.rotate_every == 0 and proxies:
                await new_session(force_proxy_advance=True)
            try:
                n = await scrape_query(
                    page, query, category, city, state, conn, tally,
                    args.max_results
                )
                print(f"[{i}/{len(job_list)}] +{n:<3} {query}  (total={db.count(conn)})")
            except Exception as e:
                print(f"[{i}/{len(job_list)}] FAIL {query}: {e}")
                # an error mid-run often means a soft block -> rotate IP
                if proxies:
                    await new_session(force_proxy_advance=True)
            await human_pause(1.5, 3.5)

        # ---- pass 2: verify the stalest rows, ranked or not -----------------
        rows = db.stale_listings(conn, args.verify, args.verify_offset) \
            if args.verify else []
        if rows:
            print(f"\n--- verifying {len(rows)} stalest of {db.count(conn)} "
                  f"live listings ---")
            print(f"Staleness range: {rows[0]['last_seen']} .. "
                  f"{rows[-1]['last_seen']}")

        for i, row in enumerate(rows, 1):
            if i > 1 and (i - 1) % args.verify_rotate_every == 0 and proxies:
                await new_session(force_proxy_advance=True)
            label = f"[{i}/{len(rows)}] {row['name'][:38]:<38}"
            try:
                outcome, detail = await recheck(page, row)
            except Exception as e:
                outcome, detail = "fail", None
                print(f"{label} ERROR {type(e).__name__}: {e}")

            if outcome == "fail":
                tally["fail"] += 1
                if db.note_failure(conn, row["id"], args.max_fails):
                    tally["gone"] += 1
                    print(f"{label} GONE (retired after {args.max_fails} fails)")
                else:
                    print(f"{label} fail")
            elif outcome == "closed":
                db.mark_status(conn, row["id"], "closed")
                tally["closed"] += 1
                print(f"{label} CLOSED -> hidden")
            else:
                changed = db.apply_refresh(conn, row["id"], detail)
                if outcome == "temp":
                    tally["temp"] += 1
                if changed:
                    tally["updated"] += 1
                    print(f"{label} updated: {', '.join(changed)}")
                else:
                    tally["same"] += 1
                    print(f"{label} ok")

            await human_pause(1.5, 3.5)

        if browser:
            await browser.close()

    print("\n--- run summary ---")
    for k in ("new", "updated", "same", "closed", "gone", "temp", "fail",
              "junk", "skipped"):
        print(f"  {k:<8} {tally[k]}")
    print(f"Live listings: {db.count(conn)}  "
          f"(all statuses: {db.status_counts(conn)})")


if __name__ == "__main__":
    asyncio.run(main())
