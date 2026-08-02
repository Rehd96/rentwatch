"""Password + signed-session-cookie auth for the dashboard.

Stdlib only, on purpose: the project rule is no new dependency without a strong
reason, and PBKDF2 + HMAC out of hashlib/hmac is exactly what a session cookie
needs. The gate lives in the app rather than in nginx so that reaching
127.0.0.1:8777 directly — an SSH tunnel, a misplaced proxy_pass, a future
container port — still asks for a password.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

log = logging.getLogger(__name__)

ITERATIONS = 260_000
SESSION_TTL = 30 * 24 * 3600  # a month; this is a personal flat hunt, not a bank
COOKIE_NAME = "rentwatch_session"

# Login throttle: after this many failures from one IP, refuse for a while.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


# ── passwords ────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.b64decode(salt_b64), int(iterations)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


# ── secret key ───────────────────────────────────────────────────────────────

def get_secret_key(cfg: dict) -> bytes:
    """Key that signs session cookies.

    Config wins; otherwise a key is generated once and kept in data/ beside the
    database, so a restart does not log the phone out. Never in config.toml —
    that file is committed.
    """
    configured = (cfg.get("auth") or {}).get("secret_key")
    if configured:
        return configured.encode()

    key_file = Path(cfg["db_path"]).parent / "session_secret"
    if key_file.exists():
        return key_file.read_bytes().strip()

    key = secrets.token_urlsafe(48).encode()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(key)
    os.chmod(key_file, 0o600)
    log.info("generated a new session secret at %s", key_file)
    return key


# ── session cookies ──────────────────────────────────────────────────────────

def _sign(secret: bytes, payload: bytes) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(secret, payload, hashlib.sha256).digest()
    ).decode().rstrip("=")


def make_token(secret: bytes, user: str = "ion", ttl: int = SESSION_TTL) -> str:
    payload = json.dumps({"u": user, "exp": int(time.time()) + ttl}).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_sign(secret, payload)}"


def read_token(secret: bytes, token: str | None) -> dict | None:
    """Return the session payload, or None if absent/forged/expired."""
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(_sign(secret, payload), signature):
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


# ── login throttle ───────────────────────────────────────────────────────────

class LoginThrottle:
    """In-memory failure counter per IP. Resets on restart, which is fine —
    it exists to make online guessing slow, not to be an audit trail."""

    def __init__(self, max_failures: int = MAX_FAILURES,
                 lockout: int = LOCKOUT_SECONDS):
        self.max_failures = max_failures
        self.lockout = lockout
        self._failures: dict[str, list[float]] = {}

    def locked_for(self, ip: str) -> int:
        """Seconds remaining before this IP may try again (0 = go ahead)."""
        recent = [t for t in self._failures.get(ip, []) if t > time.time() - self.lockout]
        self._failures[ip] = recent
        if len(recent) < self.max_failures:
            return 0
        return int(recent[0] + self.lockout - time.time()) + 1

    def record_failure(self, ip: str) -> None:
        self._failures.setdefault(ip, []).append(time.time())

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)
