"""Local dashboard: FastAPI serving a single-page overview of the listings DB."""

import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from . import db

STATIC_DIR = Path(__file__).parent / "static"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _days_since(iso: str) -> int:
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def create_app(cfg: dict) -> FastAPI:
    app = FastAPI(title="rentwatch")
    db_path = cfg["db_path"]
    db.connect(db_path).close()  # ensure schema/migrations before serving

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/listings")
    def listings(include_inactive: bool = False, include_hidden: bool = False):
        conn = _connect(db_path)
        try:
            conds = []
            if not include_inactive:
                conds.append("l.is_active = 1")
            if not include_hidden:
                conds.append("l.hidden = 0")
            where = f"WHERE {' AND '.join(conds)}" if conds else ""
            rows = conn.execute(f"""
                SELECT l.id, l.url, l.title, l.typology, l.price, l.surface_m2,
                       l.rooms, l.bathrooms, l.floor, l.elevator, l.address,
                       l.macrozone, l.microzone, l.latitude, l.longitude,
                       l.agency, l.photo_url, l.first_seen, l.last_seen, l.is_active,
                       l.hidden, l.liked,
                       (SELECT COUNT(*) FROM price_history ph WHERE ph.listing_id = l.id) AS n_prices,
                       (SELECT ph.price FROM price_history ph WHERE ph.listing_id = l.id
                        ORDER BY ph.observed_at ASC LIMIT 1) AS initial_price
                FROM listings l {where}
            """).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["ppm2"] = round(r["price"] / r["surface_m2"], 1) \
                    if r["price"] and r["surface_m2"] else None
                d["days_on_market"] = _days_since(r["first_seen"])
                d["price_delta"] = (r["price"] - r["initial_price"]) \
                    if r["price"] and r["initial_price"] and r["n_prices"] > 1 else 0
                d["suspect"] = db.is_suspect(r["price"], r["surface_m2"], r["rooms"])
                out.append(d)
            return out
        finally:
            conn.close()

    @app.get("/api/overview")
    def overview():
        conn = _connect(db_path)
        try:
            active = conn.execute(
                "SELECT price, surface_m2, rooms, macrozone, first_seen FROM listings WHERE is_active = 1"
            ).fetchall()
            prices = [r["price"] for r in active if r["price"]]
            # per-room "student" prices would drag the €/m² medians down
            ppm2 = [r["price"] / r["surface_m2"] for r in active
                    if r["price"] and r["surface_m2"] and r["surface_m2"] > 5
                    and not db.is_suspect(r["price"], r["surface_m2"], r["rooms"])]
            new_7d = sum(1 for r in active if _days_since(r["first_seen"]) <= 7)

            zones: dict[str, dict] = {}
            for r in active:
                z = r["macrozone"] or "N/D"
                zones.setdefault(z, {"prices": [], "ppm2": []})
                if r["price"]:
                    zones[z]["prices"].append(r["price"])
                    if r["surface_m2"] and r["surface_m2"] > 5 \
                            and not db.is_suspect(r["price"], r["surface_m2"], r["rooms"]):
                        zones[z]["ppm2"].append(r["price"] / r["surface_m2"])
            zone_stats = sorted(
                ({"zone": z,
                  "count": len(v["prices"]),
                  "median_price": round(statistics.median(v["prices"])) if v["prices"] else None,
                  "median_ppm2": round(statistics.median(v["ppm2"]), 1) if v["ppm2"] else None}
                 for z, v in zones.items()),
                key=lambda x: -(x["median_ppm2"] or 0),
            )

            last_run = conn.execute(
                "SELECT finished_at, listings_seen, new_listings, status FROM scrape_runs "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return {
                "active_count": len(active),
                "new_7d": new_7d,
                "median_price": round(statistics.median(prices)) if prices else None,
                "median_ppm2": round(statistics.median(ppm2), 1) if ppm2 else None,
                "zones": zone_stats,
                "last_run": dict(last_run) if last_run else None,
            }
        finally:
            conn.close()

    def _set_flag(listing_id: int, column: str, value: bool):
        conn = _connect(db_path)
        try:
            cur = conn.execute(
                f"UPDATE listings SET {column} = ? WHERE id = ?",
                (1 if value else 0, listing_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return JSONResponse(status_code=404, content={"error": "unknown listing"})
            return {"id": listing_id, column: value}
        finally:
            conn.close()

    @app.post("/api/listings/{listing_id}/hidden")
    def set_hidden(listing_id: int, value: bool = True):
        return _set_flag(listing_id, "hidden", value)

    @app.post("/api/listings/{listing_id}/liked")
    def set_liked(listing_id: int, value: bool = True):
        return _set_flag(listing_id, "liked", value)

    @app.get("/api/price-history/{listing_id}")
    def price_history(listing_id: int):
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT observed_at, price FROM price_history WHERE listing_id = ? "
                "ORDER BY observed_at", (listing_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @app.exception_handler(Exception)
    async def on_error(request, exc):
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return app
