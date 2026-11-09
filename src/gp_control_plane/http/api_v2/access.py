"""API 2 transport for the single Core administrator."""

from functools import wraps
from pathlib import Path

from fastapi import FastAPI, Request, Response

from ...application import access
from ...resource_budget import JSON_REQUEST_MAX_BYTES
from .models import LoginRequest, PasswordRequest
from .openapi import describe
from .protocol import ApiError, endpoint, json_body, required_text_fields


def _as_api_error(error: access.AccessError) -> ApiError:
    message = {
        "invalid_credentials": "Неверный логин или пароль",
        "storage_unavailable": "Доступ Core не инициализирован",
    }.get(error.code, "Требуется действующий Bearer-токен")
    return ApiError(error.status, error.code, message)


def require_access(state_dir: Path, authorization: str | None) -> None:
    try:
        access.require_access(state_dir, authorization)
    except access.AccessError as error:
        raise _as_api_error(error) from None


def authorized(state_dir: Path):
    def decorate(action):
        @wraps(action)
        @endpoint
        def handle(request: Request, *args, **kwargs):
            require_access(state_dir, request.headers.get("Authorization"))
            return action(request, *args, **kwargs)
        return handle
    return decorate


def register_access(app: FastAPI, state_dir: Path, *, json_limit: int = JSON_REQUEST_MAX_BYTES) -> None:
    @app.post("/api/v2/access/authentication", **describe("authentication"))
    @endpoint
    def authentication(request: Request):
        data = required_text_fields(request, json_body(request, json_limit), LoginRequest)
        try:
            return access.authenticate(state_dir, data["username"], data["password"])
        except access.AccessError as error:
            raise _as_api_error(error) from None

    @app.put("/api/v2/access/change_password", **describe("change_password"))
    @authorized(state_dir)
    def change_password(request: Request):
        authorization = request.headers.get("Authorization")
        data = required_text_fields(request, json_body(request, json_limit), PasswordRequest)
        try:
            access.change_password(state_dir, authorization, data["username"], data["old_password"], data["new_password"])
        except access.AccessError as error:
            raise _as_api_error(error) from None
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
