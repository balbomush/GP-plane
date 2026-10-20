from __future__ import annotations

from http import HTTPStatus
from typing import Any


_DEFAULTS: dict[int, tuple[str, str]] = {
    HTTPStatus.BAD_REQUEST: ("invalid_request", "The request is invalid."),
    HTTPStatus.UNAUTHORIZED: ("authentication_required", "A Bearer token is required."),
    HTTPStatus.FORBIDDEN: ("forbidden", "The operation is not permitted."),
    HTTPStatus.NOT_FOUND: ("not_found", "The resource was not found."),
    HTTPStatus.CONFLICT: ("conflict", "The operation conflicts with the current state."),
    HTTPStatus.REQUEST_ENTITY_TOO_LARGE: ("request_too_large", "The request body is too large."),
    HTTPStatus.BAD_GATEWAY: ("core_unavailable", "The Core API is temporarily unavailable."),
}

_KNOWN_ERRORS: dict[str, tuple[str, str]] = {
    "runtime_busy": ("runtime_busy", "The operation is unavailable while discovery is running."),
    "not found": ("not_found", "The resource was not found."),
    "core api is unavailable": ("core_unavailable", "The Core API is temporarily unavailable."),
}


def error_payload(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the sole JSON error shape exposed by Core and Web APIs."""
    return {
        "error": {
            "code": str(code or "unexpected_error"),
            "message": str(message or "Internal server error."),
            "details": dict(details or {}),
        }
    }


def normalize_error_payload(payload: dict[str, Any], status: HTTPStatus) -> dict[str, Any]:
    """Return a stable public error envelope without exposing exception text."""
    if int(status) < HTTPStatus.BAD_REQUEST:
        return payload

    raw_error = payload.get("error")
    if isinstance(raw_error, dict):
        code = str(raw_error.get("code") or "unexpected_error")
        message = str(raw_error.get("message") or "Internal server error.")
        details = raw_error.get("details")
        return error_payload(code, message, details if isinstance(details, dict) else {})

    if isinstance(raw_error, str):
        known = _KNOWN_ERRORS.get(raw_error.strip().lower())
        if known:
            return error_payload(*known)

    code, message = _DEFAULTS.get(int(status), ("unexpected_error", "Internal server error."))
    return error_payload(code, message)
