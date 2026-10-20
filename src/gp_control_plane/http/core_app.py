"""Bottle application factory for the Core HTTP boundary.

The Core factory is intentionally a request dispatcher only.  Startup builds
the one :class:`JobRunner` and injects its application service; importing or
constructing this module never binds a socket, starts recovery, or imports the
HTML renderer in headless mode.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs

from bottle import Bottle, HTTPResponse, request, response

from .. import __version__, core_api, service_api
from ..application import web_views
from ..application.discovery import DiscoveryService
from ..application.web_view_contracts import (
    INTERNAL_AUTH_VERIFY_BEARER_PATH,
    INTERNAL_WEB_EVENTS_SNAPSHOT_PATH,
    INTERNAL_WEB_EVENTS_STREAM_PATH,
    INTERNAL_WEB_VIEW_GET_PATHS,
    INTERNAL_WEB_VIEW_POST_PATHS,
    external_web_view_path,
)
from ..auth import AuthenticationError, PasswordValidationError, change_password, health_payload, login, require_bearer_token
from ..backups import (
    create_post_run_snapshot,
    create_snapshot_if_idle,
    delete_snapshot_if_idle,
    import_snapshot_archive,
    restore_snapshot_if_idle,
    snapshot_file_path,
)
from ..config import AppConfig
from ..resource_budget import BACKUP_STREAM_CHUNK_BYTES, BACKUP_UPLOAD_MAX_BYTES, JSON_REQUEST_MAX_BYTES
from ..settings import read_run_settings, read_service_settings, save_run_settings
from ..state import active_job_lock_payload, has_active_runtime
from ..storage import storage_unavailable_diagnostic
from ..storage import is_storage_unavailable_error as _is_storage_unavailable_error
from ..strategy_finder import candidate_storage_version
from .docs import OPENAPI_JSON_CONTENT_TYPE, SWAGGER_HTML_CONTENT_TYPE, SWAGGER_PATHS, openapi_json_bytes, swagger_ui_html
from .cheroot_connection import WebStreamLifecycle
from .errors import error_payload, normalize_error_payload
from .http_body import ProxyRequestBodyMalformed, ProxyRequestBodyTooLarge, read_request_body
from .legacy_http import unsupported_method
from .stream_admission import ClosingIterator, StreamAdmission, stream_kind
from .routes import JSON_GET_ROUTE_PATHS, JSON_HEAD_ROUTE_PATHS, JSON_POST_ROUTE_PATHS, route_for


NDJSON_CONTENT_TYPE = "application/x-ndjson; charset=utf-8"
SSE_HEARTBEAT_SECONDS = 15.0
# Preserve the public operational logger identity while dispatch moved from the
# compatibility module into this factory.
_LOGGER = logging.getLogger("gp_control_plane.web.api_server")


class RuntimeBusyError(RuntimeError):
    """An existing Core operation still owns the runtime lock."""


class _CoreBottle(Bottle):
    """Commit genuine SSE headers before a deliberately gated first frame."""

    def wsgi(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        is_event_stream = environ.get("REQUEST_METHOD") == "GET" and environ.get("PATH_INFO") in {
            INTERNAL_WEB_EVENTS_STREAM_PATH,
            "/api/web/events/stream",
        }

        if not is_event_stream:
            return super().wsgi(environ, start_response)
        # Bottle normally peeks an iterable to infer its type before calling
        # start_response.  The first *real* SSE frame may intentionally be
        # gated; dispatch this known bytes-iterator without peeking so its
        # legitimate headers are observable before that race is released.
        out = self._handle(environ)
        try:
            if isinstance(out, HTTPResponse):
                # Authorization and route errors are ordinary finite responses.
                out = self._cast(out)
            exc_info = environ.pop("bottle.exc_info", None)
            write = start_response(response._wsgi_status_line(), response.headerlist, exc_info)
            # Empty bytes commit headers without inventing an SSE frame.
            write(b"")
            return out
        except BaseException as error:
            # Until return, the server cannot own or close this iterable.
            try:
                close = getattr(out, "close", None)
                if close is not None:
                    close()
            except BaseException as close_error:
                raise error from close_error
            raise


class AuthAdapter(Protocol):
    """Core-owned authorization seam for factory tests and normal runtime."""

    def __call__(self, state_dir: Path, authorization: str | None) -> None: ...


@dataclass
class CoreServices:
    """Already-started Core application services; no listener ownership."""

    config: AppConfig
    discovery: DiscoveryService
    runtime_role: str = "core"
    web_install_enabled: bool | None = None
    ui_enabled: bool = False
    json_max_bytes: int = JSON_REQUEST_MAX_BYTES
    backup_max_bytes: int = BACKUP_UPLOAD_MAX_BYTES
    stream_lifecycle: WebStreamLifecycle | None = None
    stream_admission: StreamAdmission = field(default_factory=StreamAdmission)
    v2fly_update_lock: threading.RLock = field(default_factory=threading.RLock)


def create_core_app(services: CoreServices, auth_adapter: AuthAdapter | None = None) -> Bottle:
    """Construct a Core WSGI app without bind/listen, threads or a runner."""

    if not isinstance(services, CoreServices):
        raise TypeError("services must be CoreServices")
    if services.ui_enabled:
        raise ValueError("Core no longer embeds Web; install GP Plane Web and run gp-web separately")
    authorize = auth_adapter or require_bearer_token
    app = _CoreBottle()

    @app.route("/<path:path>", method="ANY")
    @app.route("/", method="ANY")
    def all_routes(path: str = "") -> HTTPResponse | Iterable[bytes]:
        return _dispatch(services, authorize, "/" + path, request.method.upper(), request.query_string, request.headers, request.environ)

    return app


def _dispatch(
    services: CoreServices,
    authorize: AuthAdapter,
    path: str,
    method: str,
    query_string: str,
    headers: Mapping[str, str],
    environ: Mapping[str, object],
) -> HTTPResponse | Iterable[bytes]:
    if method not in {"GET", "HEAD", "POST"}:
        # BaseHTTPRequestHandler returned 501, without a generated Allow.
        return unsupported_method(method)
    authorization = headers.get("Authorization")
    authorization_error = _authorize(services, authorize, path, method, authorization)
    if authorization_error is not None:
        return authorization_error
    query = parse_qs(query_string)
    if method == "GET":
        if path == "/api/web/events/stream" and not services.ui_enabled:
            return _not_found()
        kind = stream_kind(method, path)
        if kind is None:
            return _get(services, authorize, path, query, authorization)
        permit = services.stream_admission.acquire(kind)
        if permit is None:
            return _json(error_payload("stream_capacity_exhausted", "Stream capacity is temporarily exhausted."), HTTPStatus.SERVICE_UNAVAILABLE)
        try:
            result = _get(services, authorize, path, query, authorization)
            if isinstance(result, HTTPResponse):
                permit.release()
                return result
            return ClosingIterator(result, permit.release)
        except BaseException:
            permit.release()
            raise
    if method == "HEAD":
        return _head(services, path)
    return _post(services, path, query, headers, environ)


def _authorize(
    services: CoreServices,
    authorize: AuthAdapter,
    path: str,
    method: str,
    authorization: str | None,
) -> HTTPResponse | None:
    if not path.startswith("/api/"):
        return None
    route = route_for(method, path)
    if route is not None and not route.auth_required:
        return None
    try:
        authorize(services.config.output.state_dir, authorization)
    except Exception as error:  # auth must classify Core state failures itself
        if isinstance(error, PermissionError) or _is_storage_unavailable_error(error):
            return _storage_unavailable(method, path, error)
        if isinstance(error, AuthenticationError):
            return _auth_error()
        raise
    return None


def _get(services: CoreServices, authorize: AuthAdapter, path: str, query: dict[str, list[str]], authorization: str | None) -> HTTPResponse | Iterable[bytes]:
    config = services.config
    if path == "/":
        return _json({"error": "web ui is disabled in core mode"}, HTTPStatus.NOT_FOUND)
    if path == INTERNAL_AUTH_VERIFY_BEARER_PATH:
        return _json({"authorized": True})
    if path == INTERNAL_WEB_EVENTS_SNAPSHOT_PATH:
        return _json(web_views.events_snapshot_payload(config, query))
    if path == INTERNAL_WEB_EVENTS_STREAM_PATH:
        return _events(services, authorize, authorization, path)
    if path in INTERNAL_WEB_VIEW_GET_PATHS:
        return _call_json(lambda: web_views.get_payload(config, external_web_view_path(path), query), HTTPStatus.BAD_REQUEST, "GET", path)
    if path == "/openapi.json":
        try:
            return _bytes(openapi_json_bytes(core_only=not services.ui_enabled), OPENAPI_JSON_CONTENT_TYPE, no_store=True)
        except OSError:
            return _json({"error": "openapi contract is not available"}, HTTPStatus.NOT_FOUND)
    if path in SWAGGER_PATHS:
        return _bytes(swagger_ui_html().encode("utf-8"), SWAGGER_HTML_CONTENT_TYPE, no_store=True)
    if path == "/api/core/strategy-candidates/export":
        return _candidate_export(config, query)
    if path == "/api/core/backups/download-archive":
        return _download_backup(config, {"snapshot": [_query_one(query, "snapshot_id")], "file": ["archive"]})
    if path == "/api/web/events/stream":
        return _events(services, authorize, authorization, path) if services.ui_enabled else _not_found()
    if path in JSON_GET_ROUTE_PATHS:
        if path.startswith("/api/web/"):
            if not services.ui_enabled:
                return _not_found()
            return _call_json(lambda: web_views.get_payload(config, path, query), HTTPStatus.BAD_REQUEST, "GET", path)
        return _core_get(services, path, query)
    return _not_found()


def _head(services: CoreServices, path: str) -> HTTPResponse:
    if path == "/":
        return _head_response(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)
    if path in {INTERNAL_AUTH_VERIFY_BEARER_PATH, INTERNAL_WEB_EVENTS_SNAPSHOT_PATH, *INTERNAL_WEB_VIEW_GET_PATHS}:
        return _head_response(HTTPStatus.OK, "application/json; charset=utf-8", 0)
    if path == INTERNAL_WEB_EVENTS_STREAM_PATH:
        return _head_response(HTTPStatus.OK, "text/event-stream; charset=utf-8", 0)
    if path == "/openapi.json":
        try:
            return _head_response(HTTPStatus.OK, OPENAPI_JSON_CONTENT_TYPE, len(openapi_json_bytes(core_only=not services.ui_enabled)), no_store=True)
        except OSError:
            return _head_response(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)
    if path in SWAGGER_PATHS:
        return _head_response(HTTPStatus.OK, SWAGGER_HTML_CONTENT_TYPE, len(swagger_ui_html().encode("utf-8")), no_store=True)
    if path == "/api/core/strategy-candidates/export":
        return _head_response(HTTPStatus.OK, NDJSON_CONTENT_TYPE, 0)
    if path == "/api/web/events/stream" and services.ui_enabled:
        return _head_response(HTTPStatus.OK, "text/event-stream; charset=utf-8", 0)
    if path.startswith("/api/web/") and not services.ui_enabled:
        return _head_response(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)
    if path in JSON_HEAD_ROUTE_PATHS:
        return _head_response(HTTPStatus.OK, "application/json; charset=utf-8", 0)
    return _head_response(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)


def _post(
    services: CoreServices,
    path: str,
    query: dict[str, list[str]],
    headers: Mapping[str, str],
    environ: Mapping[str, object],
) -> HTTPResponse:
    del query
    config = services.config
    if path == "/api/core/backups/upload":
        try:
            if has_active_runtime(config.output.state_dir):
                raise RuntimeBusyError()
            uploaded = _request_upload_bytes(services, environ, path, headers)
            imported = import_snapshot_archive(config.output.state_dir, uploaded)
            return _json(core_api.backup_snapshot_payload(imported.get("snapshot") or {}), HTTPStatus.CREATED)
        except Exception as error:  # retain the public legacy classification
            if _is_storage_unavailable_error(error):
                return _storage_unavailable("POST", path, error)
            if isinstance(error, RuntimeBusyError):
                return _json({"error": "runtime_busy"}, HTTPStatus.CONFLICT)
            if isinstance(error, ProxyRequestBodyTooLarge):
                return _json({"error": str(error)}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return _json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
    # Preserve the legacy route boundary: unknown and headless-Web routes are
    # classified after authorization but before an untrusted request body is
    # read or sized.
    if path not in INTERNAL_WEB_VIEW_POST_PATHS and path not in JSON_POST_ROUTE_PATHS:
        return _not_found()
    if path.startswith("/api/web/") and not services.ui_enabled:
        return _not_found()
    try:
        payload = _request_json(services, environ, path)
    except ProxyRequestBodyTooLarge as error:
        return _json({"error": str(error)}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    except Exception as error:
        return _json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
    if path in INTERNAL_WEB_VIEW_POST_PATHS:
        return _dispatch_post(lambda: (web_views.post_response(config, external_web_view_path(path), payload), HTTPStatus.OK), HTTPStatus.BAD_REQUEST, HTTPStatus.BAD_REQUEST, "POST", path)
    routes = _post_routes(services, payload, headers)
    return _dispatch_post(*routes[path], method="POST", path=path)


def _core_get(services: CoreServices, path: str, query: dict[str, list[str]]) -> HTTPResponse:
    config = services.config

    def catalog(action: Callable[[], Any]) -> Any:
        with services.v2fly_update_lock:
            return action()

    routes: dict[str, Callable[[], dict[str, Any]]] = {
        "/api/health": health_payload,
        "/api/core/status": lambda: core_api.status_payload(config),
        "/api/core/strategy-discovery/current-run-progress": lambda: core_api.current_progress_payload(config),
        "/api/core/strategy-discovery/current-run-latest-log": lambda: _legacy()._current_run_latest_log_payload(config, query),
        "/api/core/strategy-discovery/preflight": lambda: core_api.preflight_payload(config),
        "/api/core/presets/domain-lists": lambda: core_api.domain_lists_payload(config),
        "/api/core/presets/v2fly/categories": lambda: catalog(lambda: core_api.v2fly_categories_payload(config, query)),
        "/api/core/presets/v2fly/category-domains": lambda: catalog(lambda: core_api.v2fly_category_domains_payload(config, query)),
        "/api/core/backups/list": lambda: core_api.backups_list_payload(config),
        "/api/core/clean-install-vaults/list": lambda: {"vaults": [_legacy()._clean_install_vault_public_metadata(item) for item in (core_api.clean_install_vault_list_payload(config).get("vaults") or []) if isinstance(item, dict)]},
        "/api/core/clean-install-vaults/status": lambda: _legacy()._clean_install_vault_public_metadata(core_api.clean_install_vault_status_payload(config, query)),
        "/api/core/run-settings": lambda: core_api.run_settings_payload(read_run_settings(config)),
        "/api/core/runs/history": lambda: core_api.runs_history_payload(config, query),
        "/api/core/runs/latest-log": lambda: _legacy()._latest_log_payload(config, query),
        "/api/core/strategy-candidates": lambda: core_api.strategy_candidates_payload(config, query),
        "/api/core/events": lambda: _legacy()._events_response_payload(config, query, stream="core"),
        "/api/service/status": lambda: catalog(lambda: service_api.service_status_payload(config, current_version=__version__, runtime_role=services.runtime_role, web_enabled=services.web_install_enabled)),
        "/api/service/releases/available": lambda: service_api.available_releases_payload(read_service_settings(config), current_version=__version__),
        "/api/service/v2fly/local-storage-status": lambda: catalog(lambda: service_api.v2fly_storage_status_payload(config)),
    }
    try:
        return _json(routes[path]())
    except Exception as error:
        if _is_storage_unavailable_error(error):
            return _storage_unavailable("GET", path, error)
        if path == "/api/core/clean-install-vaults/status" and isinstance(error, FileNotFoundError):
            return _json(error_payload("not_found", "Clean-install vault was not found."), HTTPStatus.NOT_FOUND)
        if path in {"/api/core/clean-install-vaults/list", "/api/core/clean-install-vaults/status"}:
            return _json(error_payload("invalid_request", str(error)), HTTPStatus.BAD_REQUEST)
        if path in {"/api/core/presets/v2fly/category-domains", "/api/core/strategy-candidates"}:
            return _json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        # A WSGI listener must not turn an unexpected controller exception
        # into a half-written response or a worker traceback.  It is neither
        # a storage 503 nor a success; preserve a finite honest 500 envelope.
        return _json({"error": "unexpected_error"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def _post_routes(services: CoreServices, payload: dict[str, Any], headers: Mapping[str, str]) -> dict[str, tuple[Callable[[], tuple[dict[str, Any], HTTPStatus]], HTTPStatus, HTTPStatus]]:
    config = services.config

    def stop_current_run() -> tuple[dict[str, Any], HTTPStatus]:
        result = services.discovery.cancel(dry_run=bool(payload.get("dry_run")))
        return (result if payload.get("dry_run") else core_api.action_accepted_payload(result), HTTPStatus.ACCEPTED)

    def start_strategy_discovery() -> tuple[dict[str, Any], HTTPStatus]:
        return core_api.run_accepted_payload(services.discovery.start_payload(payload)), HTTPStatus.ACCEPTED

    def create_core_backup() -> tuple[dict[str, Any], HTTPStatus]:
        created = create_snapshot_if_idle(config.output.state_dir)
        if created.get("queued"):
            raise RuntimeBusyError()
        return core_api.backup_snapshot_payload(created.get("snapshot") or {}), HTTPStatus.CREATED

    def restore_core_backup() -> tuple[dict[str, Any], HTTPStatus]:
        snapshot_id = core_api.payload_snapshot_id(payload)
        restored = restore_snapshot_if_idle(config.output.state_dir, snapshot_id)
        if restored.get("queued"):
            raise RuntimeBusyError()
        return {"accepted": True, "status": "success", "snapshot_id": snapshot_id}, HTTPStatus.ACCEPTED

    def delete_core_backup() -> tuple[dict[str, Any], HTTPStatus]:
        snapshot_id = core_api.payload_snapshot_id(payload)
        deleted = delete_snapshot_if_idle(config.output.state_dir, snapshot_id)
        if deleted.get("queued"):
            raise RuntimeBusyError()
        return {"deleted": 1}, HTTPStatus.OK

    def create_clean_install_vault() -> tuple[dict[str, Any], HTTPStatus]:
        if payload:
            raise ValueError("clean-install vault create does not accept request fields")
        return _legacy()._clean_install_vault_create_response(core_api.clean_install_vault_create_payload(config, payload)), HTTPStatus.CREATED

    def restore_clean_install_vault() -> tuple[dict[str, Any], HTTPStatus]:
        allowed = {"vault_id", "confirm_restore"}
        unknown = sorted(str(key) for key in payload if str(key) not in allowed)
        if unknown:
            raise ValueError(f"unsupported clean-install vault restore fields: {', '.join(unknown)}")
        restored = core_api.clean_install_vault_restore_payload(config, payload)
        result = _legacy()._clean_install_vault_restore_response(restored, "")
        if not (result["completed"] and result["verification"]["verified"] and result["storage_status"]["ready"] and result["cleanup"]["source_deleted"]):
            raise RuntimeError("clean-install vault restore did not complete; source retained")
        return result, HTTPStatus.OK

    def ensure_service_action_idle() -> None:
        if active_job_lock_payload(config.output.state_dir, cleanup_stale=True):
            raise RuntimeError("service action is blocked while another job is running")

    def v2fly_check_updates() -> tuple[dict[str, Any], HTTPStatus]:
        ensure_service_action_idle()
        with services.v2fly_update_lock:
            return service_api.v2fly_check_updates_payload(config), HTTPStatus.OK

    def v2fly_update_local_storage() -> tuple[dict[str, Any], HTTPStatus]:
        if not payload.get("dry_run"):
            ensure_service_action_idle()
            if not services.v2fly_update_lock.acquire(blocking=False):
                raise RuntimeError("v2fly catalog update is already running")
            try:
                return service_api.v2fly_update_local_storage_payload(config, payload), HTTPStatus.OK
            finally:
                services.v2fly_update_lock.release()
        with services.v2fly_update_lock:
            return service_api.v2fly_update_local_storage_payload(config, payload), HTTPStatus.OK

    return {
        "/api/auth/login": (lambda: (login(config.output.state_dir, payload), HTTPStatus.OK), HTTPStatus.UNAUTHORIZED, HTTPStatus.BAD_REQUEST),
        "/api/auth/change-password": (lambda: (change_password(config.output.state_dir, payload, headers.get("Authorization")), HTTPStatus.OK), HTTPStatus.UNAUTHORIZED, HTTPStatus.BAD_REQUEST),
        "/api/core/strategy-discovery/stop-current-run": (stop_current_run, HTTPStatus.CONFLICT, HTTPStatus.CONFLICT),
        "/api/core/strategy-discovery/start-run": (start_strategy_discovery, HTTPStatus.CONFLICT, HTTPStatus.BAD_REQUEST),
        "/api/core/presets/save-domain-list": (lambda: (core_api.save_domain_list_payload(config, payload), HTTPStatus.OK), HTTPStatus.BAD_REQUEST, HTTPStatus.BAD_REQUEST),
        "/api/core/presets/delete-user-domain-list": (lambda: (core_api.delete_user_domain_list_payload(config, payload), HTTPStatus.OK), HTTPStatus.BAD_REQUEST, HTTPStatus.BAD_REQUEST),
        "/api/core/backups/create": (create_core_backup, HTTPStatus.CONFLICT, HTTPStatus.CONFLICT),
        "/api/core/backups/restore": (restore_core_backup, HTTPStatus.CONFLICT, HTTPStatus.CONFLICT),
        "/api/core/backups/delete": (delete_core_backup, HTTPStatus.CONFLICT, HTTPStatus.CONFLICT),
        "/api/core/clean-install-vaults/create": (create_clean_install_vault, HTTPStatus.CONFLICT, HTTPStatus.BAD_REQUEST),
        "/api/core/clean-install-vaults/restore": (restore_clean_install_vault, HTTPStatus.CONFLICT, HTTPStatus.BAD_REQUEST),
        "/api/core/run-settings/save": (lambda: (core_api.run_settings_payload(save_run_settings(config, payload.get("settings") or payload)), HTTPStatus.OK), HTTPStatus.BAD_REQUEST, HTTPStatus.BAD_REQUEST),
        "/api/service/v2fly/check-updates": (v2fly_check_updates, HTTPStatus.CONFLICT, HTTPStatus.BAD_REQUEST),
        "/api/service/v2fly/update-local-storage": (v2fly_update_local_storage, HTTPStatus.CONFLICT, HTTPStatus.BAD_REQUEST),
        "/api/web/run-preferences": (lambda: (web_views.post_response(config, "/api/web/run-preferences", payload), HTTPStatus.OK), HTTPStatus.BAD_REQUEST, HTTPStatus.BAD_REQUEST),
        "/api/web/presets/save": (lambda: (web_views.post_response(config, "/api/web/presets/save", payload), HTTPStatus.OK), HTTPStatus.BAD_REQUEST, HTTPStatus.BAD_REQUEST),
        "/api/web/presets/delete-user-lists": (lambda: (web_views.post_response(config, "/api/web/presets/delete-user-lists", payload), HTTPStatus.OK), HTTPStatus.BAD_REQUEST, HTTPStatus.BAD_REQUEST),
    }


def _dispatch_post(handler: Callable[[], tuple[dict[str, Any], HTTPStatus]], error_status: HTTPStatus, value_error_status: HTTPStatus, method: str, path: str) -> HTTPResponse:
    try:
        payload, status = handler()
    except Exception as error:
        if _is_storage_unavailable_error(error):
            return _storage_unavailable(method, path, error)
        if isinstance(error, AuthenticationError):
            return _auth_error()
        if isinstance(error, PasswordValidationError):
            return _json(error_payload("invalid_request", str(error)), HTTPStatus.BAD_REQUEST)
        if isinstance(error, RuntimeBusyError):
            return _json({"error": "runtime_busy"}, HTTPStatus.CONFLICT)
        if isinstance(error, ValueError):
            return _json({"error": str(error)}, value_error_status)
        return _json({"error": str(error)}, error_status)
    return _json(payload, status)


def _events(services: CoreServices, authorize: AuthAdapter, authorization: str | None, path: str) -> Iterable[bytes]:
    config = services.config
    lifecycle = services.stream_lifecycle
    peer = _stream_peer(request.environ)
    terminal = threading.Event()
    close = terminal.set
    stream_id = lifecycle.register(peer, close) if lifecycle is not None else None

    def stream() -> Iterable[bytes]:
        previous: dict[str, str] = {}
        heartbeat_at = 0.0
        try:
            while not terminal.is_set():
                try:
                    authorize(config.output.state_dir, authorization)
                    for event_name, payload in _legacy()._event_payloads(config).items():
                        try:
                            fingerprint = _legacy()._event_fingerprint(payload)
                            if previous.get(event_name) == fingerprint:
                                continue
                            previous[event_name] = fingerprint
                            authorize(config.output.state_dir, authorization)
                            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                            yield f"event: {event_name}\ndata: {data}\n\n".encode("utf-8")
                        except (TypeError, ValueError) as error:
                            authorize(config.output.state_dir, authorization)
                            data = json.dumps(
                                {"event": event_name, "error": "serialization", "message": str(error)},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            yield f"event: event-error\ndata: {data}\n\n".encode("utf-8")
                    now = time.monotonic()
                    if now - heartbeat_at >= SSE_HEARTBEAT_SECONDS:
                        authorize(config.output.state_dir, authorization)
                        yield b": keepalive\n\n"
                        heartbeat_at = now
                    time.sleep(1)
                except AuthenticationError:
                    return
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return
                except Exception as error:
                    if _is_storage_unavailable_error(error):
                        _log_storage_unavailable("GET", path, error)
                        data = json.dumps({"error": "storage_unavailable", "message": "Storage is temporarily unavailable."}, ensure_ascii=False, separators=(",", ":"))
                        yield f"event: event-error\ndata: {data}\n\n".encode("utf-8")
                        return
                    try:
                        authorize(config.output.state_dir, authorization)
                        data = json.dumps({"error": "event-loop", "message": str(error)}, ensure_ascii=False, separators=(",", ":"))
                        yield f"event: event-error\ndata: {data}\n\n".encode("utf-8")
                    except (AuthenticationError, BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                        return
                    except Exception:
                        # A terminal Core state teardown can make a later auth
                        # recheck fail after stream headers have committed.
                        return
                    time.sleep(1)
        finally:
            if lifecycle is not None and stream_id is not None:
                lifecycle.release(stream_id, peer, close)

    def release() -> None:
        terminal.set()
        if lifecycle is not None and stream_id is not None:
            lifecycle.release(stream_id, peer, close)

    return _stream_response(ClosingIterator(stream(), release), "text/event-stream; charset=utf-8", no_store=True)


def _candidate_export(config: AppConfig, query: dict[str, list[str]]) -> HTTPResponse | Iterable[bytes]:
    try:
        iterator = core_api.iter_strategy_candidates_export_lines(config, query)
        first_line = next(iterator)
    except ValueError as error:
        return _json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
    except StopIteration:
        first_line = None
    except Exception as error:
        if _is_storage_unavailable_error(error):
            return _storage_unavailable("GET", "/api/core/strategy-candidates/export", error)
        raise

    def stream() -> Iterable[bytes]:
        if first_line is not None:
            yield first_line
        try:
            yield from iterator
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except Exception as error:
            if _is_storage_unavailable_error(error):
                _log_storage_unavailable("GET", "/api/core/strategy-candidates/export", error)
                return
            raise

    return _stream_response(stream(), NDJSON_CONTENT_TYPE, no_store=True)


def _download_backup(config: AppConfig, query: dict[str, list[str]]) -> HTTPResponse | Iterable[bytes]:
    try:
        path = snapshot_file_path(config.output.state_dir, _query_one(query, "snapshot"), _query_one(query, "file") or "archive")
    except Exception as error:
        if _is_storage_unavailable_error(error):
            return _storage_unavailable("GET", "/api/core/backups/download-archive", error)
        return _not_found()
    # This endpoint's existing contract is ZIP, independent of host mimetype
    # tables (Windows can otherwise emit application/x-zip-compressed).
    content_type = "application/zip"

    def stream() -> Iterable[bytes]:
        with path.open("rb") as handle:
            while chunk := handle.read(BACKUP_STREAM_CHUNK_BYTES):
                yield chunk

    return _stream_response(stream(), content_type, headers={"Content-Length": str(path.stat().st_size), "Content-Disposition": f'attachment; filename="{path.name}"'})


def _request_json(services: CoreServices, environ: Mapping[str, object], path: str) -> dict[str, Any]:
    raw = read_request_body(environ, path, json_max_bytes=services.json_max_bytes, backup_max_bytes=services.backup_max_bytes)
    if not raw or not raw.strip():
        return {}
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("request body must be a JSON object")
    return parsed


def _request_upload_bytes(services: CoreServices, environ: Mapping[str, object], path: str, headers: Mapping[str, str]) -> bytes:
    body = read_request_body(environ, path, json_max_bytes=services.json_max_bytes, backup_max_bytes=services.backup_max_bytes)
    if not body:
        raise ValueError("empty backup upload")
    content_type = str(headers.get("Content-Type") or "")
    if content_type.startswith("application/zip") or content_type.startswith("application/octet-stream"):
        return body
    if content_type.startswith("multipart/form-data"):
        marker = "boundary="
        if marker not in content_type:
            raise ValueError("multipart boundary is missing")
        return _multipart_file_bytes(body, content_type.split(marker, 1)[1].strip().strip('"'))
    raise ValueError("expected zip upload")


def _multipart_file_bytes(body: bytes, boundary: str) -> bytes:
    marker = ("--" + boundary).encode("utf-8")
    for part in body.split(marker):
        if b"filename=" not in part or b"\r\n\r\n" not in part:
            continue
        value = part.split(b"\r\n\r\n", 1)[1]
        return value.rsplit(b"\r\n", 1)[0]
    raise ValueError("multipart file is missing")


def _call_json(action: Callable[[], dict[str, Any]], error_status: HTTPStatus, method: str, path: str) -> HTTPResponse:
    try:
        return _json(action())
    except Exception as error:
        if _is_storage_unavailable_error(error):
            return _storage_unavailable(method, path, error)
        return _json({"error": str(error)}, error_status)


def _json(payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> HTTPResponse:
    data = json.dumps(normalize_error_payload(payload, status), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return HTTPResponse(status=status, body=b"" if request.method.upper() == "HEAD" else data, headers={"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(data))})


def _bytes(data: bytes, content_type: str, *, no_store: bool = False) -> HTTPResponse:
    headers = {"Content-Type": content_type, "Content-Length": str(len(data))}
    if no_store:
        headers["Cache-Control"] = "no-store"
    return HTTPResponse(status=HTTPStatus.OK, body=b"" if request.method.upper() == "HEAD" else data, headers=headers)


def _empty(status: HTTPStatus) -> HTTPResponse:
    return HTTPResponse(status=status, body=b"", headers={"Content-Length": "0"})


def _head_response(status: HTTPStatus, content_type: str, length: int, *, no_store: bool = False) -> HTTPResponse:
    headers = {"Content-Type": content_type, "Content-Length": str(length)}
    if no_store:
        headers["Cache-Control"] = "no-store"
    return HTTPResponse(status=status, body=b"", headers=headers)


def _stream_response(stream: Iterable[bytes], content_type: str, *, no_store: bool = False, headers: dict[str, str] | None = None) -> Iterable[bytes]:
    from bottle import response

    response.status = HTTPStatus.OK
    response.set_header("Content-Type", content_type)
    if no_store:
        response.set_header("Cache-Control", "no-store")
    for key, value in (headers or {}).items():
        response.set_header(key, value)
    return stream


def _not_found() -> HTTPResponse:
    return _json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def _auth_error() -> HTTPResponse:
    payload = error_payload("authentication_required", "A Bearer token is required.")
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return HTTPResponse(status=HTTPStatus.UNAUTHORIZED, body=b"" if request.method.upper() == "HEAD" else data, headers={"Content-Type": "application/json; charset=utf-8", "WWW-Authenticate": "Bearer", "Content-Length": str(len(data))})


def _storage_unavailable(method: str, path: str, error: BaseException | None = None) -> HTTPResponse:
    _log_storage_unavailable(method, path, error)
    return _json(error_payload("storage_unavailable", "Storage is temporarily unavailable."), HTTPStatus.SERVICE_UNAVAILABLE)


def _log_storage_unavailable(method: str, path: str, error: BaseException | None = None) -> None:
    details = storage_unavailable_diagnostic(error)
    _LOGGER.warning("storage unavailable operation=%s route=%s sqlite_primary_code=%s sqlite_extended_code=%s sqlite_errorname=%s exception_type=%s", method, path, details["sqlite_primary_code"], details["sqlite_extended_code"], details["sqlite_errorname"], details["exception_type"])


def _query_one(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[0]) if values else ""


def _stream_peer(environ: Mapping[str, object]) -> tuple[str, int] | None:
    try:
        return str(environ.get("REMOTE_ADDR")), int(str(environ.get("REMOTE_PORT")))
    except (TypeError, ValueError):
        return None


def _legacy() -> Any:
    """Temporary home of pure compatibility payload helpers during A6 move."""

    from . import api_server

    return api_server


__all__ = ["CoreServices", "RuntimeBusyError", "SSE_HEARTBEAT_SECONDS", "create_core_app"]
