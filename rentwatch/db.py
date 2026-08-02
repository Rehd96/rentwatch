import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'immobiliare',
    url TEXT,
    title TEXT,
    typology TEXT,
    price INTEGER,
    surface_m2 REAL,
    rooms TEXT,
    bathrooms TEXT,
    floor TEXT,
    elevator INTEGER,
    address TEXT,
    macrozone TEXT,
    microzone TEXT,
    latitude REAL,
    longitude REAL,
    agency TEXT,
    photo_url TEXT,
    description TEXT,
    is_new_construction INTEGER,
    luxury INTEGER,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    hidden INTEGER NOT NULL DEFAULT 0,
    liked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(is_active, price);
CREATE INDEX IF NOT EXISTS idx_listings_zone ON listings(macrozone);

CREATE TABLE IF NOT EXISTS price_history (
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    observed_at TEXT NOT NULL,
    price INTEGER,
    PRIMARY KEY (listing_id, observed_at)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    listings_seen INTEGER DEFAULT 0,
    new_listings INTEGER DEFAULT 0,
    price_changes INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'
);

-- Hearts, one row per person per listing. A separate table rather than more
-- columns on listings: two people looking at the same flat have their own
-- opinion of it, and both want to see the other's.
CREATE TABLE IF NOT EXISTS favourites (
    listing_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (listing_id, username)
);
CREATE INDEX IF NOT EXISTS idx_favourites_user ON favourites(username);

-- Telegram messages that came due during quiet hours. Drained by the first
-- run after the window, so a listing found at 3am is still announced at 8am
-- instead of being silently lost.
CREATE TABLE IF NOT EXISTS notify_queue (
    listing_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'new',
    queued_at TEXT NOT NULL,
    payload TEXT,
    PRIMARY KEY (listing_id, kind)
);
"""

LISTING_COLUMNS = [
    "url", "title", "typology", "price", "surface_m2", "rooms", "bathrooms",
    "floor", "elevator", "address", "macrozone", "microzone", "latitude",
    "longitude", "agency", "photo_url", "description",
    "is_new_construction", "luxury",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for DBs created before a column existed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
    for col in ("hidden", "liked"):
        if col not in cols:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def is_suspect(price, surface_m2, rooms) -> bool:
    """Heuristic: price is likely per-room/per-bed (student housing posted as
    a whole apartment: person price + full-flat surface). Whole-unit rents in
    Torino never go below ~7 €/m², so < 5 is a safe cutoff; the rooms check
    catches e.g. a "5-room flat" at €315/month (= €/posto letto)."""
    if not price:
        return False
    if surface_m2 and surface_m2 > 5 and price / surface_m2 < 5:
        return True
    try:
        n = int(str(rooms).rstrip("+"))
    except (TypeError, ValueError):
        return False
    return n >= 4 and price / n < 120


def upsert_listing(conn: sqlite3.Connection, listing: dict,
                   seen_at: str) -> tuple[str, int | None]:
    """Insert or refresh a listing.

    Returns (outcome, previous_price) where outcome is 'new', 'price_changed'
    or 'seen'. The old price comes back because the Telegram notifier reports
    drops as "€900 → €850" and re-querying it per listing would double the
    work of a 2000-row run.
    """
    row = conn.execute(
        "SELECT price FROM listings WHERE id = ?", (listing["id"],)
    ).fetchone()

    if row is None:
        cols = ["id", *LISTING_COLUMNS, "first_seen", "last_seen", "is_active"]
        values = [listing["id"], *[listing.get(c) for c in LISTING_COLUMNS], seen_at, seen_at, 1]
        conn.execute(
            f"INSERT INTO listings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            values,
        )
        conn.execute(
            "INSERT OR IGNORE INTO price_history (listing_id, observed_at, price) VALUES (?, ?, ?)",
            (listing["id"], seen_at, listing.get("price")),
        )
        return "new", None

    assignments = ", ".join(f"{c} = ?" for c in LISTING_COLUMNS)
    conn.execute(
        f"UPDATE listings SET {assignments}, last_seen = ?, is_active = 1 WHERE id = ?",
        [*[listing.get(c) for c in LISTING_COLUMNS], seen_at, listing["id"]],
    )
    if row["price"] != listing.get("price"):
        conn.execute(
            "INSERT OR IGNORE INTO price_history (listing_id, observed_at, price) VALUES (?, ?, ?)",
            (listing["id"], seen_at, listing.get("price")),
        )
        return "price_changed", row["price"]
    return "seen", row["price"]


def deactivate_unseen(conn: sqlite3.Connection, run_started: str) -> int:
    """Mark listings not seen during a completed full run as inactive."""
    cur = conn.execute(
        "UPDATE listings SET is_active = 0 WHERE is_active = 1 AND last_seen < ?",
        (run_started,),
    )
    return cur.rowcount


def adopt_legacy_likes(conn: sqlite3.Connection, username: str) -> int:
    """Give the pre-multi-user hearts to whoever was the only user.

    The old schema had one `liked` flag per listing with no idea who set it.
    Those hearts were somebody's, and the only honest guess is the account that
    existed at the time — the first one in the config. Runs once: after this
    the favourites table is no longer empty.
    """
    if not username:
        return 0
    already = conn.execute("SELECT COUNT(*) c FROM favourites").fetchone()["c"]
    if already:
        return 0
    cur = conn.execute(
        "INSERT OR IGNORE INTO favourites (listing_id, username, created_at)"
        " SELECT id, ?, ? FROM listings WHERE liked = 1",
        (username, now_iso()),
    )
    conn.commit()
    return cur.rowcount


def set_favourite(conn: sqlite3.Connection, listing_id: int, username: str,
                  value: bool) -> bool:
    """Add or remove one person's heart. False if the listing does not exist."""
    if not conn.execute("SELECT 1 FROM listings WHERE id = ?",
                        (listing_id,)).fetchone():
        return False
    if value:
        conn.execute(
            "INSERT OR IGNORE INTO favourites (listing_id, username, created_at)"
            " VALUES (?, ?, ?)", (listing_id, username, now_iso()))
    else:
        conn.execute("DELETE FROM favourites WHERE listing_id = ? AND username = ?",
                     (listing_id, username))
    # `liked` stays in sync so the Markdown report and any old query still work.
    conn.execute(
        "UPDATE listings SET liked = (SELECT COUNT(*) > 0 FROM favourites f"
        " WHERE f.listing_id = listings.id) WHERE id = ?", (listing_id,))
    conn.commit()
    return True


def favourites_by_listing(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """{listing_id: [username, ...]} for every hearted listing."""
    out: dict[int, list[str]] = {}
    for row in conn.execute(
            "SELECT listing_id, username FROM favourites ORDER BY username"):
        out.setdefault(row["listing_id"], []).append(row["username"])
    return out


def favourite_listings(conn: sqlite3.Connection, username: str | None = None,
                       limit: int = 50) -> list[dict]:
    """Hearted listings, newest heart first, each with `liked_by` attached."""
    if username:
        rows = conn.execute(
            "SELECT l.*, f.created_at AS hearted_at FROM listings l"
            " JOIN favourites f ON f.listing_id = l.id WHERE f.username = ?"
            " ORDER BY f.created_at DESC LIMIT ?", (username, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT l.*, MAX(f.created_at) AS hearted_at FROM listings l"
            " JOIN favourites f ON f.listing_id = l.id"
            " GROUP BY l.id ORDER BY hearted_at DESC LIMIT ?", (limit,)).fetchall()
    by_listing = favourites_by_listing(conn)
    listings = []
    for row in rows:
        listing = dict(row)
        listing["liked_by"] = by_listing.get(row["id"], [])
        listings.append(listing)
    return listings


def get_listing(conn: sqlite3.Connection, listing_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return dict(row) if row else None


def queue_notification(conn: sqlite3.Connection, listing_id: int,
                       kind: str = "new", payload: str | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO notify_queue (listing_id, kind, queued_at, payload)"
        " VALUES (?, ?, ?, ?)",
        (listing_id, kind, now_iso(), payload),
    )


def drain_notifications(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Take queued notifications off the queue and return them with their
    listing rows attached. Rows whose listing vanished are dropped."""
    queued = conn.execute(
        "SELECT listing_id, kind, payload FROM notify_queue ORDER BY queued_at LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for row in queued:
        listing = get_listing(conn, row["listing_id"])
        conn.execute(
            "DELETE FROM notify_queue WHERE listing_id = ? AND kind = ?",
            (row["listing_id"], row["kind"]),
        )
        if listing:
            out.append({"listing": listing, "kind": row["kind"], "payload": row["payload"]})
    conn.commit()
    return out


def queue_size(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) c FROM notify_queue").fetchone()["c"]


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO scrape_runs (started_at) VALUES (?)", (now_iso(),))
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, *, seen: int, new: int,
               price_changes: int, status: str) -> None:
    conn.execute(
        "UPDATE scrape_runs SET finished_at = ?, listings_seen = ?, new_listings = ?,"
        " price_changes = ?, status = ? WHERE id = ?",
        (now_iso(), seen, new, price_changes, status, run_id),
    )
