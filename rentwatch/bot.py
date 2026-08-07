"""Interactive Telegram bot: ask rentwatch things from the phone.

Long-polls getUpdates in a plain loop — no webhook, so nothing has to be
reachable from the internet and this runs happily beside the dashboard.

Only the chat_id in the config is answered. A bot token is a URL anyone can
guess their way into if it leaks, and this bot reads out addresses, prices and
the flats marked as favourites, so every other chat gets silence.
"""

import logging
import time
from pathlib import Path

from . import db, notify
from .config import load_config
from .settings_store import save_config

log = logging.getLogger(__name__)

POLL_TIMEOUT = 50          # seconds held open by Telegram per getUpdates call
ERROR_BACKOFF = 15

HELP = """Comandi disponibili:

/stato — ultimo scrape, annunci attivi, coda
/ultimi [n] — ultimi annunci trovati (default 5)
/preferiti [nome|miei] — annunci col cuore, di tutti o di una persona
/filtri — filtri di notifica attivi
/prezzo <euro> — cambia il prezzo massimo delle notifiche
/superficie <m2> — cambia la superficie minima
/silenzia — sospendi le notifiche
/riattiva — riprendi le notifiche
/aiuto — questo messaggio"""


def _offset_file(cfg: dict) -> Path:
    return Path(cfg["db_path"]).parent / "telegram_offset"


def _read_offset(cfg: dict) -> int:
    path = _offset_file(cfg)
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def _write_offset(cfg: dict, offset: int) -> None:
    try:
        _offset_file(cfg).write_text(str(offset))
    except OSError as e:
        log.warning("could not persist telegram offset: %s", e)


# ── command handlers ─────────────────────────────────────────────────────────

def _cmd_stato(cfg: dict, conn, args: list[str]) -> str:
    run = conn.execute(
        "SELECT finished_at, listings_seen, new_listings, price_changes, status"
        " FROM scrape_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    active = conn.execute(
        "SELECT COUNT(*) c FROM listings WHERE is_active = 1 AND hidden = 0"
    ).fetchone()["c"]
    per_user = conn.execute(
        "SELECT username, COUNT(*) c FROM favourites GROUP BY username"
        " ORDER BY c DESC").fetchall()
    liked = conn.execute(
        "SELECT COUNT(DISTINCT listing_id) c FROM favourites").fetchone()["c"]

    lines = [f"📊 {active} annunci attivi · {liked} preferiti"]
    if len(per_user) > 1:
        lines.append("   ♥ " + " · ".join(f"{r['username']} {r['c']}" for r in per_user))
    if run:
        lines.append(f"🕒 Ultimo scrape: {run['finished_at'] or 'in corso'}")
        lines.append(f"   {run['listings_seen']} visti · {run['new_listings']} nuovi"
                     f" · {run['price_changes']} variazioni di prezzo")
        if run["status"] != "ok":
            lines.append(f"   ⚠️ stato: {run['status']}")
    else:
        lines.append("🕒 Nessuno scrape ancora eseguito.")

    queued = db.queue_size(conn)
    if queued:
        lines.append(f"🔕 {queued} notifiche in coda (ore di silenzio)")
    tg = cfg.get("telegram", {})
    lines.append(f"🔔 Notifiche: {'attive' if tg.get('enabled') else 'sospese'}"
                 f" · ogni {cfg.get('schedule', {}).get('every_hours', 4)}h")
    return "\n".join(lines)


def _cmd_ultimi(cfg: dict, conn, args: list[str]) -> str:
    try:
        limit = max(1, min(10, int(args[0])))
    except (IndexError, ValueError):
        limit = 5
    rows = conn.execute(
        "SELECT * FROM listings WHERE is_active = 1 AND hidden = 0"
        " ORDER BY first_seen DESC LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        return "Nessun annuncio in archivio."
    template = cfg.get("telegram", {}).get("template")
    return "\n\n".join(notify.format_listing(dict(r), template) for r in rows)


def _cmd_preferiti(cfg: dict, conn, args: list[str]) -> str:
    """/preferiti [nome] — everyone's hearts, or just one person's."""
    who = args[0].strip() if args else None
    listings = db.favourite_listings(conn, username=who, limit=15)
    if not listings:
        if who:
            return f"Nessun preferito di '{who}'. Prova /preferiti senza nome."
        return "Nessun preferito. Metti ♥ agli annunci dalla dashboard."

    template = cfg.get("telegram", {}).get("template")
    out = []
    for listing in listings:
        text = notify.format_listing(listing, template)
        # Whose shortlist this is on is half the point of sharing one.
        hearts = listing.get("liked_by") or []
        if hearts:
            text = f"♥ {' + '.join(hearts)}\n{text}"
        if not listing.get("is_active"):
            text = "⚠️ NON PIÙ ONLINE\n" + text
        out.append(text)

    header = f"❤️ Preferiti di {who}" if who else "❤️ Preferiti"
    return f"{header} ({len(listings)})\n\n" + "\n\n".join(out)


def _cmd_filtri(cfg: dict, conn, args: list[str]) -> str:
    f = cfg.get("telegram", {}).get("filters", {})
    lines = ["🎛 Filtri notifiche:",
             f"  prezzo: {f.get('price_min') or 0} – {f.get('price_max') or '∞'} €",
             f"  superficie minima: {f.get('surface_min') or 0} m²",
             f"  locali minimi: {f.get('rooms_min') or 0}"]
    zones = f.get("zones") or []
    lines.append(f"  zone: {', '.join(zones) if zones else 'tutte'}")
    if f.get("exclude_agencies"):
        lines.append("  solo privati")
    if f.get("skip_suspect", True):
        lines.append("  esclusi i prezzi per stanza")
    lines.append("\nCambia con /prezzo o /superficie, o dalla dashboard.")
    return "\n".join(lines)


def _save_filter(key: str, value) -> None:
    """Re-read from disk before writing so we never clobber an edit made in the
    dashboard between two bot commands."""
    fresh = load_config()
    fresh["telegram"]["filters"][key] = value
    save_config(fresh)


def _cmd_prezzo(cfg: dict, conn, args: list[str]) -> str:
    try:
        value = int(args[0])
    except (IndexError, ValueError):
        return "Uso: /prezzo 900   (0 = nessun limite)"
    _save_filter("price_max", value)
    cfg["telegram"]["filters"]["price_max"] = value
    return f"✅ Prezzo massimo delle notifiche: {value or '∞'} €"


def _cmd_superficie(cfg: dict, conn, args: list[str]) -> str:
    try:
        value = int(args[0])
    except (IndexError, ValueError):
        return "Uso: /superficie 45   (0 = nessun minimo)"
    _save_filter("surface_min", value)
    cfg["telegram"]["filters"]["surface_min"] = value
    return f"✅ Superficie minima delle notifiche: {value or 0} m²"


def _set_enabled(cfg: dict, value: bool) -> str:
    fresh = load_config()
    fresh["telegram"]["enabled"] = value
    save_config(fresh)
    cfg["telegram"]["enabled"] = value
    return "🔕 Notifiche sospese. /riattiva per riprenderle." if not value \
        else "🔔 Notifiche riattivate."


COMMANDS = {
    "stato": _cmd_stato,
    "ultimi": _cmd_ultimi,
    "preferiti": _cmd_preferiti,
    "filtri": _cmd_filtri,
    "prezzo": _cmd_prezzo,
    "superficie": _cmd_superficie,
    "silenzia": lambda cfg, conn, args: _set_enabled(cfg, False),
    "riattiva": lambda cfg, conn, args: _set_enabled(cfg, True),
    "aiuto": lambda cfg, conn, args: HELP,
    "start": lambda cfg, conn, args: "Ciao! " + HELP,
    "help": lambda cfg, conn, args: HELP,
}


def user_for_chat(cfg: dict, chat_id: str | None) -> str:
    """The dashboard account linked to this Telegram chat, if any.

    Lets "/preferiti miei" mean something when two people share the bot.
    """
    for recipient in (cfg.get("telegram", {}).get("recipients") or []):
        if str(recipient.get("chat_id")) == str(chat_id):
            return recipient.get("user") or ""
    return ""


def _handle_callback(cfg: dict, conn, cq: dict, allowed: set[str]) -> None:
    """A tap on the ❤️ button under a notification — same effect as the
    dashboard's heart, so a listing can be saved without leaving Telegram."""
    token = cfg.get("telegram", {}).get("bot_token")
    data = cq.get("data") or ""
    message = cq.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    message_id = message.get("message_id")
    answer = {"callback_query_id": cq["id"]}

    if chat_id not in allowed or not data.startswith("fav:"):
        notify.call(token, "answerCallbackQuery", answer)
        return

    listing_id = int(data.split(":", 1)[1])
    username = user_for_chat(cfg, chat_id)
    if not username:
        answer["text"] = ("Questa chat non è collegata a un account dashboard — "
                          "python -m rentwatch telegram-add-chat --chat-id "
                          f"{chat_id} --user <account>")
        answer["show_alert"] = True
        notify.call(token, "answerCallbackQuery", answer)
        return

    was_liked = conn.execute(
        "SELECT 1 FROM favourites WHERE listing_id = ? AND username = ?",
        (listing_id, username)).fetchone() is not None
    now_liked = not was_liked
    if not db.set_favourite(conn, listing_id, username, now_liked):
        answer["text"] = "Annuncio non più in archivio."
        notify.call(token, "answerCallbackQuery", answer)
        return

    answer["text"] = "❤️ Aggiunto ai preferiti" if now_liked else "🤍 Rimosso dai preferiti"
    notify.call(token, "answerCallbackQuery", answer)
    if message_id:
        notify.call(token, "editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": notify.like_keyboard(listing_id, liked=now_liked),
        })


def handle(cfg: dict, conn, text: str, chat_id: str | None = None) -> str | None:
    if not text.startswith("/"):
        return None
    parts = text[1:].split()
    if not parts:
        return None
    name = parts[0].split("@")[0].lower()   # /stato@MioBot in groups
    handler = COMMANDS.get(name)
    if not handler:
        return f"Comando sconosciuto: /{name}\n\n{HELP}"
    args = parts[1:]
    # "miei" resolves to whoever is asking, so neither person has to remember
    # the exact spelling of their own account name.
    me = user_for_chat(cfg, chat_id)
    if args and args[0].lower() in ("miei", "mie", "mio"):
        args = [me] if me else []
    try:
        return handler(cfg, conn, args)
    except Exception as e:                  # a broken command must not kill the loop
        log.exception("command /%s failed", name)
        return f"⚠️ Errore nell'eseguire /{name}: {e}"


# ── the loop ─────────────────────────────────────────────────────────────────

def run(cfg: dict) -> int:
    tg = cfg.get("telegram", {})
    token = tg.get("bot_token")
    if not token:
        log.error("no bot_token configured — nothing to poll")
        return 1
    allowed = set(notify.chat_ids(tg))
    if not allowed:
        log.error("no recipients configured — refusing to answer every chat")
        return 1

    me = notify.call(token, "getMe", {})
    log.info("bot @%s listening (only chats %s are answered)",
             (me or {}).get("username", "?"), ", ".join(sorted(allowed)))
    notify.call(token, "setMyCommands", {"commands": [
        {"command": name, "description": name}
        for name in ("stato", "ultimi", "preferiti", "filtri", "silenzia",
                     "riattiva", "aiuto")
    ]})

    offset = _read_offset(cfg)
    while True:
        updates = notify.call(token, "getUpdates",
                              {"offset": offset, "timeout": POLL_TIMEOUT},
                              timeout=POLL_TIMEOUT + 10)
        if updates is None:
            time.sleep(ERROR_BACKOFF)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            # Re-read config each time: the dashboard may have changed it, and
            # a stale token here would silently stop answering. It also means a
            # recipient added a minute ago can talk to the bot straight away.
            live = load_config()
            allowed = set(notify.chat_ids(live["telegram"])) or allowed

            callback = update.get("callback_query")
            if callback:
                conn = db.connect(live["db_path"])
                try:
                    _handle_callback(live, conn, callback, allowed)
                finally:
                    conn.close()
                continue

            message = update.get("message") or update.get("edited_message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = (message.get("text") or "").strip()
            if not text:
                continue
            if chat_id not in allowed:
                log.warning("ignoring message from unauthorised chat %s", chat_id)
                continue

            conn = db.connect(live["db_path"])
            try:
                reply = handle(live, conn, text, chat_id=chat_id)
            finally:
                conn.close()
            if reply:
                notify.send_message(live["telegram"], reply, chat_id=chat_id,
                                    preview=False)

        if updates:
            _write_offset(cfg, offset)
