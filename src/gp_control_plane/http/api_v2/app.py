"""Production composition for the Core-only API."""

from importlib.resources import files
import os
from pathlib import Path

from bottle import Bottle, HTTPResponse

from ... import __version__
from ...application.api2_search import resume_available
from ...application.operation_admission import OperationAdmission
from ...resource_budget import JSON_REQUEST_MAX_BYTES
from ..docs import swagger_ui_html
from . import (
    register_access, register_backups, register_catalog, register_domain_lists,
    register_history, register_service, register_sources,
)
from .protocol import ApiError, json_response
from .search import register_search


def _contract():
    resource = files("gp_control_plane.http").joinpath("openapi-v2.json")
    if resource.is_file():
        return resource.read_bytes()
    checkout = Path(__file__).resolve().parents[4]
    return (checkout / "openapi-v2.json").read_bytes()


def create_api2_app(state_dir, *, admission=None, search=None, updater=None, stream_lifecycle=None):
    app = Bottle()
    if admission is None:
        admission = OperationAdmission()
    register_access(app, state_dir)
    for register in (register_catalog, register_domain_lists, register_sources, register_backups):
        register(app, state_dir, admission=admission)
    register_service(app, state_dir, admission=admission, updater=updater)
    register_history(app, state_dir, resume_available=resume_available, admission=admission)
    if search is not None:
        register_search(app, state_dir, search, stream_lifecycle=stream_lifecycle, json_limit=JSON_REQUEST_MAX_BYTES)

    @app.get("/api/health")
    def health():
        return json_response({"status": "ok"}, headers={
            "X-GP-Core-Version": __version__, "X-GP-Core-Pid": str(os.getpid()),
        })

    @app.get("/openapi.json")
    def openapi():
        return HTTPResponse(_contract(), headers={
            "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
        })

    @app.get("/swagger")
    @app.get("/swagger/")
    def swagger():
        return HTTPResponse(swagger_ui_html(), headers={"Content-Type": "text/html; charset=utf-8"})

    @app.error(404)
    def missing(_):
        return ApiError(404, "not_found", "Метод или ресурс не найден").response()

    @app.error(405)
    def unsupported(error):
        response = ApiError(405, "method_not_allowed", "Метод запроса не поддерживается").response()
        allow = error.headers.get("Allow")
        if allow:
            response.set_header("Allow", allow)
        return response

    return app
