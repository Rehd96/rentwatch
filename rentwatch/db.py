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


def upsert_listing(conn: sqlite3.Connection, listing: dict, seen_at: str) -> str:
    """Insert or refresh a listing. Returns 'new', 'price_changed' or 'seen'."""
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
        return "new"

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
        return "price_changed"
    return "seen"


def deactivate_unseen(conn: sqlite3.Connection, run_started: str) -> int:
    """Mark listings not seen during a completed full run as inactive."""
    cur = conn.execute(
        "UPDATE listings SET is_active = 0 WHERE is_active = 1 AND last_seen < ?",
        (run_started,),
    )
    return cur.rowcount


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
