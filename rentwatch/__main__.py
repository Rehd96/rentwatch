import argparse
import getpass
import logging
import sys
from pathlib import Path

from . import db as dbmod
from . import notify
from .config import load_config
from .scraper import ImmobiliareScraper, ScrapeError

log = logging.getLogger("rentwatch")


def cmd_scrape(cfg: dict, args: argparse.Namespace) -> int:
    conn = dbmod.connect(cfg["db_path"])
    first_run = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"] == 0
    scraper = ImmobiliareScraper(delay=tuple(cfg["request_delay"]))
    run_started = dbmod.now_iso()
    run_id = dbmod.start_run(conn)
    conn.commit()

    seen = new = changed = 0
    new_listings: list[dict] = []
    price_drops: list[tuple[dict, int]] = []
    status = "ok"
    try:
        for search in cfg["searches"]:
            log.info("scraping search: %s", search.get("name", "?"))
            for listing in scraper.iter_listings(search, max_pages=args.max_pages):
                outcome, old_price = dbmod.upsert_listing(conn, listing, dbmod.now_iso())
                seen += 1
                if outcome == "new":
                    new += 1
                    new_listings.append(listing)
                elif outcome == "price_changed":
                    changed += 1
                    if old_price and listing.get("price") and listing["price"] < old_price:
                        price_drops.append((listing, old_price))
                if seen % 100 == 0:
                    conn.commit()
        # Only prune when we swept every page, otherwise unseen ≠ gone
        if args.max_pages is None:
            deactivated = dbmod.deactivate_unseen(conn, run_started)
            log.info("deactivated %d listings no longer online", deactivated)
    except ScrapeError as e:
        status = f"error: {e}"
        log.error("scrape aborted: %s", e)
    finally:
        dbmod.finish_run(conn, run_id, seen=seen, new=new, price_changes=changed, status=status)
        conn.commit()
        # Released here rather than in the web app: whoever started the scrape,
        # the process that finishes it is the one that knows it is over.
        lock = Path(cfg["db_path"]).parent / "scrape.lock"
        lock.unlink(missing_ok=True)

    log.info("done: %d seen, %d new, %d price changes", seen, new, changed)

    from .report import generate_report
    log.info("markdown report: %s", generate_report(cfg["db_path"]))

    if first_run:
        log.info("first run — skipping notifications for the initial backlog")
    else:
        summary = (f"🔄 Scrape completato: {seen} annunci visti, {new} nuovi, "
                   f"{changed} variazioni di prezzo.")
        sent = notify.notify(conn, cfg.get("telegram", {}), new_listings,
                             price_drops, run_summary=summary)
        if sent:
            log.info("sent %d telegram notifications", sent)
    conn.close()
    return 0 if status == "ok" else 1


def cmd_serve(cfg: dict, args: argparse.Namespace) -> int:
    import uvicorn

    from .web import create_app

    if cfg.get("auth", {}).get("enabled", True) \
            and not cfg.get("auth", {}).get("password_hash"):
        log.warning("no password set — run `python -m rentwatch set-password` "
                    "or nobody will get in")
    uvicorn.run(create_app(cfg), host=args.host, port=args.port,
                root_path=args.root_path, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")
    return 0


def cmd_set_password(cfg: dict, args: argparse.Namespace) -> int:
    """Hash a password into config.local.toml. The plain text is never stored."""
    from .auth import hash_password
    from .settings_store import save_config

    password = args.password or getpass.getpass("Nuova password dashboard: ")
    if not args.password:
        if password != getpass.getpass("Ripeti: "):
            print("Le password non coincidono.", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("Almeno 8 caratteri, per favore.", file=sys.stderr)
        return 1

    cfg.setdefault("auth", {})
    cfg["auth"]["enabled"] = True
    if args.username:
        cfg["auth"]["username"] = args.username
    cfg["auth"]["password_hash"] = hash_password(password)
    path = save_config(cfg)
    print(f"Password impostata per '{cfg['auth']['username']}' in {path}")
    print("Riavvia il servizio web perché abbia effetto.")
    return 0


def cmd_telegram_test(cfg: dict, args: argparse.Namespace) -> int:
    ok, message = notify.check_credentials(cfg.get("telegram", {}))
    print(message)
    return 0 if ok else 1


def cmd_telegram_chat_id(cfg: dict, args: argparse.Namespace) -> int:
    """Ask Telegram which chats have written to the bot, and what their ids are."""
    tg = cfg.get("telegram", {})
    token = tg.get("bot_token")
    if not token:
        print("Nessun bot_token impostato.")
        return 1

    me = notify.call(token, "getMe", {})
    if not me:
        print("Token rifiutato da Telegram.")
        return 1
    print(f"Bot: @{me.get('username')}")
    print(f"chat_id configurato: {tg.get('chat_id') or '(nessuno)'}\n")

    chats = notify.known_chats(token)
    if not chats:
        print(f"Nessun messaggio ricevuto. Apri @{me.get('username')} su Telegram,")
        print("premi Start, poi rilancia questo comando.")
        print("(Un bot non può scrivere per primo: la chat non esiste finché")
        print(" non gli scrivi tu. Per un canale, aggiungilo come amministratore.)")
        return 1

    for chat in chats:
        who = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
        mark = "  <-- questo, per i messaggi diretti" if chat.get("type") == "private" else ""
        print(f"  {chat['id']:<16} {chat.get('type', '?'):<10} {who}{mark}")
    print("\nIncolla l'id nella pagina impostazioni, oppure in config.local.toml.")
    return 0


def cmd_bot(cfg: dict, args: argparse.Namespace) -> int:
    from .bot import run

    return run(cfg)


def main() -> int:
    parser = argparse.ArgumentParser(prog="rentwatch",
                                     description="Monitor immobiliare.it rentals in Torino")
    parser.add_argument("--config", help="path to config toml", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_scrape = sub.add_parser("scrape", help="fetch listings and update the database")
    p_scrape.add_argument("--max-pages", type=int, default=None,
                          help="limit pages per search (for testing; skips deactivation)")

    p_serve = sub.add_parser("serve", help="run the dashboard web server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8777)
    p_serve.add_argument("--root-path", default="",
                         help="URL prefix when behind a reverse proxy, e.g. /case")

    sub.add_parser("report", help="regenerate reports/overview.md from the database")

    p_pw = sub.add_parser("set-password", help="set the dashboard login password")
    p_pw.add_argument("--username", default=None)
    p_pw.add_argument("--password", default=None,
                      help="non-interactive; note it lands in your shell history")

    sub.add_parser("telegram-test", help="verify the bot token and chat id")
    sub.add_parser("telegram-chat-id",
                   help="list the chats that have written to the bot, with their ids")
    sub.add_parser("bot", help="answer Telegram commands (long-poll loop)")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)

    commands = {
        "scrape": cmd_scrape,
        "serve": cmd_serve,
        "set-password": cmd_set_password,
        "telegram-test": cmd_telegram_test,
        "telegram-chat-id": cmd_telegram_chat_id,
        "bot": cmd_bot,
    }
    if args.command in commands:
        return commands[args.command](cfg, args)
    if args.command == "report":
        from .report import generate_report

        print(generate_report(cfg["db_path"]))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
