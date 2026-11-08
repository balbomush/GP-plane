"""The single API 2 admin account and atomic password revision."""

from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
import secrets
import sqlite3

from bottle import Bottle, HTTPResponse, request

from ... import auth, storage
from ...resource_budget import JSON_REQUEST_MAX_BYTES
from .protocol import ApiError, endpoint, json_body, json_response, required_text_fields


_TOKEN_TTL = 7 * 24 * 60 * 60


def _access(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM access_state WHERE id=1").fetchone()
    if row is None:
        raise ApiError(503, "storage_unavailable", "Доступ Core не инициализирован")
    return dict(row)


def _verify(settings: dict, authorization: str | None) -> None:
    try:
        auth._validate_bearer_token(settings, authorization)
    except auth.AuthenticationError as error:
        raise ApiError(401, error.code, "Требуется действующий Bearer-токен") from error


def require_access(state_dir: Path, authorization: str | None) -> None:
    if not isinstance(authorization, str) or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "token_invalid", "Требуется действующий Bearer-токен")
    with storage.auth_read_snapshot(state_dir) as conn:
        if conn is None:
            raise ApiError(401, "token_invalid", "Требуется действующий Bearer-токен")
        _verify(_access(conn), authorization)


def authorized(state_dir: Path):
    def decorate(action):
        @wraps(action)
        @endpoint
        def handle(*args, **kwargs):
            require_access(state_dir, request.headers.get("Authorization"))
            return action(*args, **kwargs)
        return handle
    return decorate


def register_access(app: Bottle, state_dir: Path, *, json_limit: int = JSON_REQUEST_MAX_BYTES) -> None:
    @app.post("/api/v2/access/authentication")
    @endpoint
    def authentication():
        data = required_text_fields(json_body(json_limit), ("username", "password"))
        with storage.connect_api2(state_dir) as conn:
            settings = _access(conn)
            if data["username"] != auth.DEFAULT_USERNAME or not auth._password_matches(settings, data["password"]):
                raise ApiError(401, "invalid_credentials", "Неверный логин или пароль")
            token, expiration = auth._issue_token(settings, _TOKEN_TTL)
        expires = datetime.fromtimestamp(expiration, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        return json_response({"bearer_token": token, "expires_at": expires})

    @app.put("/api/v2/access/change_password")
    @endpoint
    def change_password():
        authorization = request.headers.get("Authorization")
        require_access(state_dir, authorization)
        data = required_text_fields(json_body(json_limit), ("username", "old_password", "new_password"))
        with storage.connect_api2(state_dir) as conn:
            conn.execute("BEGIN IMMEDIATE")
            settings = _access(conn)
            _verify(settings, authorization)
            if data["username"] != auth.DEFAULT_USERNAME or not auth._password_matches(settings, data["old_password"]):
                raise ApiError(403, "invalid_credentials", "Неверный логин или пароль")
            salt = secrets.token_urlsafe(24)
            conn.execute(
                """UPDATE access_state SET password_hash=?, password_salt=?,
                   token_version=token_version+1 WHERE id=1""",
                (auth._password_hash(data["new_password"], salt), salt),
            )
        return HTTPResponse(status=204, headers={"Cache-Control": "no-store"})
