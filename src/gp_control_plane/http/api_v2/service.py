"""Core status and acquisition of a verified, versioned supply."""

from pathlib import Path

from bottle import Bottle

from ... import __version__, core_supply, storage, user_backup
from ...application.operation_admission import OperationAdmission
from ...resource_budget import JSON_REQUEST_MAX_BYTES
from .access import authorized
from .protocol import ApiError, json_body, json_response, no_body, required_text_fields
from .query import Query


_ROOT = "/api/v2/service"


def _upstream_error(error):
    if core_supply.is_rate_limit(error):
        return ApiError(503, "upstream_rate_limited", "Внешний каталог ограничил запросы")
    return ApiError(502, "upstream_error", "Не удалось получить проверенную поставку Core")


def register_service(
    app: Bottle, state_dir: Path, *, admission: OperationAdmission,
    json_limit: int = JSON_REQUEST_MAX_BYTES,
) -> None:
    authorize = authorized(state_dir)

    @app.get(_ROOT + "/status")
    @authorize
    def status():
        no_body()
        try:
            with storage.connect_api2(state_dir) as conn:
                conn.execute("SELECT 1 FROM milestones LIMIT 1")
            readiness = "ready"
        except (OSError, ValueError, storage.StorageUnavailableError):
            readiness = "error"
        operation = admission.active
        return json_response({
            "core_version": __version__, "api_version": "2", "storage": readiness,
            "busy": operation is not None, "operation": operation,
            "backup_exists": user_backup.saved_path(state_dir).is_file(),
        })

    @app.get(_ROOT + "/releases")
    @authorize
    def list_releases():
        query = Query({"channel"})
        channel = query.text("channel", values={"stable", "prerelease"}, default="stable")
        query.check()
        try:
            items = [info for info, _ in core_supply.catalog() if info["prerelease"] == (channel == "prerelease")]
        except (OSError, ValueError) as error:
            raise _upstream_error(error) from error
        return json_response({"channel": channel, "items": items})

    @app.post(_ROOT + "/download")
    @authorize
    def download():
        version = required_text_fields(json_body(json_limit), ("version",))["version"]
        with admission.claim("program_download"):
            try:
                size = core_supply.prepare(state_dir, version)
            except (OSError, ValueError) as error:
                raise _upstream_error(error) from error
            if size is None:
                raise ApiError(404, "not_found", "Точная поставка Core не найдена")
        return json_response({"version": version, "size_bytes": size})
