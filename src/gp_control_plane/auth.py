from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from .state import now_iso
from .storage import auth_read_snapshot, auth_transaction


AUTH_SETTINGS_KEY = "auth"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
PASSWORD_MIN_LENGTH = 8
TOKEN_TTL_SECONDS = 24 * 60 * 60
PBKDF2_ITERATIONS = 240_000
_PASSWORD_PREFIX = f"pbkdf2-sha256${PBKDF2_ITERATIONS}$"


class AuthenticationError(ValueError):
    """Authentication failure that API adapters must expose as HTTP 401."""

    status_code = 401

    def __init__(self, message: str, *, code: str = "token_invalid"):
        super().__init__(message)
        self.code = code


class PasswordValidationError(ValueError):
    """Invalid password-change input that API adapters must expose as HTTP 400."""

    status_code = 400


def login(state_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    username = str(payload.get("username") or "").strip()
    password = _password_value(payload.get("password"))
    settings = _existing_settings_snapshot(state_dir)
    if settings is None:
        # First use creates credentials under the dedicated writer lock.  This
        # preserves one-time bootstrap semantics across Core/Web processes.
        with auth_transaction(state_dir) as conn:
            settings = _settings(conn)
    if username != DEFAULT_USERNAME or not _password_matches(settings, password):
        raise AuthenticationError("invalid credentials")
    return _token_payload(settings)


def change_password(
    state_dir: Path, payload: dict[str, Any], authorization: str | None = None
) -> dict[str, Any]:
    current_password = _password_value(payload.get("current_password"))
    new_password = _password_value(payload.get("new_password"))
    if new_password != DEFAULT_PASSWORD and len(new_password) < PASSWORD_MIN_LENGTH:
        raise PasswordValidationError(f"new password must be at least {PASSWORD_MIN_LENGTH} characters")
    with auth_transaction(state_dir) as conn:
        settings = _settings(conn)
        if authorization is not None:
            _validate_bearer_token(settings, authorization)
        if not _password_matches(settings, current_password):
            raise PasswordValidationError("invalid current password")
        salt = secrets.token_urlsafe(24)
        updated = {
            **settings,
            "password_salt": salt,
            "password_hash": _password_hash(new_password, salt),
            "token_secret": secrets.token_urlsafe(32),
            "token_version": int(settings["token_version"]) + 1,
        }
        _save_settings(conn, updated)
        return _token_payload(updated)


def require_bearer_token(state_dir: Path, authorization: str | None) -> None:
    settings = _existing_settings_snapshot(state_dir)
    if settings is None:
        # Preserve fail-closed bootstrap for an uninitialized state directory.
        with auth_transaction(state_dir) as conn:
            settings = _settings(conn)
    _validate_bearer_token(settings, authorization)


def _validate_bearer_token(settings: dict[str, Any], authorization: str | None) -> None:
    if not isinstance(authorization, str):
        raise AuthenticationError("missing bearer token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("missing bearer token")
    signed_payload, separator, signature = token.strip().partition(".")
    if not separator or not signature or not token.isascii():
        raise AuthenticationError("invalid bearer token")
    expected = _sign(settings, signed_payload)
    if not hmac.compare_digest(signature, expected):
        raise AuthenticationError("invalid bearer token")
    try:
        payload = json.loads(_decode(signed_payload).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationError("invalid bearer token") from exc
    if not isinstance(payload, dict):
        raise AuthenticationError("invalid bearer token")
    if payload.get("sub") != DEFAULT_USERNAME:
        raise AuthenticationError("invalid bearer token")
    try:
        token_version = int(payload["token_version"])
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError("invalid bearer token") from exc
    if token_version != int(settings["token_version"]):
        raise AuthenticationError("expired bearer token", code="token_revoked")
    if expires_at <= int(time.time()):
        raise AuthenticationError("expired bearer token", code="token_expired")


def health_payload() -> dict[str, str]:
    return {"status": "ok"}


def _settings(conn: Any) -> dict[str, Any]:
    row = conn.execute("SELECT value_json FROM app_settings WHERE key = ?", (AUTH_SETTINGS_KEY,)).fetchone()
    if row is None:
        raw = _initial_settings()
        conn.execute(
            "INSERT INTO app_settings(key, value_json, updated_at) VALUES(?, ?, ?)",
            (AUTH_SETTINGS_KEY, json.dumps(raw, ensure_ascii=False, separators=(",", ":")), now_iso()),
        )
    else:
        raw = _settings_value(row)
    return _validated_settings(raw)


def _existing_settings_snapshot(state_dir: Path) -> dict[str, Any] | None:
    """Return an initialized auth record from one SQLite reader snapshot."""
    with auth_read_snapshot(state_dir) as conn:
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT value_json FROM app_settings WHERE key = ?", (AUTH_SETTINGS_KEY,)
            ).fetchone()
        except sqlite3.OperationalError as error:
            # A pre-auth database may need schema migration, which is a writer
            # operation.  Do not treat any other storage failure as bootstrap.
            if "no such table: app_settings" in str(error).lower():
                return None
            raise
    if row is None:
        return None
    return _validated_settings(_settings_value(row))


def _settings_value(row: Any) -> Any:
    try:
        return json.loads(str(row["value_json"] or "null"))
    except json.JSONDecodeError:
        return None


def _validated_settings(raw: Any) -> dict[str, Any]:
    if not _valid_settings(raw):
        raise AuthenticationError("auth settings are invalid")
    return dict(raw)


def _valid_settings(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    required_text = ("password_salt", "password_hash", "token_secret")
    if not all(isinstance(raw.get(key), str) and raw[key] for key in required_text):
        return False
    try:
        return int(raw["token_version"]) > 0
    except (KeyError, TypeError, ValueError):
        return False


def _save_settings(conn: Any, settings: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO app_settings(key, value_json, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value_json = excluded.value_json,
            updated_at = excluded.updated_at
        """,
        (AUTH_SETTINGS_KEY, json.dumps(settings, ensure_ascii=False, separators=(",", ":")), now_iso()),
    )


def _initial_settings() -> dict[str, Any]:
    salt = secrets.token_urlsafe(24)
    return {
        "username": DEFAULT_USERNAME,
        "password_salt": salt,
        "password_hash": _password_hash(DEFAULT_PASSWORD, salt),
        "token_secret": secrets.token_urlsafe(32),
        "token_version": 1,
    }


def _password_value(value: Any) -> str:
    return str(value) if isinstance(value, str) else ""


def _password_hash(password: str, salt: str) -> str:
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    )
    return derived.hex()


def _password_matches(settings: dict[str, Any], password: str) -> bool:
    expected = str(settings.get("password_hash") or "")
    if expected.startswith(_PASSWORD_PREFIX):
        expected = expected.removeprefix(_PASSWORD_PREFIX)
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return False
    actual = _password_hash(password, str(settings.get("password_salt") or ""))
    return hmac.compare_digest(actual, expected)


def encoded_password_hash(password: str, salt: str) -> str:
    return _PASSWORD_PREFIX + _password_hash(password, salt)


def initial_core_settings() -> dict[str, Any]:
    settings = _initial_settings()
    settings["password_hash"] = _PASSWORD_PREFIX + settings["password_hash"]
    return settings


def _issue_token(settings: dict[str, Any], ttl_seconds: int) -> tuple[str, int]:
    expires_at = int(time.time()) + ttl_seconds
    payload = {
        "sub": DEFAULT_USERNAME,
        "exp": expires_at,
        "token_version": int(settings["token_version"]),
        "nonce": secrets.token_urlsafe(12),
    }
    encoded = _encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{encoded}.{_sign(settings, encoded)}", expires_at


def _token_payload(settings: dict[str, Any]) -> dict[str, Any]:
    token, _ = _issue_token(settings, TOKEN_TTL_SECONDS)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_SECONDS,
    }


def _sign(settings: dict[str, Any], payload: str) -> str:
    secret = str(settings.get("token_secret") or "").encode("utf-8")
    return _encode(hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest())


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))
