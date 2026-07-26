"""Generate a Markdown snapshot of the DB (reports/overview.md).

Runs after every scrape so the latest market picture is a plain .md file —
readable from a phone via GitHub/any Markdown viewer without the dashboard.
"""

import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import PROJECT_ROOT

REPORT_PATH = PROJECT_ROOT / "reports" / "overview.md"
MAX_ROWS = 30


def _md_listing_row(r: sqlite3.Row) -> str:
    ppm2 = f"{r['price'] / r['surface_m2']:.1f}" if r["price"] and r["surface_m2"] else "—"
    zone = r["macrozone"] or "—"
    price = f"€ {r['price']}" if r["price"] else "—"
    m2 = f"{r['surface_m2']:.0f}" if r["surface_m2"] else "—"
    return (f"| [{(r['title'] or str(r['id']))[:60]}]({r['url']}) | {zone} | "
            f"{price} | {m2} | {ppm2} | {r['rooms'] or '—'} |")


def generate_report(db_path: str, out_path: Path = REPORT_PATH) -> Path:
    conn = db.connect(db_path)
    try:
        active = conn.execute(
            "SELECT price, surface_m2, rooms, macrozone FROM listings WHERE is_active = 1"
        ).fetchall()
        prices = [r["price"] for r in active if r["price"]]
        ppm2 = [r["price"] / r["surface_m2"] for r in active
                if r["price"] and r["surface_m2"] and r["surface_m2"] > 5
                and not db.is_suspect(r["price"], r["surface_m2"], r["rooms"])]

        now = datetime.now(timezone.utc).astimezone()
        lines = [
            "# Affitti Torino — panoramica",
            "",
            f"_Aggiornato: {now.strftime('%d/%m/%Y %H:%M')}_",
            "",
            f"- **Annunci attivi:** {len(active)}",
            f"- **Canone mediano:** € {round(statistics.median(prices)) if prices else '—'}/mese",
            f"- **€/m² mediano:** {round(statistics.median(ppm2), 1) if ppm2 else '—'}",
            "",
        ]

        liked = conn.execute("""
            SELECT * FROM listings WHERE liked = 1
            ORDER BY is_active DESC, last_seen DESC
        """).fetchall()
        if liked:
            lines += [
                f"## Preferiti ({len(liked)})",
                "",
                "| Annuncio | Zona | Prezzo | m² | Stato |",
                "|---|---|---|---|---|",
            ]
            for r in liked:
                gone = f"❌ rimosso ({r['last_seen'][8:10]}/{r['last_seen'][5:7]})"
                stato = "✅ attivo" if r["is_active"] else gone
                m2 = f"{r['surface_m2']:.0f}" if r["surface_m2"] else "—"
                lines.append(
                    f"| [{(r['title'] or str(r['id']))[:60]}]({r['url']}) | {r['macrozone'] or '—'} | "
                    f"€ {r['price'] or '—'} | {m2} | {stato} |")
            lines.append("")

        new = [r for r in conn.execute("""
            SELECT * FROM listings WHERE is_active = 1 AND hidden = 0
            ORDER BY first_seen DESC, price ASC
        """) if not db.is_suspect(r["price"], r["surface_m2"], r["rooms"])][:MAX_ROWS]
        lines += [
            f"## Ultimi annunci ({len(new)})",
            "",
            "| Annuncio | Zona | Prezzo | m² | €/m² | Locali |",
            "|---|---|---|---|---|---|",
            *[_md_listing_row(r) for r in new],
            "",
        ]

        drops = [r for r in conn.execute("""
            SELECT l.*,
                   (SELECT ph.price FROM price_history ph WHERE ph.listing_id = l.id
                    ORDER BY ph.observed_at ASC LIMIT 1) AS initial_price
            FROM listings l
            WHERE l.is_active = 1 AND l.hidden = 0 AND l.price IS NOT NULL
              AND initial_price > l.price
            ORDER BY (initial_price - l.price) DESC
        """) if not db.is_suspect(r["price"], r["surface_m2"], r["rooms"])][:MAX_ROWS]
        if drops:
            lines += [
                f"## Cali di prezzo ({len(drops)})",
                "",
                "| Annuncio | Zona | Prezzo | Prima | m² | €/m² |",
                "|---|---|---|---|---|---|",
            ]
            for r in drops:
                ppm2_v = f"{r['price'] / r['surface_m2']:.1f}" if r["surface_m2"] else "—"
                m2 = f"{r['surface_m2']:.0f}" if r["surface_m2"] else "—"
                lines.append(
                    f"| [{(r['title'] or str(r['id']))[:60]}]({r['url']}) | {r['macrozone'] or '—'} | "
                    f"**€ {r['price']}** | ~~€ {r['initial_price']}~~ | {m2} | {ppm2_v} |")
            lines.append("")

        zones: dict[str, dict] = {}
        for r in active:
            z = r["macrozone"] or "N/D"
            zones.setdefault(z, {"prices": [], "ppm2": []})
            if r["price"]:
                zones[z]["prices"].append(r["price"])
                if r["surface_m2"] and r["surface_m2"] > 5 \
                        and not db.is_suspect(r["price"], r["surface_m2"], r["rooms"]):
                    zones[z]["ppm2"].append(r["price"] / r["surface_m2"])
        lines += [
            "## Zone (mediane)",
            "",
            "| Zona | Annunci | Canone | €/m² |",
            "|---|---|---|---|",
        ]
        for z, v in sorted(zones.items(), key=lambda kv: -len(kv[1]["prices"])):
            if not v["prices"]:
                continue
            zp = round(statistics.median(v["prices"]))
            zm = round(statistics.median(v["ppm2"]), 1) if v["ppm2"] else "—"
            lines.append(f"| {z} | {len(v['prices'])} | € {zp} | {zm} |")
        lines.append("")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines), encoding="utf-8")
        return out_path
    finally:
        conn.close()
