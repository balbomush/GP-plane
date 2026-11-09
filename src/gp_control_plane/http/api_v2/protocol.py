"""Common HTTP response and validation rules for API 2."""

from collections.abc import Mapping
from functools import wraps
import json
import logging
from traceback import extract_tb
from urllib.parse import parse_qs

from anyio import CancelScope, fail_after, from_thread, to_thread
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Receive, Scope, Send

from ...application.operation_admission import OperationBusy
from ...storage import is_storage_unavailable_error


_LOGGER = logging.getLogger(__name__)


class ApiError(ValueError):
    def __init__(self, status: int, code: str, message: str, details: list[dict] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details

    def response(self) -> JSONResponse:
        error: dict[str, object] = {"code": self.code, "message": str(self)}
        if self.details:
            error["details"] = self.details
        headers = {"WWW-Authenticate": "Bearer"} if self.code.startswith("token_") else {}
        return json_response({"error": error}, self.status, headers)


def json_response(payload: dict, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        payload, status_code=status, headers={
            **(headers or {}), "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
        },
    )


class RejectAmbiguousFraming:
    """Reject conflicting length/transfer coding before any API dispatch."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            names = {name.lower() for name, _ in scope["headers"]}
            if {b"content-length", b"transfer-encoding"} <= names:
                response = ApiError(400, "invalid_request", "Недопустимое обрамление HTTP-запроса").response()
                response.headers["Connection"] = "close"
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def endpoint(action):
    @wraps(action)
    def run(*args, **kwargs):
        try:
            return action(*args, **kwargs)
        except Exception as error:
            return as_api_error(action.__name__, error).response()
    return run


def as_api_error(operation: str, error: Exception) -> ApiError:
    if isinstance(error, ApiError):
        return error
    if isinstance(error, OperationBusy):
        return ApiError(409, "busy", "Core выполняет другую операцию", [{"operation": error.operation}])
    if is_storage_unavailable_error(error):
        return ApiError(503, "storage_unavailable", "Хранилище временно недоступно")
    _LOGGER.error("operation=%s error_code=internal_error type=%s", operation, type(error).__name__)
    if _LOGGER.isEnabledFor(logging.DEBUG):
        # Frame locations provide a traceback without arguments, locals, or
        # exception text (validation exceptions can include user input).
        _LOGGER.debug("operation=%s traceback=%s", operation, [
            (frame.filename, frame.lineno, frame.name) for frame in extract_tb(error.__traceback__)
        ])
    return ApiError(500, "internal_error", "Внутренняя ошибка Core")


def _invalid_json(message: str):
    raise ValueError(message)


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        key.encode('utf-8')  # Invalid Unicode object keys cannot form wire error pointers.
        if key in obj:
            _invalid_json("duplicate key")
        obj[key] = value
    return obj


async def _json_bytes(request: Request, limit: int) -> bytearray:
    length = request.headers.get("Content-Length")
    if request.headers.get("Transfer-Encoding") or (length is not None and not length.isdecimal()):
        raise ValueError("invalid request framing")
    if length is not None and int(length) > limit:
        raise ApiError(413, "payload_too_large", "Превышен допустимый размер запроса")
    body = bytearray()
    with fail_after(30):
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise ApiError(413, "payload_too_large", "Превышен допустимый размер запроса")
            body.extend(chunk)
    if length is not None and len(body) != int(length):
        raise ValueError("incomplete request body")
    return body


def json_body(request: Request, limit: int) -> object:
    media_type = request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise ApiError(415, "unsupported_media_type", "Нужен Content-Type application/json")
    try:
        body = from_thread.run(_json_bytes, request, limit)
        return json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_json)
    except ApiError:
        raise
    except (ClientDisconnect, TimeoutError, ValueError, RecursionError) as error:
        raise ApiError(400, "invalid_request", "Не удалось прочитать JSON запроса") from error


class FileStream(StreamingResponse):
    # Keep the file and permit owned through ASGI response cleanup.

    def __init__(self, chunks, release, *, headers):
        super().__init__(chunks, headers=headers)
        self._release = release

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        except ClientDisconnect:
            pass
        except Exception as error:
            as_api_error("file_stream", error)
        finally:
            with CancelScope(shield=True):
                await to_thread.run_sync(self._release)


def detail(field: str, code: str, message: str) -> dict[str, str]:
    return {"field": field, "code": code, "message": message}


def invalid_parameters(details: list[dict]) -> None:
    if details:
        raise ApiError(400, "invalid_parameters", "Проверьте параметры запроса", details)


def query_fields(request: Request, allowed: set[str]) -> tuple[dict[str, list[str]], list[dict]]:
    query = parse_qs(request.url.query, keep_blank_values=True)
    errors = [
        detail(f"query:{name}", "unknown_parameter", "Неизвестный параметр")
        for name in query if name not in allowed
    ]
    return query, errors


def validation_details(error: ValidationError) -> list[dict[str, str]]:
    details = []
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        location, kind = item["loc"], item["type"]
        field = "body:" + "".join("/" + str(key).replace("~", "~0").replace("/", "~1") for key in location)
        if kind == "missing":
            code, message = "required", "Обязательный параметр"
        elif kind == "extra_forbidden":
            code, message = "unknown_parameter", "Неизвестный параметр"
        elif kind in {"string_too_short", "too_short"}:
            code, message = "empty", "Нужно непустое значение"
        elif kind == "literal_error" and location == ("parameters", "scan_level"):
            code, message = "invalid_value", "Недопустимый режим перебора"
        elif kind == "string_unicode":
            code, message = "invalid_value", "Строка должна быть корректным Unicode"
        elif kind == "string_pattern_mismatch":
            code, message = "invalid_value", "Недопустимое значение"
        elif kind in {"greater_than_equal", "less_than", "less_than_equal"}:
            code, message = "invalid_value", "Число вне допустимого диапазона"
        else:
            code, message = "invalid_type", "Недопустимый тип значения"
        details.append(detail(field, code, message))
    return details


def required_text_fields(request: Request, value: object, model: type[BaseModel]) -> dict[str, str]:
    _, errors = query_fields(request, set())
    parsed = None
    try:
        parsed = model.model_validate(value)
    except ValidationError as error:
        problems = validation_details(error)
        for problem in problems:
            if (isinstance(value, dict) and problem["field"] == "body:/update_id"
                    and problem["code"] == "invalid_value"):
                if value.get("update_id") == "":
                    problem.update(code="empty", message="Нужна непустая строка")
                elif problem["message"] != "Строка должна быть корректным Unicode":
                    problem["message"] = "Нужен UUID обновления"
        errors.extend(problems)
    if isinstance(value, dict):
        for field in model.model_fields:
            raw = value.get(field)
            if isinstance(raw, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in raw):
                pointer = f"body:/{field}"
                if not any(problem["field"] == pointer for problem in errors):
                    errors.append(detail(pointer, "invalid_value", "Строка должна быть корректным Unicode"))
    invalid_parameters(errors)
    assert parsed is not None
    return parsed.model_dump()


def no_body_error(request: Request):
    if request.headers.get("Content-Length") not in (None, "", "0") or request.headers.get("Transfer-Encoding"):
        return detail("body:", "unknown_parameter", "Метод не принимает тело запроса")
    return None


def no_body(request: Request) -> None:
    _, errors = query_fields(request, set())
    if (problem := no_body_error(request)) is not None:
        errors.append(problem)
    invalid_parameters(errors)
