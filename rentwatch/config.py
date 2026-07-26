import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"
LOCAL_CONFIG = PROJECT_ROOT / "config.local.toml"


def load_config(path: str | Path | None = None) -> dict:
    """Load config.toml; config.local.toml (gitignored) wins if present."""
    if path is None:
        path = LOCAL_CONFIG if LOCAL_CONFIG.exists() else DEFAULT_CONFIG
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("db_path", "data/rentwatch.db")
    cfg.setdefault("request_delay", [1.5, 3.0])
    cfg.setdefault("telegram", {"enabled": False})
    cfg.setdefault("searches", [{"name": "Torino", "path": "/affitto-case/torino/", "params": {}}])
    db = Path(cfg["db_path"])
    if not db.is_absolute():
        cfg["db_path"] = str(PROJECT_ROOT / db)
    return cfg
