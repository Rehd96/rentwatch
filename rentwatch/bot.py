"""Interactive Telegram bot: ask rentwatch things from the phone.

Long-polls getUpdates in a plain loop — no webhook, so nothing has to be
reachable from the internet and this runs happily beside the dashboard.

Only the chat_id in the config is answered. A bot token is a URL anyone can
guess their way into if it leaks, and this bot reads out addresses, prices and
the flats marked as favourites, so every other chat gets silence.
"""

import logging
import subprocess
import sys
import time
from pathlib import Path

from . import db, notify
from .config import PROJECT_ROOT, load_config
from .settings_store import save_config

log = logging.getLogger(__name__)

POLL_TIMEOUT = 50          # seconds held open by Telegram per getUpdates call
ERROR_BACKOFF = 15

HELP = """Comandi disponibili:

/stato — riepilogo annunci attivi e ultimo scrape
/ultimi [n] — ultimi annunci (con pulsanti ♥ e ✕)
/cerca <testo|prezzo> — cerca per quartiere, via o prezzo max
/ribassi [n] — annunci che hanno abbassato il canone
/zone — statistiche canoni e €/m² per macrozona
/preferiti [nome|miei] — annunci salvati col cuore
/aggiorna — avvia subito uno scrape degli annunci
/filtri — visualizza i filtri di notifica correnti
/prezzo <euro> — imposta il prezzo massimo delle notifiche
/superficie <m2> — imposta la superficie minima
/silenzia — sospendi temporaneamente le notifiche
/riattiva — riprendi le notifiche
/aiuto — mostra questo messaggio"""


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

def _cmd_stato(cfg: dict, conn, args: list[str], chat_id: str | None = None) -> str:
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


def _cmd_ultimi(cfg: dict, conn, args: list[str], chat_id: str | None = None):
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
    user = user_for_chat(cfg, chat_id)
    out = []
    for r in rows:
        d = dict(r)
        text = notify.format_listing(d, template)
        liked = bool(user and db.is_favourite(conn, d["id"], user))
        kbd = notify.listing_keyboard(d["id"], liked=liked, hidden=bool(d.get("hidden")))
        out.append((text, kbd))
    return out


def _cmd_cerca(cfg: dict, conn, args: list[str], chat_id: str | None = None):
    if not args:
        return "Uso: /cerca <testo o prezzo max>\nEsempi:\n  /cerca vanchiglia\n  /cerca 800"
    query = " ".join(args)
    results = db.search_active_listings(conn, query, limit=5)
    if not results:
        return f"Nessun annuncio trovato per '{query}'."
    template = cfg.get("telegram", {}).get("template")
    user = user_for_chat(cfg, chat_id)
    out = [(f"🔎 Risultati per '{query}' ({len(results)}):", None)]
    for d in results:
        text = notify.format_listing(d, template)
        liked = bool(user and db.is_favourite(conn, d["id"], user))
        kbd = notify.listing_keyboard(d["id"], liked=liked, hidden=bool(d.get("hidden")))
        out.append((text, kbd))
    return out


def _cmd_ribassi(cfg: dict, conn, args: list[str], chat_id: str | None = None):
    try:
        limit = max(1, min(10, int(args[0])))
    except (IndexError, ValueError):
        limit = 5
    drops = db.recent_price_drops(conn, limit=limit)
    if not drops:
        return "Nessun ribasso recente rilevato."
    template = cfg.get("telegram", {}).get("template")
    user = user_for_chat(cfg, chat_id)
    out = [(f"📉 Ultimi ribassi rilevati ({len(drops)}):", None)]
    for d in drops:
        text = notify.format_listing(d, template, kind="price_drop", old_price=d.get("initial_price"))
        liked = bool(user and db.is_favourite(conn, d["id"], user))
        kbd = notify.listing_keyboard(d["id"], liked=liked, hidden=bool(d.get("hidden")))
        out.append((text, kbd))
    return out


def _cmd_zone(cfg: dict, conn, args: list[str], chat_id: str | None = None):
    summary = db.zone_summary(conn, limit=8)
    if not summary:
        return "Nessun dato sulle zone disponibile."
    lines = ["📊 Canoni medi per macrozona:"]
    for z in summary:
        lines.append(f"• {z['macrozone']}: {z['count']} ann. · € {int(z['avg_price'])}/m "
                     f"({z['avg_ppm2']} €/m²)")
    return "\n".join(lines)


def _cmd_scrape(cfg: dict, conn, args: list[str], chat_id: str | None = None):
    db_path = cfg["db_path"]
    lock = Path(db_path).parent / "scrape.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 3600:
        return "⏳ Uno scrape è già in corso. Attendi qualche minuto."
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(int(time.time())))

    python = sys.executable
    subprocess.Popen(
        [python, "-m", "rentwatch", "scrape"],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return "🔄 Scrape avviato in background! Riceverai le notifiche per i nuovi annunci trovati."


def _cmd_preferiti(cfg: dict, conn, args: list[str], chat_id: str | None = None):
    """/preferiti [nome] — everyone's hearts, or just one person's."""
    who = args[0].strip() if args else None
    listings = db.favourite_listings(conn, username=who, limit=15)
    if not listings:
        if who:
            return f"Nessun preferito di '{who}'. Prova /preferiti senza nome."
        return "Nessun preferito. Metti ♥ agli annunci dalla dashboard."

    template = cfg.get("telegram", {}).get("template")
    user = user_for_chat(cfg, chat_id)
    header = f"❤️ Preferiti di {who}" if who else "❤️ Preferiti"
    out = [(f"{header} ({len(listings)}):", None)]
    for listing in listings:
        text = notify.format_listing(listing, template)
        hearts = listing.get("liked_by") or []
        if hearts:
            text = f"♥ {' + '.join(hearts)}\n{text}"
        if not listing.get("is_active"):
            text = "⚠️ NON PIÙ ONLINE\n" + text
        liked = bool(user and user in hearts)
        kbd = notify.listing_keyboard(listing["id"], liked=liked, hidden=bool(listing.get("hidden")))
        out.append((text, kbd))
    return out


def _cmd_filtri(cfg: dict, conn, args: list[str], chat_id: str | None = None) -> str:
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


def _cmd_prezzo(cfg: dict, conn, args: list[str], chat_id: str | None = None) -> str:
    try:
        value = int(args[0])
    except (IndexError, ValueError):
        return "Uso: /prezzo 900   (0 = nessun limite)"
    _save_filter("price_max", value)
    cfg["telegram"]["filters"]["price_max"] = value
    return f"✅ Prezzo massimo delle notifiche: {value or '∞'} €"


def _cmd_superficie(cfg: dict, conn, args: list[str], chat_id: str | None = None) -> str:
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
    "cerca": _cmd_cerca,
    "ribassi": _cmd_ribassi,
    "zone": _cmd_zone,
    "medie": _cmd_zone,
    "preferiti": _cmd_preferiti,
    "aggiorna": _cmd_scrape,
    "scrape": _cmd_scrape,
    "filtri": _cmd_filtri,
    "prezzo": _cmd_prezzo,
    "superficie": _cmd_superficie,
    "silenzia": lambda cfg, conn, args, chat_id=None: _set_enabled(cfg, False),
    "riattiva": lambda cfg, conn, args, chat_id=None: _set_enabled(cfg, True),
    "aiuto": lambda cfg, conn, args, chat_id=None: HELP,
    "start": lambda cfg, conn, args, chat_id=None: "Ciao! " + HELP,
    "help": lambda cfg, conn, args, chat_id=None: HELP,
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
    """A tap on the ♥ or ✕ button under a notification — same effect as the
    dashboard, so a listing can be saved or discarded without leaving Telegram.

    ♥ is per-user (needs the chat linked via --user); ✕ is shared, like the
    dashboard's own discard, so anyone in the recipients list can use it.
    """
    token = cfg.get("telegram", {}).get("bot_token")
    data = cq.get("data") or ""
    message = cq.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    message_id = message.get("message_id")
    answer = {"callback_query_id": cq["id"]}

    action, _, raw_id = data.partition(":")
    if chat_id not in allowed or action not in ("fav", "hide") or not raw_id.isdigit():
        notify.call(token, "answerCallbackQuery", answer)
        return
    listing_id = int(raw_id)

    if action == "fav":
        username = user_for_chat(cfg, chat_id)
        if not username:
            answer["text"] = ("Questa chat non è collegata a un account dashboard — "
                              "python -m rentwatch telegram-add-chat --chat-id "
                              f"{chat_id} --user <account>")
            answer["show_alert"] = True
            notify.call(token, "answerCallbackQuery", answer)
            return
        now_liked = not db.is_favourite(conn, listing_id, username)
        if not db.set_favourite(conn, listing_id, username, now_liked):
            answer["text"] = "Annuncio non più in archivio."
            notify.call(token, "answerCallbackQuery", answer)
            return
        answer["text"] = "❤️ Aggiunto ai preferiti" if now_liked else "🤍 Rimosso dai preferiti"
        notify.call(token, "answerCallbackQuery", answer)
        if not message_id:
            return
        listing = db.get_listing(conn, listing_id)
        liked = bool(username and db.is_favourite(conn, listing_id, username))
        notify.call(token, "editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": notify.listing_keyboard(
                listing_id, liked=liked, hidden=bool(listing and listing["hidden"])),
        })
        return

    listing = db.get_listing(conn, listing_id)
    if not listing:
        answer["text"] = "Annuncio non più in archivio."
        notify.call(token, "answerCallbackQuery", answer)
        return
    db.set_hidden(conn, listing_id, True)
    answer["text"] = "✕ Annuncio scartato"
    notify.call(token, "answerCallbackQuery", answer)
    notify.delete_listing_messages(conn, cfg.get("telegram", {}), listing_id,
                                   current_chat_id=chat_id, current_message_id=message_id)


def handle(cfg: dict, conn, text: str, chat_id: str | None = None):
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
    me = user_for_chat(cfg, chat_id)
    if args and args[0].lower() in ("miei", "mie", "mio"):
        args = [me] if me else []
    try:
        return handler(cfg, conn, args, chat_id=chat_id)
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
        {"command": "stato", "description": "Riepilogo annunci e ultimo scrape"},
        {"command": "ultimi", "description": "Ultimi annunci trovati"},
        {"command": "cerca", "description": "Cerca per via, zona o prezzo"},
        {"command": "ribassi", "description": "Annunci con prezzo ribassato"},
        {"command": "zone", "description": "Canoni medi per macrozona"},
        {"command": "preferiti", "description": "Annunci salvati col cuore"},
        {"command": "aggiorna", "description": "Avvia subito uno scrape"},
        {"command": "filtri", "description": "Filtri di notifica attivi"},
        {"command": "silenzia", "description": "Sospendi le notifiche"},
        {"command": "riattiva", "description": "Riattiva le notifiche"},
        {"command": "aiuto", "description": "Guida ai comandi"},
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
                if isinstance(reply, list):
                    for item in reply:
                        if isinstance(item, tuple):
                            msg_text, kbd = item
                            deliveries = notify.send_message_get_deliveries(
                                live["telegram"], msg_text, chat_id=chat_id,
                                preview=False, reply_markup=kbd)
                            if deliveries and kbd and "inline_keyboard" in kbd:
                                lid = None
                                for row in kbd.get("inline_keyboard", []):
                                    for btn in row:
                                        cb = btn.get("callback_data", "")
                                        if cb.startswith("hide:"):
                                            try:
                                                lid = int(cb.split(":")[1])
                                            except ValueError:
                                                pass
                                if lid:
                                    conn = db.connect(live["db_path"])
                                    try:
                                        for target_chat, msg_id in deliveries:
                                            if msg_id:
                                                db.record_telegram_message(conn, lid, target_chat, msg_id)
                                        conn.commit()
                                    finally:
                                        conn.close()
                        else:
                            notify.send_message(live["telegram"], str(item), chat_id=chat_id,
                                                preview=False)
                else:
                    notify.send_message(live["telegram"], reply, chat_id=chat_id,
                                        preview=False)

        if updates:
            _write_offset(cfg, offset)
