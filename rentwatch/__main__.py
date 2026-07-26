import argparse
import logging
import sys

from . import db as dbmod
from .config import load_config
from .notify import send_new_listings
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
    status = "ok"
    try:
        for search in cfg["searches"]:
            log.info("scraping search: %s", search.get("name", "?"))
            for listing in scraper.iter_listings(search, max_pages=args.max_pages):
                outcome = dbmod.upsert_listing(conn, listing, dbmod.now_iso())
                seen += 1
                if outcome == "new":
                    new += 1
                    new_listings.append(listing)
                elif outcome == "price_changed":
                    changed += 1
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

    log.info("done: %d seen, %d new, %d price changes", seen, new, changed)

    from .report import generate_report
    log.info("markdown report: %s", generate_report(cfg["db_path"]))

    if first_run:
        log.info("first run — skipping notifications for the initial backlog")
    elif new_listings:
        sent = send_new_listings(cfg.get("telegram", {}), new_listings)
        if sent:
            log.info("sent %d telegram notifications", sent)
    return 0 if status == "ok" else 1


def cmd_serve(cfg: dict, args: argparse.Namespace) -> int:
    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(cfg), host=args.host, port=args.port)
    return 0


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

    sub.add_parser("report", help="regenerate reports/overview.md from the database")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)

    if args.command == "scrape":
        return cmd_scrape(cfg, args)
    if args.command == "serve":
        return cmd_serve(cfg, args)
    if args.command == "report":
        from .report import generate_report

        print(generate_report(cfg["db_path"]))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
