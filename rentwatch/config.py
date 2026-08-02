from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:          # Python 3.10 and older — the VPS is on 3.10
    # tomli is what tomllib was adopted from: same API, pure Python, no build.
    # Imported under the stdlib name so the rest of the code cannot tell.
    import tomli as tomllib

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"
LOCAL_CONFIG = PROJECT_ROOT / "config.local.toml"

# Every key the app reads, with the value it falls back to. Keeping them in one
# place means an old config.local.toml written before a feature existed still
# loads — the missing keys just take their defaults.
DEFAULTS = {
    "db_path": "data/rentwatch.db",
    "request_delay": [1.5, 3.0],
    "auth": {
        # The dashboard exposes addresses, prices and the flats you liked.
        # Off is available for laptop use, but the VPS must keep it on.
        "enabled": True,
        # One [[auth.users]] block per person, each with username and
        # password_hash. Managed with: python -m rentwatch set-password
        "users": [],
        "secret_key": "",         # blank -> generated once into data/session_secret
    },
    "schedule": {
        # Documentation for the systemd timer, and what the dashboard shows as
        # "next run". Changing it here does not move the timer; see deploy/.
        "every_hours": 4,
    },
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "max_per_run": 20,
        # Nothing is sent between these hours (local time); the listings are
        # held back and go out with the first run after the window.
        "quiet_hours_start": 23,
        "quiet_hours_end": 8,
        "notify_price_drops": True,
        "send_run_summary": False,
        # Placeholders: {title} {price} {surface} {ppm2} {rooms} {floor}
        # {zone} {address} {agency} {url}
        "template": "🏠 {title}\n💶 {price} · {surface}{ppm2}\n📍 {zone}\n{url}",
        "filters": {
            "price_min": 0,
            "price_max": 0,          # 0 = no limit
            "surface_min": 0,
            "rooms_min": 0,
            "zones": [],             # macrozone names; empty = all
            "exclude_agencies": False,
            "skip_suspect": True,    # drop the per-room "student price" listings
        },
    },
    "searches": [
        {"name": "Torino", "path": "/affitto-case/torino/", "params": {}},
    ],
}


def _fill(target: dict, defaults: dict) -> dict:
    for key, value in defaults.items():
        if isinstance(value, dict):
            child = target.get(key)
            target[key] = _fill(child if isinstance(child, dict) else {}, value)
        else:
            target.setdefault(key, value.copy() if isinstance(value, list) else value)
    return target


def _normalise_auth(auth: dict) -> dict:
    """Turn any accepted spelling of the accounts into one users list.

    Before multi-user there was a single `username`/`password_hash` pair
    directly under [auth]. Configs written then must keep working, so they are
    folded into users[0] here and the legacy keys dropped — one source of
    truth, and the next save writes the new shape.
    """
    users = [u for u in (auth.get("users") or [])
             if isinstance(u, dict) and u.get("username")]

    legacy_name = auth.pop("username", None)
    legacy_hash = auth.pop("password_hash", None)
    if legacy_name and legacy_hash and \
            not any(u["username"] == legacy_name for u in users):
        users.insert(0, {"username": legacy_name, "password_hash": legacy_hash})

    for user in users:
        user.setdefault("password_hash", "")
    auth["users"] = users
    return auth


def load_config(path: str | Path | None = None) -> dict:
    """Load config.toml; config.local.toml (gitignored) wins if present."""
    if path is None:
        path = LOCAL_CONFIG if LOCAL_CONFIG.exists() else DEFAULT_CONFIG
    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    _fill(cfg, DEFAULTS)
    _normalise_auth(cfg["auth"])
    if not cfg.get("searches"):
        cfg["searches"] = [dict(s) for s in DEFAULTS["searches"]]
    for search in cfg["searches"]:
        search.setdefault("params", {})

    db = Path(cfg["db_path"])
    if not db.is_absolute():
        cfg["db_path"] = str(PROJECT_ROOT / db)
    return cfg
