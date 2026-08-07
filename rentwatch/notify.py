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

def request(token: str, method: str, payload: dict,
            timeout: int = 20) -> tuple[bool, object]:
    """One Telegram API call as (ok, result-or-error-description).

    The description matters: "chat not found" and "bot was blocked by the user"
    are different problems with different fixes, and callers that only see None
    can only say "it failed".
    """
    if not token:
        return False, "nessun bot_token"
    try:
        r = requests.post(API.format(token=token, method=method),
                          json=payload, timeout=timeout)
    except Exception as e:                      # network flake, DNS, timeout
        log.warning("telegram %s failed: %s", method, e)
        return False, str(e)
    try:
        body = r.json()
    except Exception:
        log.warning("telegram %s: HTTP %s %s", method, r.status_code, r.text[:200])
        return False, f"HTTP {r.status_code}"
    if not body.get("ok"):
        description = body.get("description") or f"HTTP {r.status_code}"
        log.warning("telegram %s: %s", method, description)
        return False, description
    return True, body.get("result")


def call(token: str, method: str, payload: dict, timeout: int = 20) -> dict | None:
    """One Telegram API call. Returns the 'result' object, or None on failure."""
    ok, result = request(token, method, payload, timeout)
    return result if ok else None


def recipients(cfg: dict) -> list[dict]:
    return [r for r in (cfg.get("recipients") or []) if r.get("chat_id")]


def chat_ids(cfg: dict) -> list[str]:
    return [str(r["chat_id"]) for r in recipients(cfg)]


def send_message(cfg: dict, text: str, chat_id: str | None = None,
                 preview: bool = True, reply_markup: dict | None = None) -> bool:
    """Send to one chat, or to every recipient when chat_id is not given.

    True if it reached at least one person: one blocked or deleted chat should
    not report the whole notification as failed for everybody else.
    """
    if not cfg.get("bot_token"):
        log.warning("telegram: no bot_token")
        return False

    targets = [str(chat_id)] if chat_id else chat_ids(cfg)
    if not targets:
        log.warning("telegram: no recipients configured")
        return False

    delivered = 0
    for target in targets:
        payload = {
            "chat_id": target,
            "text": text,
            "disable_web_page_preview": not preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        ok, error = request(cfg["bot_token"], "sendMessage", payload)
        if ok:
            delivered += 1
        else:
            log.warning("telegram: delivery to %s failed: %s", target, error)
    return delivered > 0


def like_keyboard(listing_id: int, liked: bool = False) -> dict:
    """Inline button so a heart can be tapped from the notification itself —
    no copying the address into the dashboard's search box."""
    label = "💔 Rimuovi dai preferiti" if liked else "🤍 Preferiti"
    return {"inline_keyboard": [[{"text": label, "callback_data": f"fav:{listing_id}"}]]}


def known_chats(token: str) -> list[dict]:
    """Chats that have written to the bot, from getUpdates.

    The reliable way to learn a chat_id: it comes from Telegram itself, so a
    typo or the wrong kind of id cannot survive it. Telegram only keeps recent
    updates, so this is empty until someone messages the bot.
    """
    chats: dict[int, dict] = {}
    for update in call(token, "getUpdates", {"timeout": 0}) or []:
        message = (update.get("message") or update.get("edited_message")
                   or update.get("channel_post") or {})
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            chats[chat["id"]] = chat
    return list(chats.values())


def check_credentials(cfg: dict) -> tuple[bool, str]:
    """Used by `rentwatch telegram-test` and the settings page.

    Tests every recipient separately: with two people configured, "it worked"
    is not useful if only one of them got the message.
    """
    token = cfg.get("bot_token")
    if not token:
        return False, "Nessun bot_token impostato."
    ok, me = request(token, "getMe", {})
    if not ok:
        return False, f"Token rifiutato da Telegram: {me}"
    name = f"@{me.get('username')}"

    targets = recipients(cfg)
    if not targets:
        return False, f"Bot {name} ok, ma non c'è nessun destinatario."

    good, bad = [], []
    for target in targets:
        label = target.get("user") or target["chat_id"]
        ok, error = request(token, "sendMessage", {
            "chat_id": target["chat_id"],
            "text": f"✅ rentwatch è collegato a {name}.",
        })
        (good if ok else bad).append(label if ok else f"{label} ({error})")

    if not bad:
        return True, f"Messaggio di prova inviato da {name} a: {', '.join(good)}."

    # "chat not found" is nearly always the same thing: a bot cannot open a
    # conversation, so the chat does not exist until you write to it first.
    hint = ""
    if any("chat not found" in b.lower() for b in bad):
        candidates = known_chats(token)
        if candidates:
            hint = (" Chat che hanno scritto al bot: "
                    + ", ".join(f"{c['id']} ({c.get('type')})" for c in candidates)
                    + ".")
        else:
            hint = (f" Chi non riceve deve aprire {name} su Telegram e premere "
                    "Start: un bot non può scrivere per primo. Per un gruppo, "
                    "aggiungi il bot come membro.")
    prefix = f"Inviato a {', '.join(good)}. " if good else ""
    return False, f"{prefix}Non consegnato a: {'; '.join(bad)}.{hint}"


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
    if not cfg.get("bot_token") or not recipients(cfg):
        log.warning("telegram enabled but bot_token or recipients missing")
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
        text = format_listing(item["listing"], template, item["kind"], item["old_price"])
        if send_message(cfg, text, reply_markup=like_keyboard(item["listing"]["id"])):
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
