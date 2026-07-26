"""Optional Telegram notifications for new listings."""

import logging

from curl_cffi import requests

log = logging.getLogger(__name__)

MAX_PER_RUN = 20


def format_listing(listing: dict) -> str:
    price = f"€ {listing['price']}/mese" if listing.get("price") else "prezzo n.d."
    surface = f"{listing['surface_m2']:.0f} m²" if listing.get("surface_m2") else "m² n.d."
    per_m2 = ""
    if listing.get("price") and listing.get("surface_m2"):
        per_m2 = f" ({listing['price'] / listing['surface_m2']:.1f} €/m²)"
    zone = " · ".join(x for x in (listing.get("macrozone"), listing.get("microzone")) if x)
    lines = [
        f"🏠 {listing.get('title') or 'Nuovo annuncio'}",
        f"💶 {price} · {surface}{per_m2}",
    ]
    if zone:
        lines.append(f"📍 {zone}")
    lines.append(listing["url"])
    return "\n".join(lines)


def send_new_listings(telegram_cfg: dict, listings: list[dict]) -> int:
    """Send one message per new listing (capped). Returns messages sent."""
    if not telegram_cfg.get("enabled") or not listings:
        return 0
    token, chat_id = telegram_cfg.get("bot_token"), telegram_cfg.get("chat_id")
    if not token or not chat_id:
        log.warning("telegram enabled but bot_token/chat_id missing")
        return 0

    to_send = listings[:MAX_PER_RUN]
    sent = 0
    for listing in to_send:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": format_listing(listing),
                  "disable_web_page_preview": False},
            timeout=15,
        )
        if r.status_code == 200:
            sent += 1
        else:
            log.warning("telegram send failed: %s %s", r.status_code, r.text[:200])
    if len(listings) > MAX_PER_RUN:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id,
                  "text": f"… e altri {len(listings) - MAX_PER_RUN} nuovi annunci. "
                          f"Apri la dashboard per vederli tutti."},
            timeout=15,
        )
    return sent
