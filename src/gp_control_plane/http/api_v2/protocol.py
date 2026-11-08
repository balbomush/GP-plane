"""Common HTTP response and validation rules for API 2."""

from collections.abc import Mapping
from functools import wraps
import json
import logging
from urllib.parse import parse_qs

from bottle import HTTPResponse, request

from ...application.operation_admission import OperationBusy
from ...storage import is_storage_unavailable_error
from ..http_body import ProxyRequestBodyMalformed, ProxyRequestBodyTooLarge, read_request_body


_LOGGER = logging.getLogger(__name__)


class ApiError(ValueError):
    def __init__(self, status: int, code: str, message: str, details: list[dict] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details

    def response(self) -> HTTPResponse:
        error = {"code": self.code, "message": str(self)}
        if self.details:
            error["details"] = self.details
        headers = {"WWW-Authenticate": "Bearer"} if self.code.startswith("token_") else {}
        return json_response({"error": error}, self.status, headers)


def json_response(payload: dict, status: int = 200, headers: dict | None = None) -> HTTPResponse:
    return HTTPResponse(
        json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8"),
        status=status, headers={
            **(headers or {}), "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
        },
    )


def endpoint(action):
    @wraps(action)
    def run(*args, **kwargs):
        try:
            return action(*args, **kwargs)
        except OperationBusy as error:
            return ApiError(409, "busy", "Core выполняет другую операцию", [
                {"operation": error.operation},
            ]).response()
        except ApiError as error:
            return error.response()
        except Exception as error:
            if is_storage_unavailable_error(error):
                return ApiError(503, "storage_unavailable", "Хранилище временно недоступно").response()
            _LOGGER.error("API 2 request failed (%s)", type(error).__name__)
            return ApiError(500, "internal_error", "Внутренняя ошибка Core").response()
    return run


def _invalid_json(message: str):
    raise ValueError(message)


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            _invalid_json("duplicate key")
        obj[key] = value
    return obj


def json_body(limit: int) -> object:
    media_type = request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise ApiError(415, "unsupported_media_type", "Нужен Content-Type application/json")
    try:
        body = read_request_body(request.environ, request.path, json_max_bytes=limit)
        return json.loads(
            (body or b"").decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_invalid_json,
        )
    except ProxyRequestBodyTooLarge as error:
        raise ApiError(413, "payload_too_large", "Превышен допустимый размер запроса") from error
    except (ProxyRequestBodyMalformed, ValueError, RecursionError) as error:
        raise ApiError(400, "invalid_request", "Не удалось прочитать JSON запроса") from error


def detail(field: str, code: str, message: str) -> dict[str, str]:
    return {"field": field, "code": code, "message": message}


def invalid_parameters(details: list[dict]) -> None:
    if details:
        raise ApiError(400, "invalid_parameters", "Проверьте параметры запроса", details)


def query_fields(allowed: set[str]) -> tuple[dict[str, list[str]], list[dict]]:
    query = parse_qs(request.query_string, keep_blank_values=True)
    errors = [
        detail(f"query:{name}", "unknown_parameter", "Неизвестный параметр")
        for name in query if name not in allowed
    ]
    return query, errors


def object_fields(value: object, allowed: set[str], pointer: str = "body:") -> tuple[Mapping, list[dict]]:
    if not isinstance(value, dict):
        return {}, [detail(pointer, "invalid_type", "Нужен объект JSON")]
    errors = [
        detail(pointer + "/" + key.replace("~", "~0").replace("/", "~1"),
               "unknown_parameter", "Неизвестный параметр")
        for key in value if key not in allowed
    ]
    return value, errors


def required_text_fields(value: object, fields: tuple[str, ...]) -> dict:
    _, errors = query_fields(set())
    data, body_errors = object_fields(value, set(fields))
    errors.extend(body_errors)
    if isinstance(value, dict):
        for field in fields:
            pointer = f"body:/{field}"
            if field not in data:
                errors.append(detail(pointer, "required", "Обязательный параметр"))
            elif not isinstance(data[field], str):
                errors.append(detail(pointer, "invalid_type", "Нужна строка"))
            elif not data[field]:
                errors.append(detail(pointer, "empty", "Нужна непустая строка"))
            elif any(0xD800 <= ord(char) <= 0xDFFF for char in data[field]):
                errors.append(detail(pointer, "invalid_value", "Строка должна быть корректным Unicode"))
    invalid_parameters(errors)
    return dict(data)


def no_body_error():
    if request.environ.get("CONTENT_LENGTH") not in (None, "", "0") or request.environ.get("HTTP_TRANSFER_ENCODING"):
        return detail("body:", "unknown_parameter", "Метод не принимает тело запроса")
    return None


def no_body() -> None:
    _, errors = query_fields(set())
    if (problem := no_body_error()) is not None:
        errors.append(problem)
    invalid_parameters(errors)
