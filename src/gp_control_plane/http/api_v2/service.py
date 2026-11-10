"""Core status and acquisition of a verified, versioned supply."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response

from ... import __version__, core_supply, storage, user_backup
from ...application.core_update import UpdateError, update_identity
from ...application.operation_admission import OperationAdmission
from ...resource_budget import JSON_REQUEST_MAX_BYTES
from .access import authorized
from .models import UpdateRequest, VersionRequest
from .openapi import describe
from .protocol import ApiError, detail, json_body, no_body, required_text_fields
from .query import Query


_ROOT = "/api/v2/service"


def _upstream_error(error):
    if core_supply.is_rate_limit(error):
        return ApiError(503, "upstream_rate_limited", "Внешний каталог ограничил запросы")
    return ApiError(502, "upstream_error", "Не удалось получить проверенную поставку Core")


def register_service(
    app: FastAPI, state_dir: Path, *, admission: OperationAdmission,
    json_limit: int = JSON_REQUEST_MAX_BYTES, updater=None,
) -> None:
    authorize = authorized(state_dir)

    @app.get(_ROOT + "/status", **describe("getCoreStatus"))
    @authorize
    def status(request: Request):
        no_body(request)
        try:
            with storage.open_api2(state_dir) as conn:
                conn.execute("SELECT 1 FROM milestones LIMIT 1")
            readiness = "ready"
        except (OSError, ValueError, storage.StorageUnavailableError):
            readiness = "error"
        operation = admission.active
        return {
            "core_version": __version__, "api_version": "2", "storage": readiness,
            "busy": operation is not None, "operation": operation,
            "backup_exists": user_backup.saved_exists(state_dir),
        }

    @app.get(_ROOT + "/releases", **describe("listReleases"))
    @authorize
    def list_releases(request: Request):
        query = Query(request, {"tag"})
        tag = query.text("tag", values={"alpha", "stable", "prerelease"}, default="stable")
        query.check()
        try:
            items = core_supply.list_releases(tag)
        except (OSError, ValueError) as error:
            raise _upstream_error(error) from error
        return {"items": items}

    @app.post(_ROOT + "/download", **describe("downloadCore"))
    @authorize
    def download(request: Request):
        body = required_text_fields(request, json_body(request, json_limit), VersionRequest)
        version, tag = body["version"], body["tag"]
        with admission.claim("program_download"):
            try:
                supply = core_supply.prepare(state_dir, version, tag)
            except (OSError, ValueError) as error:
                raise _upstream_error(error) from error
            if supply is None:
                raise ApiError(404, "not_found", "Точная поставка Core не найдена")
        return {"version": version, "tag": tag, "size_bytes": supply["size_bytes"]}

    if updater is None:
        return

    @app.get(_ROOT + "/updates/{update_id}", **describe("getCoreUpdate"))
    @authorize
    def update_report(request: Request, update_id: str):
        query = Query(request, set())
        key = update_identity(update_id)
        if key is None:
            query.errors.append(detail("path:update_id", "invalid_value", "Нужен UUID обновления"))
        query.check()
        try:
            return updater.read(key)
        except UpdateError as error:
            raise ApiError(error.status, error.code, str(error)) from error

    @app.post(_ROOT + "/updates", **describe("startCoreUpdate"))
    @authorize
    def start_update(request: Request, response: Response):
        body = required_text_fields(request, json_body(request, json_limit), UpdateRequest)
        key = update_identity(body["update_id"])
        try:
            status, report = updater.start(key, body["version"], body["tag"])
            response.status_code = status
            return report
        except UpdateError as error:
            raise ApiError(error.status, error.code, str(error)) from error
        except (OSError, ValueError) as error:
            raise _upstream_error(error) from error
