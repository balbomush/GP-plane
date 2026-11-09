"""Production composition for the Core-only API."""

from contextlib import asynccontextmanager
from importlib.resources import files
import json
import logging
import os
from pathlib import Path
from threading import Event

from anyio import to_thread
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from starlette.exceptions import HTTPException
from starlette.routing import Match

from ... import __version__
from ...application.api2_search import SearchError, resume_available
from ...application.operation_admission import OperationAdmission
from ...resource_budget import JSON_REQUEST_MAX_BYTES
from ..docs import swagger_ui_html
from . import (
    register_access, register_backups, register_catalog, register_domain_lists,
    register_history, register_service, register_sources,
)
from .openapi import configure_openapi
from .protocol import ApiError, RejectAmbiguousFraming, as_api_error, detail, json_response
from .search import register_search


def create_api2_app(state_dir, *, admission=None, search=None, updater=None, shutdown=None):
    shutdown = shutdown if shutdown is not None else Event()

    @asynccontextmanager
    async def lifespan(app):
        to_thread.current_default_thread_limiter().total_tokens = 8
        try:
            yield
        finally:
            shutdown.set()
            if search is not None:
                try:
                    await to_thread.run_sync(search.shutdown)
                except Exception as error:
                    code = error.code if isinstance(error, SearchError) else as_api_error("search_shutdown", error).code
                    logging.getLogger(__name__).error("operation=search_shutdown error_code=%s", code)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
                  redirect_slashes=False, title="GP Plane API 2", version="2.0.0-draft.33",
                  servers=[{"url": "/api/v2"}],
                  openapi_tags=[{"name": name} for name in
                                ("Доступ", "Поиск", "Данные", "Резервные копии", "Обслуживание")])
    app.add_middleware(RejectAmbiguousFraming)
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _request_validation_error)
    app.add_exception_handler(ResponseValidationError, _response_validation_error)
    if admission is None:
        admission = OperationAdmission()
    register_access(app, state_dir)
    for register in (register_catalog, register_domain_lists, register_sources, register_backups):
        register(app, state_dir, admission=admission)
    register_service(app, state_dir, admission=admission, updater=updater)
    register_history(app, state_dir, resume_available=resume_available, admission=admission)
    if search is not None:
        register_search(app, state_dir, search, shutdown=shutdown, json_limit=JSON_REQUEST_MAX_BYTES)

    @app.get("/api/health", include_in_schema=False)
    def health():
        return json_response({"status": "ok"}, headers={
            "X-GP-Core-Version": __version__, "X-GP-Core-Pid": str(os.getpid()),
        })

    @app.get("/openapi.json", include_in_schema=False)
    def openapi():
        return json_response(app.openapi())

    @app.get("/swagger", include_in_schema=False)
    @app.get("/swagger/", include_in_schema=False)
    def swagger():
        return Response(swagger_ui_html(), headers={"Content-Type": "text/html; charset=utf-8"})

    @app.get("/swagger/assets/{asset}", include_in_schema=False)
    def swagger_asset(asset: str):
        media_type = {
            "swagger-ui.css": "text/css",
            "swagger-ui-bundle.js": "text/javascript",
            "swagger-ui-standalone-preset.js": "text/javascript",
        }.get(asset)
        if media_type is None:
            raise ApiError(404, "not_found", "Ресурс Swagger не найден")
        return Response(files(__package__).joinpath("swagger", asset).read_bytes(), media_type=media_type)

    configure_openapi(app)
    return app


async def _api_error(request: Request, error: ApiError):
    return error.response()


async def _http_error(request: Request, error: HTTPException):
    if error.status_code == 404:
        return ApiError(404, "not_found", "Метод или ресурс не найден").response()
    if error.status_code == 405:
        response = ApiError(405, "method_not_allowed", "Метод запроса не поддерживается").response()
        allowed = {
            method for route in request.app.routes
            if route.matches(request.scope)[0] is Match.PARTIAL
            for method in (getattr(route, "methods", None) or ())
        }
        if allowed:
            response.headers["Allow"] = ", ".join(sorted(allowed))
        return response
    return ApiError(error.status_code, "invalid_request", "Не удалось обработать запрос").response()


async def _request_validation_error(request: Request, error: RequestValidationError):
    problems = [detail(str(row["loc"][0]), "invalid_value", "Недопустимое значение") for row in error.errors()]
    return ApiError(400, "invalid_parameters", "Проверьте параметры запроса", problems).response()


async def _response_validation_error(request: Request, error: ResponseValidationError):
    return as_api_error(getattr(request.scope.get("route"), "name", "response"), error).response()


def verify_package() -> dict:
    """Verify the packaged schema and the local Swagger resources before cutover."""
    from ...application.api2_search import SearchController
    from ...application.core_update import CoreUpdate
    from ...engines.api2_blockcheck import BlockcheckEngine

    state_dir = Path(".")  # Composition and schema export perform no state I/O.
    admission = OperationAdmission()
    app = create_api2_app(
        state_dir, admission=admission, search=SearchController(state_dir, admission, BlockcheckEngine()),
        updater=CoreUpdate(state_dir, admission),
    )
    schema = app.openapi()
    if sum(len(methods) for methods in schema["paths"].values()) != 41:
        raise ValueError("incomplete Core API 2 schema")
    root = files(__package__).joinpath("swagger")
    for asset in ("swagger-ui.css", "swagger-ui-bundle.js", "swagger-ui-standalone-preset.js"):
        if not root.joinpath(asset).read_bytes():
            raise ValueError("incomplete Core Swagger assets")
    return schema


def _contract() -> bytes:
    # The accepted predecessor's trusted installer calls this preparation
    # probe before activating a wheel. Its data still comes from live routes.
    return json.dumps(verify_package(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
