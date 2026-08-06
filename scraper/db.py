"""SQLite store for scraped pest-control listings."""
import sqlite3
import re
import unicodedata
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pestcontrol.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    slug        TEXT UNIQUE NOT NULL,
    category    TEXT,
    city        TEXT,
    state       TEXT,
    address     TEXT,
    phone       TEXT,
    website     TEXT,
    lat         REAL,
    lng         REAL,
    rating      REAL,
    reviews     INTEGER,
    hours       TEXT,           -- JSON blob
    maps_url    TEXT,
    cid         TEXT,           -- google place CID, used for dedup
    scraped_at  TEXT DEFAULT (datetime('now')),
    -- maintenance columns (see scraper/refresh.py)
    status       TEXT NOT NULL DEFAULT 'active',  -- active | closed | gone
    refreshed_at TEXT,           -- last successful re-check, NULL = never
    refresh_fails INTEGER NOT NULL DEFAULT 0      -- consecutive failed re-checks
);
-- Gemini-generated SEO copy, cached so we never call the API per pageview.
CREATE TABLE IF NOT EXISTS content (
    key          TEXT PRIMARY KEY,   -- e.g. city:austin-tx, category:termite-control
    kind         TEXT,               -- city | category | listing
    intro        TEXT,
    faq          TEXT,               -- JSON [{q,a},...]
    generated_at TEXT DEFAULT (datetime('now'))
);

-- Long-form informational guides (top-of-funnel SEO content).
CREATE TABLE IF NOT EXISTS guides (
    slug         TEXT PRIMARY KEY,
    title        TEXT,
    description  TEXT,
    category     TEXT,               -- related service category (for cross-links)
    body         TEXT,               -- JSON {intro, sections:[{heading,content}], faq:[{q,a}]}
    generated_at TEXT DEFAULT (datetime('now'))
);
"""

# Indexes run after _migrate() so they can reference columns added to an
# already-existing table (CREATE TABLE IF NOT EXISTS won't add them).
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_city  ON listings(city, state);
CREATE INDEX IF NOT EXISTS idx_cat   ON listings(category);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cid ON listings(cid) WHERE cid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_stale  ON listings(status, refreshed_at, scraped_at);
"""

# Columns added after the original schema shipped: name -> ALTER definition.
# Defaults must be constants (SQLite restriction on ADD COLUMN).
MIGRATIONS = {
    "status":        "TEXT NOT NULL DEFAULT 'active'",
    "refreshed_at":  "TEXT",
    "refresh_fails": "INTEGER NOT NULL DEFAULT 0",
}

# Every listing the public site is allowed to show. Closed/gone businesses stay
# in the DB (auditable, reversible) but are filtered out of every query.
ACTIVE = "status = 'active'"


# Big-box stores / irrelevant chains that pollute "pest control" map searches.
JUNK_NAMES = (
    "home depot", "lowe's", "lowes", "walmart", "ace hardware", "true value",
    "tractor supply", "menards", "target", "costco", "amazon", "harbor freight",
    "do it best", "family dollar", "dollar general", "walgreens", "cvs",
)


def is_junk(name: str) -> bool:
    n = (name or "").lower()
    return any(j in n for j in JUNK_NAMES)


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


def _migrate(conn):
    """Add any maintenance columns missing from an older DB file."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
    for col, decl in MIGRATIONS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {decl}")
    conn.commit()


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(INDEXES)
    return conn


def upsert(conn, row: dict) -> bool:
    """Insert a listing. Returns True if new, False if duplicate (by cid or slug)."""
    if is_junk(row.get("name")):
        return False
    base = slugify(row.get("name", "") or "unknown")
    if row.get("city"):
        base = f"{base}-{slugify(row['city'])}"
    slug, n = base, 1
    cur = conn.cursor()

    # dedup on CID first
    if row.get("cid"):
        cur.execute("SELECT 1 FROM listings WHERE cid = ?", (row["cid"],))
        if cur.fetchone():
            return False

    # dedup on name+city+state (catches dupes with missing/different CIDs,
    # e.g. the same business returned under two category searches)
    cur.execute("SELECT 1 FROM listings WHERE lower(name)=lower(?) "
                "AND city=? AND state=?",
                (row.get("name"), row.get("city"), row.get("state")))
    if cur.fetchone():
        return False

    # ensure unique slug
    while True:
        cur.execute("SELECT 1 FROM listings WHERE slug = ?", (slug,))
        if not cur.fetchone():
            break
        n += 1
        slug = f"{base}-{n}"

    cur.execute(
        """INSERT INTO listings
           (name, slug, category, city, state, address, phone, website,
            lat, lng, rating, reviews, hours, maps_url, cid)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row.get("name"), slug, row.get("category"), row.get("city"),
            row.get("state"), row.get("address"), row.get("phone"),
            row.get("website"), row.get("lat"), row.get("lng"),
            row.get("rating"), row.get("reviews"), row.get("hours"),
            row.get("maps_url"), row.get("cid"),
        ),
    )
    conn.commit()
    return True


def count(conn) -> int:
    """Live listings — what the site actually shows."""
    return conn.execute(
        f"SELECT COUNT(*) FROM listings WHERE {ACTIVE}").fetchone()[0]


def count_all(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]


def status_counts(conn) -> dict:
    return dict(conn.execute(
        "SELECT status, COUNT(*) FROM listings GROUP BY status").fetchall())


# ------------------------------------------------------------------ refresh
# Fields a re-check is allowed to overwrite. Deliberately excluded:
#   slug             — permanent URL, must stay stable for SEO
#   category         — derived from the search query, not the place page
#   city / state     — derived from the target job; Maps address text is noisier
#   cid              — the identity we matched on
REFRESHABLE = ("name", "address", "phone", "website", "lat", "lng",
               "rating", "reviews", "hours", "maps_url")


def stale_listings(conn, limit: int, offset: int = 0):
    """Active listings, least-recently-verified first.

    Never-refreshed rows sort by their original scrape date, so the backlog
    drains oldest-first and no listing can starve.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""SELECT id, name, slug, category, city, state, maps_url, cid,
                   COALESCE(refreshed_at, scraped_at) AS last_seen
              FROM listings
             WHERE {ACTIVE}
          ORDER BY last_seen ASC, id ASC
             LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
    conn.row_factory = None
    return rows


def apply_refresh(conn, listing_id: int, row: dict) -> list:
    """Write fresh field values. Returns the names of fields that changed."""
    cur = conn.cursor()
    old = cur.execute(
        f"SELECT {','.join(REFRESHABLE)} FROM listings WHERE id=?",
        (listing_id,)).fetchone()
    before = dict(zip(REFRESHABLE, old))

    changed, sets, vals = [], [], []
    for field in REFRESHABLE:
        new = row.get(field)
        # a missing value means the panel didn't render that field this time --
        # don't let a flaky read wipe good data
        if new in (None, ""):
            continue
        if before[field] != new:
            changed.append(field)
        sets.append(f"{field}=?")
        vals.append(new)

    sets += ["refreshed_at=datetime('now')", "refresh_fails=0"]
    cur.execute(f"UPDATE listings SET {','.join(sets)} WHERE id=?",
                vals + [listing_id])
    conn.commit()
    return changed


def mark_status(conn, listing_id: int, status: str):
    """Flag a listing closed/gone. Kept in the DB, hidden from the site."""
    conn.execute(
        "UPDATE listings SET status=?, refreshed_at=datetime('now') WHERE id=?",
        (status, listing_id))
    conn.commit()


def note_failure(conn, listing_id: int, max_fails: int = 3) -> bool:
    """Record a failed re-check. Returns True once the row is retired as gone.

    One bad page load is noise; three in a row across three weekly runs means
    the place URL no longer resolves.
    """
    cur = conn.cursor()
    cur.execute("UPDATE listings SET refresh_fails = refresh_fails + 1 "
                "WHERE id=?", (listing_id,))
    fails = cur.execute("SELECT refresh_fails FROM listings WHERE id=?",
                        (listing_id,)).fetchone()[0]
    retired = fails >= max_fails
    if retired:
        cur.execute("UPDATE listings SET status='gone', "
                    "refreshed_at=datetime('now') WHERE id=?", (listing_id,))
    conn.commit()
    return retired
