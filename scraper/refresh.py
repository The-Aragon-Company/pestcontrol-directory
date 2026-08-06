"""Re-check listings we already have, instead of hunting for new ones.

The directory is built out (~24k listings), so the job now is accuracy, not
growth: phone numbers change, sites move, ratings drift, businesses close.
Each run takes the least-recently-verified slice of the DB, reopens each place
on Google Maps, and writes back what changed.

Usage:
    python scraper/refresh.py                    # default batch (250)
    python scraper/refresh.py --limit 25         # smoke test
    python scraper/refresh.py --headful          # watch the browser
    python scraper/refresh.py --proxy http://user:pass@host:port

Outcomes per listing:
    updated  — fields rewritten, refreshed_at bumped
    same     — verified, nothing changed
    closed   — Maps says permanently closed -> status='closed', hidden
    fail     — page didn't resolve; 3 consecutive fails -> status='gone'

Same IP-safety rules as scrape.py: refuses to run bare-IP off a laptop unless
you pass --allow-bare-ip. In CI (PCD_CLOUD_CI=1) the runner IP is disposable.
"""
import argparse
import asyncio
import os
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import scrape  # noqa: E402  (reuse the detail parser + UA/pauses)

from playwright.async_api import async_playwright  # noqa: E402

CLOSED_RE = re.compile(r"permanently closed", re.I)
TEMP_CLOSED_RE = re.compile(r"temporarily closed", re.I)


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


async def panel_text(page) -> str:
    el = await page.query_selector('div[role="main"]')
    if not el:
        return ""
    try:
        return await el.inner_text()
    except Exception:
        return ""


async def recheck(page, row):
    """Reopen one place. Returns (outcome, detail_row_or_None)."""
    await page.goto(place_url(row), wait_until="domcontentloaded", timeout=60000)
    await scrape.human_pause(1.5, 3.0)

    for sel in ['button[aria-label*="Accept"]', 'form[action*="consent"] button']:
        btn = await page.query_selector(sel)
        if btn:
            await btn.click()
            await scrape.human_pause()

    # a dead cid drops us on a search feed rather than a place panel
    if await page.query_selector('div[role="feed"]'):
        return "fail", None

    detail = await scrape.scrape_detail(
        page, row["category"], row["city"], row["state"])
    if not detail:
        return "fail", None

    text = await panel_text(page)
    if CLOSED_RE.search(text):
        return "closed", detail
    if TEMP_CLOSED_RE.search(text):
        # a seasonal/renovation pause is not a dead business — keep it listed
        return "temp", detail
    return "ok", detail


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=250,
                    help="listings to re-check this run")
    ap.add_argument("--offset", type=int, default=0,
                    help="skip the N stalest (for sharding across runs)")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", default=None,
                    help="file with one proxy URL per line; rotated per batch")
    ap.add_argument("--rotate-every", type=int, default=25,
                    help="new proxy + fresh browser context every N listings")
    ap.add_argument("--allow-bare-ip", action="store_true",
                    help="DANGER: re-check from your real IP with no proxy")
    ap.add_argument("--max-fails", type=int, default=3,
                    help="consecutive failed re-checks before status='gone'")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    args = ap.parse_args()

    cloud_ci = os.environ.get("PCD_CLOUD_CI") == "1"
    proxies = []
    if args.proxy_file and Path(args.proxy_file).exists():
        proxies = [l.strip() for l in Path(args.proxy_file).read_text().splitlines()
                   if l.strip() and not l.startswith("#")]
    elif args.proxy:
        proxies = [args.proxy]

    if not proxies and not args.allow_bare_ip and not cloud_ci:
        print("REFUSING TO RUN: no proxy configured (see scrape.py notes).\n"
              "  --proxy / --proxy-file, or --allow-bare-ip to accept the risk.")
        sys.exit(2)

    conn = db.connect()
    rows = db.stale_listings(conn, args.limit, args.offset)
    if not rows:
        print("Nothing to refresh.")
        return

    print(f"DB: {db.DB_PATH}")
    print(f"Re-checking {len(rows)} of {db.count(conn)} live listings"
          f"{' (DRY RUN)' if args.dry_run else ''}")
    print(f"Staleness range: {rows[0]['last_seen']} .. {rows[-1]['last_seen']}")
    print(f"Proxies: {len(proxies) or 'NONE (bare IP!)'}")

    tally = {"updated": 0, "same": 0, "closed": 0, "temp": 0, "fail": 0,
             "gone": 0}

    async with async_playwright() as p:
        browser = ctx = page = None
        pidx = -1

        async def new_session():
            nonlocal browser, ctx, page, pidx
            if ctx:
                await ctx.close()
            launch_kw = {"headless": not args.headful}
            if proxies:
                pidx = (pidx + 1) % len(proxies)
                launch_kw["proxy"] = {"server": proxies[pidx]}
                print(f"  -> proxy [{pidx}] {proxies[pidx].split('@')[-1]}")
            if browser:
                await browser.close()
            browser = await p.chromium.launch(**launch_kw)
            ctx = await browser.new_context(
                user_agent=scrape.UA, viewport={"width": 1280, "height": 900},
                locale="en-US")
            page = await ctx.new_page()
            await scrape.stealth_async(page)

        await new_session()

        for i, row in enumerate(rows, 1):
            if i > 1 and (i - 1) % args.rotate_every == 0 and proxies:
                await new_session()
            label = f"[{i}/{len(rows)}] {row['name'][:38]:<38}"
            try:
                outcome, detail = await recheck(page, row)
            except Exception as e:
                outcome, detail = "fail", None
                print(f"{label} ERROR {type(e).__name__}: {e}")

            if args.dry_run:
                print(f"{label} {outcome}")
                tally[outcome if outcome != "ok" else "same"] += 1
                await scrape.human_pause(1.5, 3.5)
                continue

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

            await scrape.human_pause(1.5, 3.5)

        if browser:
            await browser.close()

    print("\n--- refresh summary ---")
    for k in ("updated", "same", "closed", "gone", "fail", "temp"):
        print(f"  {k:<8} {tally[k]}")
    print(f"Live listings: {db.count(conn)}  "
          f"(all statuses: {db.status_counts(conn)})")


if __name__ == "__main__":
    asyncio.run(main())
