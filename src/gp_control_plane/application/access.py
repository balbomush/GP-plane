"""Single-admin authentication and atomic credential changes for Core."""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Any

import jwt

from .. import auth, storage


_TOKEN_TTL = 7 * 24 * 60 * 60


class AccessError(ValueError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _read_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM access_state WHERE id=1").fetchone()
    if row is None:
        raise AccessError(503, "storage_unavailable")
    return dict(row)


def _jwt_key(settings: dict[str, Any]) -> bytes:
    # Old records can have shorter valid signing secrets. Separate this key
    # from the old token format without rewriting existing access state.
    return hashlib.sha256(b"GP Plane API2 JWT\0" + settings["token_secret"].encode("utf-8")).digest()


def _verify(settings: dict[str, Any], authorization: str | None) -> None:
    if not authorization:
        raise AccessError(401, "token_invalid")
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or not token.isascii():
        raise AccessError(401, "token_invalid")
    if token.count(".") == 1:
        try:
            auth._validate_bearer_token(settings, authorization)
        except auth.AuthenticationError as error:
            raise AccessError(401, error.code) from None
        return
    try:
        payload = jwt.decode(
            token, _jwt_key(settings), algorithms=["HS256"], subject=auth.DEFAULT_USERNAME,
            options={"require": ["exp", "sub", "jti", "token_version"], "verify_exp": False},
        )
    except jwt.InvalidTokenError:
        raise AccessError(401, "token_invalid") from None
    expiration, revision, identity = payload["exp"], payload["token_version"], payload["jti"]
    if type(expiration) is not int or type(revision) is not int or not isinstance(identity, str) or not identity:
        raise AccessError(401, "token_invalid")
    # Preserve precedence for a token which is both revoked and expired.
    if revision != settings["token_version"]:
        raise AccessError(401, "token_revoked")
    if expiration <= int(time.time()):
        raise AccessError(401, "token_expired")


def require_access(state_dir: Path, authorization: str | None) -> None:
    if not isinstance(authorization, str) or not authorization.lower().startswith("bearer "):
        raise AccessError(401, "token_invalid")
    with storage.auth_read_snapshot(state_dir) as conn:
        if conn is None:
            raise AccessError(401, "token_invalid")
        _verify(_read_settings(conn), authorization)


def authenticate(state_dir: Path, username: str, password: str) -> dict[str, str]:
    database = state_dir / "strategy-finder" / "state.sqlite3"
    open_storage = storage.open_api2 if database.is_file() else storage.connect_api2
    with open_storage(state_dir) as conn:
        settings = _read_settings(conn)
        if username != auth.DEFAULT_USERNAME or not auth._password_matches(settings, password):
            raise AccessError(401, "invalid_credentials")
        expiration = int(time.time()) + _TOKEN_TTL
        token = jwt.encode({
            "sub": auth.DEFAULT_USERNAME, "exp": expiration, "token_version": settings["token_version"],
            "jti": secrets.token_urlsafe(16),
        }, _jwt_key(settings), algorithm="HS256")
    expires = datetime.fromtimestamp(expiration, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return {"bearer_token": token, "expires_at": expires}


def change_password(
    state_dir: Path, authorization: str | None, username: str, old_password: str, new_password: str,
) -> None:
    with storage.open_api2(state_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = _read_settings(conn)
        _verify(settings, authorization)
        if username != auth.DEFAULT_USERNAME or not auth._password_matches(settings, old_password):
            raise AccessError(403, "invalid_credentials")
        salt = secrets.token_urlsafe(24)
        conn.execute(
            """UPDATE access_state SET password_hash=?, password_salt=?,
               token_version=token_version+1 WHERE id=1""",
            (auth.encoded_password_hash(new_password, salt), salt),
        )
