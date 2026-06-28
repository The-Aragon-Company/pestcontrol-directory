# Pest Control Directory

A pest-control business directory — same playbook as mobiledetailing.io:
scrape Google Maps → store in DB → render thousands of SEO pages with Flask.

## Project layout

```
pestcontrol-directory/
├── scraper/            # data harvester (BUILT)
│   ├── scrape.py       # Playwright Google Maps scraper -> SQLite
│   ├── db.py           # schema + slugify + dedup upsert
│   ├── targets.py      # categories x cities to search
│   └── export.py       # dump DB to CSV/JSON
├── data/               # pestcontrol.db lives here (gitignored)
└── web/                # Flask app (NEXT step, not built yet)
```

## 1. Setup

```bash
cd pestcontrol-directory
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on mac/linux)
pip install -r requirements.txt
python -m playwright install chromium
```

## 2. Smoke test (5 queries, watch it run)

```bash
python scraper/scrape.py --limit 5 --headful
```

## 3. Full harvest

```bash
python scraper/scrape.py --max-results 20
# with a residential proxy (recommended for volume):
python scraper/scrape.py --proxy http://user:pass@host:port
```

Resumable — dedups on Google place CID, so re-running tops up the DB.

## 4. Export

```bash
python scraper/export.py     # writes data/listings.csv + listings.json
```

## Categories scraped
General Pest Control · Exterminators · Termite Control · Bed Bug Removal ·
Rodent Control · Mosquito Control · Wildlife Removal

## Cities — NATIONWIDE
Top 1,000 US cities by population, loaded from `data/us-cities.csv`.
1,000 cities × 7 categories = **7,000 search jobs**. CID dedup collapses the
metro overlap → tens of thousands of unique businesses, near-complete US coverage.

Tune scope with env vars:
```bash
PCD_MIN_POP=100000 python scraper/scrape.py     # only cities >100k (293 cities)
PCD_MAX_CITIES=300 python scraper/scrape.py      # largest 300 cities only
```

## Web app (BUILT)

```bash
pip install -r requirements-web.txt
python web/app.py            # http://127.0.0.1:5000
```
Routes: `/`, `/listing/<slug>`, `/<state>/<city>`, `/category/<slug>`, `/cities`,
`/categories`, `/search`, `/sitemap.xml`, `/robots.txt`, `POST /quote`.
Set domain / AdSense / GA in the `app.config` block at the top of `web/app.py`.

## SEO content (Gemini)
Unique per-page copy, generated once and cached in the DB's `content` table:
```bash
set GEMINI_API_KEY=your_key
python web/gen_content.py --kind category
python web/gen_content.py --kind city --limit 60
```
Or let the **generate-content** GitHub Action do it daily — add repo secret
`GEMINI_API_KEY` (Settings > Secrets > Actions).

## Data quality
```bash
python scraper/clean.py       # drop big-box chains + no-contact ghosts
```
New scrapes are filtered automatically (see `JUNK_NAMES` in `scraper/db.py`).

## Deploy (pick one — all free tier)
- **Render**: push repo, New > Blueprint (uses `render.yaml`). Easiest.
- **Railway / Fly.io**: uses the `Procfile` / `Dockerfile`.
- **Any VPS + Docker**: `docker build -t pcd . && docker run -p 8080:8080 pcd`
- Put **Cloudflare** in front for caching + the proxy look.

The scraped `data/pestcontrol.db` is committed back by the Actions workflows,
so the deploy host always has fresh data straight from the repo.

## Remaining polish
- [ ] Compile Tailwind to `/static/css/output.css` (now Play CDN — fine for dev)
- [ ] Wire `POST /quote` to email/CRM (currently just logs the lead)
- [ ] Point a real domain + fill AdSense/GA config

## Protecting your IP (READ THIS)

The scraper **refuses to run without a proxy** — it will not let you blast
Google from your home IP. Three safe paths:

### Safest: don't scrape yourself — use a cloud scraper
Your IP never touches Google at all.
- **Apify "Google Maps Scraper"** actor — runs on Apify's IPs. ~$5 free credit,
  then ~$0.50–$4 / 1,000 results. Full nationwide ≈ $30–80 one-time.
- **Outscraper / SerpApi** — same model, pay per result.

### Proxy pool (run this scraper, home IP hidden)
Put one proxy per line in `proxies.txt`, then:
```bash
python scraper/scrape.py --proxy-file proxies.txt --rotate-every 8
```
The script relaunches the browser through a new proxy every 8 queries, and
rotates immediately on any error (a soft block). Google only ever sees proxy
IPs; your home IP stays clean. Cheap residential providers: IPRoyal, Webshare,
Bright Data, Smartproxy. **Use residential/mobile** — datacenter proxies get
blocked almost as fast as a bare IP.

### Single rotating endpoint
Most providers give one "gateway" URL that rotates IP per request:
```bash
python scraper/scrape.py --proxy http://user:pass@gateway.provider.com:7777
```

### Last resort (NOT recommended)
```bash
python scraper/scrape.py --allow-bare-ip    # uses YOUR ip — ban risk
```

## Notes
Scraping Google Maps violates their ToS. Use rotating residential proxies,
keep concurrency low, and don't republish Google review *text* verbatim.
