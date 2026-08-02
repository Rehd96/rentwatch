"""Dashboard: FastAPI serving a single-page overview of the listings DB.

Everything behind a session cookie. The gate is a middleware rather than a
per-route dependency so that a route added later is protected by default —
forgetting to log in is a nuisance, forgetting to guard an endpoint publishes
where you are about to live.
"""

import logging
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import auth, db, notify
from .config import PROJECT_ROOT
from .settings_store import save_config

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Reachable without a session. Everything else needs the cookie.
PUBLIC_PATHS = {"/login", "/healthz"}


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _days_since(iso: str) -> int:
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def _page(name: str, base: str, user: str = "") -> HTMLResponse:
    """Serve a static page with its base URL baked in, so the same files work
    at the domain root locally and under /case/ on the VPS."""
    html = ((STATIC_DIR / name).read_text(encoding="utf-8")
            .replace("__BASE__", base)
            .replace("__USER__", user))
    return HTMLResponse(html)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


def create_app(cfg: dict) -> FastAPI:
    app = FastAPI(title="rentwatch", docs_url=None, redoc_url=None)
    db_path = cfg["db_path"]
    db.connect(db_path).close()  # ensure schema/migrations before serving

    auth_cfg = cfg.get("auth", {})
    auth_enabled = bool(auth_cfg.get("enabled", True))
    first_user = next((u.get("username") for u in auth_cfg.get("users") or []
                       if u.get("username")), "io")

    # Hearts predating multi-user have no owner; hand them to the account that
    # was the only one at the time. No-op once anyone has hearted anything.
    _conn = db.connect(db_path)
    _adopted = db.adopt_legacy_likes(_conn, first_user)
    _conn.close()
    if _adopted:
        log.info("assigned %d existing favourites to '%s'", _adopted, first_user)
    secret = auth.get_secret_key(cfg)
    throttle = auth.LoginThrottle()

    if auth_enabled and not [u for u in auth_cfg.get("users") or []
                             if u.get("password_hash")]:
        log.warning("auth is on but no account has a password — the dashboard "
                    "will refuse every login. Run: python -m rentwatch set-password")

    def base_of(request: Request) -> str:
        """URL prefix this app is mounted under, always ending in '/'."""
        root = request.scope.get("root_path", "").rstrip("/")
        return f"{root}/"

    def app_path(request: Request) -> str:
        """Path as the routes see it, with any mount prefix removed.

        request.url.path keeps the root_path on it, so comparing it against
        "/login" silently stops matching the moment the app is mounted under
        /case/ — and the gate then redirects the login page to itself.
        """
        root = request.scope.get("root_path", "").rstrip("/")
        path = request.scope.get("path") or request.url.path
        if root and path.startswith(root):
            path = path[len(root):] or "/"
        return path

    # ── the gate ─────────────────────────────────────────────────────────────

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = app_path(request)
        if not auth_enabled or path in PUBLIC_PATHS:
            return await call_next(request)

        session = auth.read_token(secret, request.cookies.get(auth.COOKIE_NAME))
        if session:
            request.state.user = session.get("u")
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"error": "login required"})
        return RedirectResponse(f"{base_of(request)}login", status_code=303)

    # ── login / logout ───────────────────────────────────────────────────────

    def login_html(request: Request, error: str = "", status: int = 200) -> HTMLResponse:
        html = ((STATIC_DIR / "login.html").read_text(encoding="utf-8")
                .replace("__BASE__", base_of(request))
                .replace("__ERROR__", error))
        return HTMLResponse(html, status_code=status)

    @app.get("/login")
    def login_page(request: Request):
        if auth.read_token(secret, request.cookies.get(auth.COOKIE_NAME)):
            return RedirectResponse(base_of(request), status_code=303)
        return login_html(request)

    @app.post("/login")
    async def login(request: Request):
        # Parsed by hand instead of fastapi.Form: that would pull in
        # python-multipart just to read two fields.
        body = (await request.body()).decode("utf-8", "replace")
        form = parse_qs(body)
        username = (form.get("username") or [""])[0].strip()
        password = (form.get("password") or [""])[0]

        ip = _client_ip(request)
        wait = throttle.locked_for(ip)
        if wait:
            return login_html(request,
                              f"Troppi tentativi. Riprova tra {wait} secondi.", 429)

        matched = auth.verify_login(auth_cfg, username, password)
        if not matched:
            throttle.record_failure(ip)
            log.warning("failed login for %r from %s", username, ip)
            return login_html(request, "Credenziali non valide.", 401)

        throttle.reset(ip)
        log.info("login: %s from %s", matched, ip)
        response = RedirectResponse(base_of(request), status_code=303)
        response.set_cookie(
            auth.COOKIE_NAME,
            auth.make_token(secret, matched),
            max_age=auth.SESSION_TTL,
            httponly=True,
            samesite="lax",
            secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
            path=base_of(request),
        )
        return response

    @app.get("/logout")
    @app.post("/logout")
    def logout(request: Request):
        response = RedirectResponse(f"{base_of(request)}login", status_code=303)
        response.delete_cookie(auth.COOKIE_NAME, path=base_of(request))
        return response

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ── pages ────────────────────────────────────────────────────────────────

    def current_user(request: Request) -> str:
        """Who is acting. With the login off there is no session, so hearts are
        attributed to the first configured account rather than to nobody."""
        return getattr(request.state, "user", "") or (
            "" if auth_enabled else first_user)

    @app.get("/")
    def index(request: Request):
        return _page("index.html", base_of(request), current_user(request))

    @app.get("/settings")
    def settings_page(request: Request):
        return _page("settings.html", base_of(request), current_user(request))

    # ── listings API ─────────────────────────────────────────────────────────

    @app.get("/api/listings")
    def listings(request: Request, include_inactive: bool = False,
                 include_hidden: bool = False):
        me = current_user(request)
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
            hearts = db.favourites_by_listing(conn)
            out = []
            for r in rows:
                d = dict(r)
                d["ppm2"] = round(r["price"] / r["surface_m2"], 1) \
                    if r["price"] and r["surface_m2"] else None
                d["days_on_market"] = _days_since(r["first_seen"])
                d["price_delta"] = (r["price"] - r["initial_price"]) \
                    if r["price"] and r["initial_price"] and r["n_prices"] > 1 else 0
                d["suspect"] = db.is_suspect(r["price"], r["surface_m2"], r["rooms"])
                # liked_by is everyone's hearts; liked is only mine, so the
                # button reflects what *I* did while the row shows both.
                d["liked_by"] = hearts.get(r["id"], [])
                d["liked"] = 1 if me in d["liked_by"] else 0
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
                "SELECT started_at, finished_at, listings_seen, new_listings, status "
                "FROM scrape_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return {
                "active_count": len(active),
                "new_7d": new_7d,
                "median_price": round(statistics.median(prices)) if prices else None,
                "median_ppm2": round(statistics.median(ppm2), 1) if ppm2 else None,
                "zones": zone_stats,
                "last_run": dict(last_run) if last_run else None,
                "every_hours": cfg.get("schedule", {}).get("every_hours", 4),
                "queued_notifications": db.queue_size(conn)
                if _has_table(conn, "notify_queue") else 0,
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
    def set_liked(request: Request, listing_id: int, value: bool = True):
        me = current_user(request) or first_user
        conn = _connect(db_path)
        try:
            if not db.set_favourite(conn, listing_id, me, value):
                return JSONResponse(status_code=404, content={"error": "unknown listing"})
            hearts = db.favourites_by_listing(conn).get(listing_id, [])
            return {"id": listing_id, "liked": value, "liked_by": hearts}
        finally:
            conn.close()

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

    # ── settings ─────────────────────────────────────────────────────────────

    @app.get("/api/settings")
    def get_settings():
        """The editable slice of the config. Secrets come back as a boolean
        'is it set', never as text — a bot token in a GET response ends up in
        browser history and proxy logs."""
        tg = cfg.get("telegram", {})
        return {
            "searches": [
                {"name": s.get("name", ""), "path": s.get("path", ""),
                 "params": s.get("params", {})}
                for s in cfg.get("searches", [])
            ],
            "schedule": cfg.get("schedule", {}),
            "telegram": {
                "enabled": tg.get("enabled", False),
                "bot_token_set": bool(tg.get("bot_token")),
                "recipients": [
                    {"chat_id": str(r.get("chat_id", "")), "user": r.get("user", "")}
                    for r in tg.get("recipients") or []
                ],
                "users": [u.get("username", "")
                          for u in cfg.get("auth", {}).get("users") or []],
                "max_per_run": tg.get("max_per_run", 20),
                "quiet_hours_start": tg.get("quiet_hours_start", 23),
                "quiet_hours_end": tg.get("quiet_hours_end", 8),
                "notify_price_drops": tg.get("notify_price_drops", True),
                "send_run_summary": tg.get("send_run_summary", False),
                "template": tg.get("template", notify.DEFAULT_TEMPLATE),
                "filters": tg.get("filters", {}),
            },
            "placeholders": notify.PLACEHOLDERS,
        }

    @app.post("/api/settings")
    async def put_settings(request: Request):
        payload = await request.json()
        tg_in = payload.get("telegram", {})
        tg = cfg.setdefault("telegram", {})

        tg["enabled"] = bool(tg_in.get("enabled"))
        seen, people = set(), []
        for entry in tg_in.get("recipients") or []:
            chat = str(entry.get("chat_id", "")).strip()
            if chat and chat not in seen:      # the same chat twice = two messages
                seen.add(chat)
                people.append({"chat_id": chat,
                               "user": str(entry.get("user", "")).strip()})
        tg["recipients"] = people
        tg["template"] = tg_in.get("template") or notify.DEFAULT_TEMPLATE
        tg["max_per_run"] = max(1, min(100, int(tg_in.get("max_per_run") or 20)))
        tg["quiet_hours_start"] = int(tg_in.get("quiet_hours_start") or 0) % 24
        tg["quiet_hours_end"] = int(tg_in.get("quiet_hours_end") or 0) % 24
        tg["notify_price_drops"] = bool(tg_in.get("notify_price_drops"))
        tg["send_run_summary"] = bool(tg_in.get("send_run_summary"))
        # Blank means "leave the stored token alone" — the form cannot show it,
        # so an empty field must not erase it.
        if tg_in.get("bot_token"):
            tg["bot_token"] = tg_in["bot_token"].strip()

        f_in = tg_in.get("filters", {})
        filters = tg.setdefault("filters", {})
        for key in ("price_min", "price_max", "surface_min", "rooms_min"):
            filters[key] = max(0, int(f_in.get(key) or 0))
        zones = f_in.get("zones") or []
        if isinstance(zones, str):
            zones = [z.strip() for z in zones.split(",")]
        filters["zones"] = [z for z in zones if z]
        filters["exclude_agencies"] = bool(f_in.get("exclude_agencies"))
        filters["skip_suspect"] = bool(f_in.get("skip_suspect"))

        searches = []
        for s in payload.get("searches", []):
            path = (s.get("path") or "").strip()
            if not path:
                continue
            params = {k: v for k, v in (s.get("params") or {}).items()
                      if v not in ("", None)}
            searches.append({"name": (s.get("name") or path).strip(),
                             "path": path, "params": params})
        if searches:
            cfg["searches"] = searches

        try:
            save_config(cfg)
        except Exception as e:
            log.exception("saving settings failed")
            return JSONResponse(status_code=500, content={"error": str(e)})
        return {"ok": True, "searches": len(cfg["searches"])}

    @app.post("/api/telegram-test")
    async def telegram_test(request: Request):
        payload = await request.json() if await request.body() else {}
        probe = dict(cfg.get("telegram", {}))
        if payload.get("bot_token"):
            probe["bot_token"] = payload["bot_token"].strip()
        # Test what is on screen, not what was last saved — otherwise you have
        # to save a chat id before you can find out whether it works.
        if payload.get("recipients"):
            probe["recipients"] = [
                {"chat_id": str(r.get("chat_id", "")).strip(),
                 "user": str(r.get("user", "")).strip()}
                for r in payload["recipients"] if str(r.get("chat_id", "")).strip()
            ]
        ok, message = notify.check_credentials(probe)
        return {"ok": ok, "message": message}

    @app.get("/api/telegram-chats")
    def telegram_chats():
        """Chats that have written to the bot — how you find a chat id without
        typing one from memory."""
        token = cfg.get("telegram", {}).get("bot_token")
        if not token:
            return {"chats": [], "error": "nessun bot_token impostato"}
        return {"chats": [
            {"id": str(c["id"]), "type": c.get("type", ""),
             "name": c.get("username") or c.get("title") or c.get("first_name") or ""}
            for c in notify.known_chats(token)
        ]}

    # ── run a scrape now ─────────────────────────────────────────────────────

    @app.post("/api/scrape-now")
    def scrape_now():
        """Kick off a scrape without waiting for the timer — mostly useful
        right after changing the search conditions."""
        lock = Path(db_path).parent / "scrape.lock"
        if lock.exists() and time.time() - lock.stat().st_mtime < 3600:
            return JSONResponse(status_code=409,
                                content={"error": "uno scrape è già in corso"})
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(int(time.time())))

        # Detached: the request returns immediately, the scrape takes minutes.
        python = sys.executable
        subprocess.Popen(
            [python, "-m", "rentwatch", "scrape"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"ok": True, "message": "scrape avviato — ricarica fra qualche minuto"}

    @app.exception_handler(Exception)
    async def on_error(request, exc):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return app


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None
