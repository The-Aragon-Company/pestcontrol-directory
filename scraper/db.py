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
    scraped_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_city  ON listings(city, state);
CREATE INDEX IF NOT EXISTS idx_cat   ON listings(category);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cid ON listings(cid) WHERE cid IS NOT NULL;

-- Gemini-generated SEO copy, cached so we never call the API per pageview.
CREATE TABLE IF NOT EXISTS content (
    key          TEXT PRIMARY KEY,   -- e.g. city:austin-tx, category:termite-control
    kind         TEXT,               -- city | category | listing
    intro        TEXT,
    faq          TEXT,               -- JSON [{q,a},...]
    generated_at TEXT DEFAULT (datetime('now'))
);
"""


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def upsert(conn, row: dict) -> bool:
    """Insert a listing. Returns True if new, False if duplicate (by cid or slug)."""
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
    return conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
