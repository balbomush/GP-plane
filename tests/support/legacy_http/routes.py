from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .docs import SWAGGER_PATHS


@dataclass(frozen=True)
class RouteSpec:
    path: str
    methods: frozenset[str]
    namespace: str
    dispatch: str
    openapi: bool = False
    allowed_in_core: bool = True
    auth_required: bool = True


def route_for(method: str, path: str) -> RouteSpec | None:
    return _ROUTE_INDEX.get((method.upper(), path))


def route_paths(*, method: str | None = None, namespace: str | None = None, dispatch: str | None = None) -> set[str]:
    method_filter = method.upper() if method else None
    return {
        spec.path
        for spec in ROUTES
        if (method_filter is None or method_filter in spec.methods)
        and (namespace is None or spec.namespace == namespace)
        and (dispatch is None or spec.dispatch == dispatch)
    }


def openapi_operations(*, core_only: bool = False) -> set[tuple[str, str]]:
    return {
        (spec.path, method)
        for spec in ROUTES
        if spec.openapi and (not core_only or spec.allowed_in_core)
        for method in spec.methods
        if method != "HEAD"
    }


def _route(
    path: str,
    methods: Iterable[str],
    namespace: str,
    dispatch: str,
    *,
    openapi: bool = False,
    allowed_in_core: bool = True,
    auth_required: bool = True,
) -> RouteSpec:
    return RouteSpec(
        path=path,
        methods=frozenset(method.upper() for method in methods),
        namespace=namespace,
        dispatch=dispatch,
        openapi=openapi,
        allowed_in_core=allowed_in_core,
        auth_required=auth_required,
    )


ROUTES = (
    _route("/", {"GET", "HEAD"}, "web", "html", allowed_in_core=False),
    _route("/openapi.json", {"GET", "HEAD"}, "openapi", "openapi-json"),
    *(
        _route(path, {"GET", "HEAD"}, "openapi", "swagger-ui")
        for path in sorted(SWAGGER_PATHS)
    ),
    _route("/api/health", {"GET", "HEAD"}, "auth", "json-get", openapi=True, auth_required=False),
    _route("/api/auth/login", {"POST"}, "auth", "json-post", openapi=True, auth_required=False),
    _route("/api/auth/change-password", {"POST"}, "auth", "json-post", openapi=True),
    _route("/api/core/status", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/strategy-discovery/start-run", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/strategy-discovery/stop-current-run", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/strategy-discovery/current-run-progress", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/strategy-discovery/current-run-latest-log", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/strategy-discovery/preflight", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/presets/domain-lists", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/presets/save-domain-list", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/presets/delete-user-domain-list", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/presets/v2fly/categories", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/presets/v2fly/category-domains", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/backups/create", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/backups/list", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/backups/restore", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/backups/delete", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/backups/download-archive", {"GET"}, "core", "download", openapi=True),
    _route("/api/core/backups/upload", {"POST"}, "core", "upload", openapi=True),
    _route("/api/core/clean-install-vaults/create", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/clean-install-vaults/list", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/clean-install-vaults/status", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/clean-install-vaults/restore", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/run-settings", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/run-settings/save", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/runs/history", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/runs/latest-log", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/strategy-candidates", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/strategy-candidates/export", {"GET", "HEAD"}, "core", "ndjson-stream", openapi=True),
    _route("/api/core/events", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/service/status", {"GET"}, "service", "json-get", openapi=True),
    _route("/api/service/releases/available", {"GET"}, "service", "json-get", openapi=True),
    _route("/api/service/v2fly/local-storage-status", {"GET"}, "service", "json-get", openapi=True),
    _route("/api/service/v2fly/check-updates", {"POST"}, "service", "json-post", openapi=True),
    _route("/api/service/v2fly/update-local-storage", {"POST"}, "service", "json-post", openapi=True),
    _route("/api/web/status", {"GET"}, "web", "json-get", openapi=True, allowed_in_core=False),
    _route("/api/web/run-preferences", {"GET", "POST", "HEAD"}, "web", "json-get-post", openapi=True, allowed_in_core=False),
    _route("/api/web/runs/history-page", {"GET"}, "web", "json-get", openapi=True, allowed_in_core=False),
    _route("/api/web/candidate-domain-index-page", {"GET"}, "web", "json-get", openapi=True, allowed_in_core=False),
    _route("/api/web/strategy-candidates-page", {"GET"}, "web", "json-get", openapi=True, allowed_in_core=False),
    _route("/api/web/presets", {"GET"}, "web", "json-get", openapi=True, allowed_in_core=False),
    _route("/api/web/presets/domains", {"GET"}, "web", "json-get", openapi=True, allowed_in_core=False),
    _route("/api/web/presets/save", {"POST"}, "web", "json-post", openapi=True, allowed_in_core=False),
    _route("/api/web/presets/delete-user-lists", {"POST"}, "web", "json-post", openapi=True, allowed_in_core=False),
    _route("/api/web/events", {"GET"}, "web", "json-get", openapi=True, allowed_in_core=False),
    _route("/api/web/events/stream", {"GET", "HEAD"}, "web", "sse", openapi=True, allowed_in_core=False),
)

_ROUTE_INDEX = {(method, spec.path): spec for spec in ROUTES for method in spec.methods}
JSON_GET_ROUTE_PATHS = frozenset(
    spec.path for spec in ROUTES if "GET" in spec.methods and spec.dispatch in {"json-get", "json-get-post"}
)
JSON_POST_ROUTE_PATHS = frozenset(
    spec.path for spec in ROUTES if "POST" in spec.methods and spec.dispatch in {"json-post", "json-get-post"}
)
JSON_HEAD_ROUTE_PATHS = frozenset(
    spec.path for spec in ROUTES if "HEAD" in spec.methods and spec.dispatch in {"json-get", "manual-json", "json-get-post"}
)
UPLOAD_ROUTE_PATHS = frozenset(spec.path for spec in ROUTES if spec.dispatch == "upload")
