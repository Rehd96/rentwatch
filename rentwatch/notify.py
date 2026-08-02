"""Telegram notifications for new listings and price drops.

Everything here is driven by the [telegram] table in the config, which the
dashboard settings page writes: which listings are worth a message, what the
message says, and when the phone is allowed to buzz.
"""

import logging
from datetime import datetime

from curl_cffi import requests

from . import db

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

PLACEHOLDERS = [
    "title", "price", "surface", "ppm2", "rooms", "floor",
    "zone", "address", "agency", "url", "old_price", "delta",
]

DEFAULT_TEMPLATE = "🏠 {title}\n💶 {price} · {surface}{ppm2}\n📍 {zone}\n{url}"


class _Blanks(dict):
    """Unknown placeholders render empty instead of raising — the template is
    user input from a web form, and a typo there must not kill the scrape."""

    def __missing__(self, key):
        return ""


# ── the HTTP bit ─────────────────────────────────────────────────────────────

def call(token: str, method: str, payload: dict, timeout: int = 20) -> dict | None:
    """One Telegram API call. Returns the 'result' object, or None on failure."""
    if not token:
        return None
    try:
        r = requests.post(API.format(token=token, method=method),
                          json=payload, timeout=timeout)
    except Exception as e:                      # network flake, DNS, timeout
        log.warning("telegram %s failed: %s", method, e)
        return None
    if r.status_code != 200:
        log.warning("telegram %s: HTTP %s %s", method, r.status_code, r.text[:200])
        return None
    body = r.json()
    if not body.get("ok"):
        log.warning("telegram %s: %s", method, body.get("description"))
        return None
    return body.get("result")


def send_message(cfg: dict, text: str, chat_id: str | None = None,
                 preview: bool = True) -> bool:
    chat = chat_id or cfg.get("chat_id")
    if not cfg.get("bot_token") or not chat:
        log.warning("telegram: bot_token/chat_id missing")
        return False
    result = call(cfg["bot_token"], "sendMessage", {
        "chat_id": chat,
        "text": text,
        "disable_web_page_preview": not preview,
    })
    return result is not None


def check_credentials(cfg: dict) -> tuple[bool, str]:
    """Used by `rentwatch telegram-test` and the settings page."""
    if not cfg.get("bot_token"):
        return False, "Nessun bot_token impostato."
    me = call(cfg["bot_token"], "getMe", {})
    if not me:
        return False, "Token rifiutato da Telegram."
    if not cfg.get("chat_id"):
        return False, f"Bot @{me.get('username')} ok, ma manca chat_id."
    if send_message(cfg, f"✅ rentwatch è collegato a @{me.get('username')}."):
        return True, f"Messaggio di prova inviato da @{me.get('username')}."
    return False, f"Bot @{me.get('username')} ok, ma l'invio al chat_id è fallito."


# ── what deserves a message ──────────────────────────────────────────────────

def passes_filters(listing: dict, filters: dict) -> bool:
    price = listing.get("price")
    surface = listing.get("surface_m2")

    price_min = filters.get("price_min") or 0
    price_max = filters.get("price_max") or 0
    if price_min and (not price or price < price_min):
        return False
    if price_max and (not price or price > price_max):
        return False
    if (filters.get("surface_min") or 0) and (not surface or surface < filters["surface_min"]):
        return False

    rooms_min = filters.get("rooms_min") or 0
    if rooms_min:
        try:
            if int(str(listing.get("rooms") or 0).rstrip("+")) < rooms_min:
                return False
        except (TypeError, ValueError):
            return False

    zones = [z.strip().lower() for z in (filters.get("zones") or []) if z.strip()]
    if zones and (listing.get("macrozone") or "").strip().lower() not in zones:
        return False

    if filters.get("exclude_agencies") and listing.get("agency"):
        return False

    if filters.get("skip_suspect", True) and db.is_suspect(
            price, surface, listing.get("rooms")):
        return False

    return True


def in_quiet_hours(cfg: dict, now: datetime | None = None) -> bool:
    """Quiet window, wrapping midnight when start > end (e.g. 23 → 8)."""
    start = cfg.get("quiet_hours_start")
    end = cfg.get("quiet_hours_end")
    if start is None or end is None or start == end:
        return False
    hour = (now or datetime.now()).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


# ── message text ─────────────────────────────────────────────────────────────

def format_listing(listing: dict, template: str | None = None,
                   kind: str = "new", old_price: int | None = None) -> str:
    price = listing.get("price")
    surface = listing.get("surface_m2")

    fields = _Blanks(
        title=listing.get("title") or "Nuovo annuncio",
        price=f"€ {price}/mese" if price else "prezzo n.d.",
        surface=f"{surface:.0f} m²" if surface else "m² n.d.",
        ppm2=f" ({price / surface:.1f} €/m²)" if price and surface else "",
        rooms=f"{listing['rooms']} locali" if listing.get("rooms") else "",
        floor=f"piano {listing['floor']}" if listing.get("floor") else "",
        zone=" · ".join(x for x in (listing.get("macrozone"),
                                    listing.get("microzone")) if x),
        address=listing.get("address") or "",
        agency=listing.get("agency") or "privato",
        url=listing.get("url") or "",
        old_price=f"€ {old_price}" if old_price else "",
        delta=f"{price - old_price:+d} €" if price and old_price else "",
    )

    text = (template or DEFAULT_TEMPLATE).format_map(fields)
    if kind == "price_drop" and old_price and price:
        text = f"📉 Ribasso: € {old_price} → € {price}\n\n{text}"
    # A template can leave blank lines where a field was empty.
    return "\n".join(line for line in text.splitlines() if line.strip())


# ── the entry point the scraper calls ────────────────────────────────────────

def notify(conn, cfg: dict, new_listings: list[dict],
           price_drops: list[tuple[dict, int]] | None = None,
           run_summary: str | None = None) -> int:
    """Send (or queue) messages for this run. Returns messages actually sent.

    During quiet hours nothing goes out and everything that passed the filters
    is parked in notify_queue; the next run outside the window drains it first,
    so an overnight find still reaches the phone in the morning.
    """
    if not cfg.get("enabled"):
        return 0
    if not cfg.get("bot_token") or not cfg.get("chat_id"):
        log.warning("telegram enabled but bot_token/chat_id missing")
        return 0

    filters = cfg.get("filters") or {}
    template = cfg.get("template") or DEFAULT_TEMPLATE
    quiet = in_quiet_hours(cfg)

    candidates: list[dict] = [
        {"listing": listing, "kind": "new", "old_price": None}
        for listing in new_listings if passes_filters(listing, filters)
    ]
    if cfg.get("notify_price_drops", True):
        for listing, old_price in (price_drops or []):
            if old_price and listing.get("price") and listing["price"] < old_price \
                    and passes_filters(listing, filters):
                candidates.append({"listing": listing, "kind": "price_drop",
                                   "old_price": old_price})

    if quiet:
        for item in candidates:
            db.queue_notification(conn, item["listing"]["id"], item["kind"],
                                  str(item["old_price"] or ""))
        conn.commit()
        if candidates:
            log.info("quiet hours — queued %d notifications", len(candidates))
        return 0

    # Outside the window: anything parked earlier goes first, oldest first.
    queued = db.drain_notifications(conn)
    pending = [
        {"listing": item["listing"], "kind": item["kind"],
         "old_price": int(item["payload"]) if (item["payload"] or "").isdigit() else None}
        for item in queued
    ] + candidates

    cap = int(cfg.get("max_per_run") or 20)
    sent = 0
    for item in pending[:cap]:
        if send_message(cfg, format_listing(item["listing"], template,
                                            item["kind"], item["old_price"])):
            sent += 1

    if len(pending) > cap:
        send_message(cfg, f"… e altri {len(pending) - cap} annunci. "
                          f"Aprine la lista completa nella dashboard.", preview=False)

    if run_summary and cfg.get("send_run_summary"):
        send_message(cfg, run_summary, preview=False)

    return sent


# Kept so older callers/scripts do not break.
def send_new_listings(cfg: dict, listings: list[dict]) -> int:
    filters = cfg.get("filters") or {}
    template = cfg.get("template") or DEFAULT_TEMPLATE
    sent = 0
    for listing in listings[:int(cfg.get("max_per_run") or 20)]:
        if passes_filters(listing, filters) and \
                send_message(cfg, format_listing(listing, template)):
            sent += 1
    return sent
