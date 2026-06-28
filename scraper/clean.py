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
    conn.commit()
    print(f"Removed {len(junk)} chains + {ghosts} no-contact rows. "
          f"{before} -> {db.count(conn)}")


if __name__ == "__main__":
    main()
