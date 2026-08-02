import tomllib
from pathlib import Path

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
        "username": "ion",
        "password_hash": "",      # set with: python -m rentwatch set-password
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


def load_config(path: str | Path | None = None) -> dict:
    """Load config.toml; config.local.toml (gitignored) wins if present."""
    if path is None:
        path = LOCAL_CONFIG if LOCAL_CONFIG.exists() else DEFAULT_CONFIG
    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    _fill(cfg, DEFAULTS)
    if not cfg.get("searches"):
        cfg["searches"] = [dict(s) for s in DEFAULTS["searches"]]
    for search in cfg["searches"]:
        search.setdefault("params", {})

    db = Path(cfg["db_path"])
    if not db.is_absolute():
        cfg["db_path"] = str(PROJECT_ROOT / db)
    return cfg
