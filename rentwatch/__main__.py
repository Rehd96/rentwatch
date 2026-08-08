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
    delisted: list[dict] = []
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
            delisted = dbmod.hearted_expiring(conn, run_started)
            deactivated = dbmod.deactivate_unseen(conn, run_started)
            log.info("deactivated %d listings no longer online (%d hearted)",
                     deactivated, len(delisted))
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
                             price_drops, delisted=delisted, run_summary=summary)
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
    """Add an account, or change the password of one that exists.

    Never overwrites a different account: with two people on the dashboard,
    silently replacing the other one's login would be the worst possible
    behaviour for a typo in --username.
    """
    from .auth import find_user, hash_password
    from .settings_store import save_config

    auth = cfg.setdefault("auth", {})
    users = auth.setdefault("users", [])

    username = (args.username or input("Utente: ")).strip()
    if not username:
        print("Serve un nome utente.", file=sys.stderr)
        return 1

    existing = find_user(auth, username)

    password = args.password or getpass.getpass(f"Password per '{username}': ")
    if not args.password:
        if password != getpass.getpass("Ripeti: "):
            print("Le password non coincidono.", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("Almeno 8 caratteri, per favore.", file=sys.stderr)
        return 1

    auth["enabled"] = True
    if existing:
        existing["password_hash"] = hash_password(password)
        action = "aggiornata la password di"
    else:
        users.append({"username": username, "password_hash": hash_password(password)})
        action = "creato l'utente"

    # Write back to whatever --config was read, not always config.local.toml.
    path = save_config(cfg, args.config)
    print(f"{action.capitalize()} '{username}' in {path}")
    print(f"Utenti ora: {', '.join(u['username'] for u in users)}")
    print("Riavvia il servizio web perché abbia effetto:")
    print("  systemctl restart rentwatch-web")
    return 0


def cmd_list_users(cfg: dict, args: argparse.Namespace) -> int:
    users = cfg.get("auth", {}).get("users") or []
    if not users:
        print("Nessun utente. Creane uno con: python -m rentwatch set-password")
        return 1
    print(f"Login attivo: {'sì' if cfg['auth'].get('enabled', True) else 'NO'}")
    for user in users:
        state = "ok" if user.get("password_hash") else "SENZA PASSWORD — non può entrare"
        print(f"  {user['username']:<20} {state}")
    return 0


def cmd_remove_user(cfg: dict, args: argparse.Namespace) -> int:
    from .settings_store import save_config

    auth = cfg.setdefault("auth", {})
    users = auth.setdefault("users", [])
    username = (args.username or input("Utente da rimuovere: ")).strip()

    remaining = [u for u in users if u.get("username") != username]
    if len(remaining) == len(users):
        print(f"Nessun utente '{username}'.", file=sys.stderr)
        return 1
    # Removing the last account with the login still on locks everybody out,
    # and the only way back in is editing the config over SSH.
    if not remaining and auth.get("enabled", True):
        print("È l'ultimo utente: rimuoverlo chiuderebbe fuori tutti.",
              file=sys.stderr)
        print("Creane un altro prima, oppure disattiva il login con "
              "[auth] enabled = false.", file=sys.stderr)
        return 1

    auth["users"] = remaining
    save_config(cfg, args.config)
    print(f"Rimosso '{username}'. Utenti ora: "
          f"{', '.join(u['username'] for u in remaining) or '(nessuno)'}")
    print("  systemctl restart rentwatch-web")
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
    current = notify.recipients(tg)
    if current:
        print("Destinatari configurati:")
        for r in current:
            print(f"  {r['chat_id']:<16} {r.get('user') or ''}")
    else:
        print("Destinatari configurati: (nessuno)")
    print()

    chats = notify.known_chats(token)
    if not chats:
        print(f"Nessun messaggio ricevuto. Apri @{me.get('username')} su Telegram,")
        print("premi Start, poi rilancia questo comando.")
        print("(Un bot non può scrivere per primo: la chat non esiste finché")
        print(" non gli scrivi tu. Per un canale, aggiungilo come amministratore.)")
        return 1

    known = {r["chat_id"] for r in current}
    print("Chat che hanno scritto al bot:")
    for chat in chats:
        who = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
        mark = " (già destinatario)" if str(chat["id"]) in known else ""
        print(f"  {chat['id']:<16} {chat.get('type', '?'):<10} {who}{mark}")
    print("\nPer aggiungerne uno:")
    print("  python -m rentwatch telegram-add-chat --chat-id <id> [--user <account>]")
    return 0


def cmd_telegram_add_chat(cfg: dict, args: argparse.Namespace) -> int:
    """Add a recipient. Everyone on the list gets every notification."""
    from .settings_store import save_config

    tg = cfg.setdefault("telegram", {})
    people = tg.setdefault("recipients", [])
    chat_id = str(args.chat_id or input("Chat id: ")).strip()
    if not chat_id:
        print("Serve un chat id.", file=sys.stderr)
        return 1

    if args.user:
        known = [u["username"] for u in cfg.get("auth", {}).get("users") or []]
        if known and args.user not in known:
            print(f"Nessun account '{args.user}'. Ci sono: {', '.join(known)}",
                  file=sys.stderr)
            return 1

    existing = next((r for r in people if str(r.get("chat_id")) == chat_id), None)
    if existing:
        existing["user"] = args.user or existing.get("user", "")
        action = "aggiornato"
    else:
        people.append({"chat_id": chat_id, "user": args.user or ""})
        action = "aggiunto"

    save_config(cfg, args.config)
    print(f"Destinatario {action}: {chat_id}"
          + (f" → {args.user}" if args.user else ""))
    print(f"Ora sono {len(people)}: "
          + ", ".join(f"{r['chat_id']}{'/' + r['user'] if r.get('user') else ''}"
                      for r in people))
    print("Verifica con: python -m rentwatch telegram-test")
    return 0


def cmd_telegram_remove_chat(cfg: dict, args: argparse.Namespace) -> int:
    from .settings_store import save_config

    tg = cfg.setdefault("telegram", {})
    people = tg.setdefault("recipients", [])
    chat_id = str(args.chat_id or input("Chat id da rimuovere: ")).strip()
    remaining = [r for r in people if str(r.get("chat_id")) != chat_id]
    if len(remaining) == len(people):
        print(f"Nessun destinatario '{chat_id}'.", file=sys.stderr)
        return 1
    tg["recipients"] = remaining
    save_config(cfg, args.config)
    print(f"Rimosso {chat_id}. Restano {len(remaining)} destinatari.")
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

    p_pw = sub.add_parser("set-password",
                          help="add a dashboard account, or change its password")
    p_pw.add_argument("--username", default=None)
    p_pw.add_argument("--password", default=None,
                      help="non-interactive; note it lands in your shell history")

    sub.add_parser("list-users", help="show the dashboard accounts")

    p_rm = sub.add_parser("remove-user", help="delete a dashboard account")
    p_rm.add_argument("--username", default=None)

    sub.add_parser("telegram-test", help="verify the bot token and chat id")
    sub.add_parser("telegram-chat-id",
                   help="list the chats that have written to the bot, with their ids")

    p_add = sub.add_parser("telegram-add-chat",
                           help="add someone to the notification recipients")
    p_add.add_argument("--chat-id", default=None)
    p_add.add_argument("--user", default=None,
                       help="dashboard account this chat belongs to (optional)")

    p_rmchat = sub.add_parser("telegram-remove-chat",
                              help="stop notifying a chat")
    p_rmchat.add_argument("--chat-id", default=None)
    sub.add_parser("bot", help="answer Telegram commands (long-poll loop)")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)

    commands = {
        "scrape": cmd_scrape,
        "serve": cmd_serve,
        "set-password": cmd_set_password,
        "list-users": cmd_list_users,
        "remove-user": cmd_remove_user,
        "telegram-test": cmd_telegram_test,
        "telegram-chat-id": cmd_telegram_chat_id,
        "telegram-add-chat": cmd_telegram_add_chat,
        "telegram-remove-chat": cmd_telegram_remove_chat,
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
