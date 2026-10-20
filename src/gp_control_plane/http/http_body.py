"""Bounded WSGI request-body handling for the Web listener."""

from __future__ import annotations

from collections.abc import Mapping
from typing import BinaryIO

from ..resource_budget import BACKUP_UPLOAD_MAX_BYTES, JSON_REQUEST_MAX_BYTES
from .routes import UPLOAD_ROUTE_PATHS


_REJECT_DRAIN_CHUNK_BYTES = 64 * 1024


class ProxyRequestBodyTooLarge(ValueError):
    """The public request limit was exceeded before Core forwarding."""


class ProxyRequestBodyMalformed(ValueError):
    """The request framing cannot be forwarded safely."""


def request_body_limit(
    path: str,
    *,
    json_max_bytes: int | None = None,
    backup_max_bytes: int | None = None,
) -> int:
    """Return the existing route-specific public request-body limit."""

    json_limit = JSON_REQUEST_MAX_BYTES if json_max_bytes is None else json_max_bytes
    backup_limit = BACKUP_UPLOAD_MAX_BYTES if backup_max_bytes is None else backup_max_bytes
    return backup_limit if path in UPLOAD_ROUTE_PATHS else json_limit


def read_request_body(
    environ: Mapping[str, object],
    path: str,
    *,
    json_max_bytes: int | None = None,
    backup_max_bytes: int | None = None,
) -> bytes | None:
    """Read exactly the declared WSGI body or raise a public framing error.

    Web deliberately forwards raw bytes.  JSON and backup validation remain
    Core-owned; this seam only protects the listener and rejects ambiguous
    Content-Length/Transfer-Encoding framing before a Core request is opened.
    """

    transfer_encoding = str(environ.get("HTTP_TRANSFER_ENCODING") or "").strip()
    if transfer_encoding:
        raise ProxyRequestBodyMalformed("transfer encoding is not supported")

    raw_length = environ.get("CONTENT_LENGTH")
    if raw_length in {None, ""}:
        return None
    try:
        length = int(str(raw_length))
    except (TypeError, ValueError) as error:
        raise ProxyRequestBodyMalformed("invalid content length") from error
    if length < 0:
        raise ProxyRequestBodyMalformed("invalid content length")
    if length > request_body_limit(path, json_max_bytes=json_max_bytes, backup_max_bytes=backup_max_bytes):
        _drain_rejected_known_length_body(environ, length)
        raise ProxyRequestBodyTooLarge("request body is too large")
    if length == 0:
        return None

    stream = environ.get("wsgi.input")
    if not hasattr(stream, "read"):
        raise ProxyRequestBodyMalformed("request body is unavailable")
    body = _read_exact(stream, length)
    if len(body) != length:
        raise ProxyRequestBodyMalformed("truncated request body")
    return body


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    """Use one bounded read so an untrusted body cannot exceed its declared cap."""

    data = stream.read(length)
    if not isinstance(data, bytes):
        raise ProxyRequestBodyMalformed("request body is not bytes")
    return data


def _drain_rejected_known_length_body(environ: Mapping[str, object], length: int) -> None:
    """Discard an already-declared oversized body without buffering it.

    Cheroot correctly marks a 413 connection for close.  On Windows, closing a
    socket that still has an eager client's request bytes unread can turn that
    otherwise-valid response into a TCP reset before the client receives it.
    Consume only fixed-size chunks first, so the caller still gets the legacy
    JSON 413 while no rejected byte reaches Core or becomes an allocation.
    """

    stream = environ.get("wsgi.input")
    if not hasattr(stream, "read"):
        return
    remaining = length
    while remaining > 0:
        chunk = stream.read(min(remaining, _REJECT_DRAIN_CHUNK_BYTES))
        if not isinstance(chunk, bytes) or not chunk:
            return
        remaining -= len(chunk)
