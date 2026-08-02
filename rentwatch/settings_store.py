"""Read and write config.local.toml from the dashboard.

tomllib reads TOML but cannot write it, and pulling in tomli-w for the handful
of scalar types this config uses would break the "no dependency without a
strong reason" rule. So: a small emitter that covers exactly our shapes
(scalars, flat lists, one nested table level, and the [[searches]] array).

Only config.local.toml is ever written. config.toml stays as the committed
example — editing settings in the browser never touches a tracked file.
"""

import logging
import shutil
from pathlib import Path

from .config import LOCAL_CONFIG, PROJECT_ROOT, load_config, tomllib

log = logging.getLogger(__name__)

# Keys that carry secrets. Never sent to the browser as plain text; the UI gets
# a "set" / "not set" marker and only submits a value when you actually retype
# one, so saving the form does not wipe a token you cannot see.
SECRET_KEYS = {"bot_token", "password_hash", "secret_key"}


def _fmt(value) -> str:
    if isinstance(value, bool):          # before int — bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "")
    return f'"{text}"'


def _table_body(table: dict) -> list[str]:
    """Scalar keys of a table, in a stable order."""
    return [f"{k} = {_fmt(v)}" for k, v in table.items()
            if not isinstance(v, dict) and not _is_table_array(v)]


def _is_table_array(value) -> bool:
    return isinstance(value, list) and value and all(isinstance(v, dict) for v in value)


def dumps(cfg: dict) -> str:
    lines = ["# rentwatch — written by the dashboard settings page.",
             "# Hand edits are fine; the next save from the browser overwrites them.",
             ""]
    lines += _table_body(cfg)

    for key, value in cfg.items():
        if isinstance(value, dict):
            lines += ["", f"[{key}]", *_table_body(value)]
            # Arrays of tables before sub-tables: once [a.b] is open, a later
            # [[a.users]] would still parse, but the file reads as if users
            # belonged to b. Emitting them first keeps it honest.
            for sub_key, sub_value in value.items():
                if _is_table_array(sub_value):
                    for entry in sub_value:
                        lines += ["", f"[[{key}.{sub_key}]]", *_table_body(entry)]
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, dict):
                    lines += ["", f"[{key}.{sub_key}]", *_table_body(sub_value)]

    for key, value in cfg.items():
        if _is_table_array(value):
            for entry in value:
                lines += ["", f"[[{key}]]", *_table_body(entry)]
                for sub_key, sub_value in entry.items():
                    if isinstance(sub_value, dict):
                        lines += ["", f"[{key}.{sub_key}]", *_table_body(sub_value)]

    return "\n".join(lines) + "\n"


def save_config(cfg: dict, path: Path | None = None) -> Path:
    """Write the config out, keeping one backup of what was there before.

    The file is written beside the target and renamed, so a crash mid-write
    cannot leave a half-parsed config that stops the scraper from starting.
    """
    path = Path(path or LOCAL_CONFIG)

    # load_config() absolutises db_path; writing that back would pin the config
    # to today's checkout directory. Store it relative again where we can.
    cfg = dict(cfg)
    try:
        cfg["db_path"] = str(Path(cfg["db_path"]).relative_to(PROJECT_ROOT))
    except (ValueError, KeyError, TypeError):
        pass

    text = dumps(cfg)

    # Refuse to write something we cannot read back.
    tomllib.loads(text)

    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    log.info("settings written to %s", path)
    return path


def redact(cfg: dict) -> dict:
    """Deep copy with secrets replaced by a boolean 'is it set?' marker."""
    out = {}
    for key, value in cfg.items():
        if isinstance(value, dict):
            out[key] = redact(value)
        elif _is_table_array(value):
            out[key] = [redact(v) for v in value]
        elif key in SECRET_KEYS:
            out[f"{key}_set"] = bool(value)
        else:
            out[key] = value
    return out


def reload() -> dict:
    return load_config()
