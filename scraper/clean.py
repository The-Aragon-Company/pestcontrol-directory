"""Purge junk rows (big-box chains, no-contact ghosts) from an existing DB."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db


def main():
    conn = db.connect()
    before = db.count(conn)
    rows = conn.execute("SELECT id, name FROM listings").fetchall()
    junk = [r[0] for r in rows if db.is_junk(r[1])]
    if junk:
        conn.executemany("DELETE FROM listings WHERE id=?", [(i,) for i in junk])
    # drop ghosts: no phone AND no website (useless to a visitor)
    ghosts = conn.execute(
        "DELETE FROM listings WHERE (phone IS NULL OR phone='') "
        "AND (website IS NULL OR website='')").rowcount

    # de-dupe same business (name+city+state); keep the richest record
    # (still live, then most reviews, then has website/phone, then lowest id)
    dupes = conn.execute("""
        SELECT id FROM listings WHERE id NOT IN (
          SELECT id FROM (
            SELECT id, ROW_NUMBER() OVER (
              PARTITION BY lower(name), city, state
              ORDER BY (status = 'active') DESC,
                       COALESCE(reviews,0) DESC,
                       (website IS NOT NULL) DESC,
                       (phone IS NOT NULL) DESC, id ASC) rn
            FROM listings
          ) WHERE rn = 1
        )""").fetchall()
    if dupes:
        conn.executemany("DELETE FROM listings WHERE id=?",
                         [(r[0],) for r in dupes])
    conn.commit()
    print(f"Removed {len(junk)} chains + {ghosts} no-contact + {len(dupes)} "
          f"duplicates. {before} -> {db.count(conn)} live "
          f"({db.status_counts(conn)})")


if __name__ == "__main__":
    main()
