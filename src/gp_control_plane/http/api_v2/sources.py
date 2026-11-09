"""Read and update the existing v2fly local source using a verified revision."""

import json
from pathlib import Path
import re
import tarfile
from threading import Lock

from fastapi import FastAPI, Request
from fastapi.responses import Response

from ... import core_supply, domain_sources
from ...application.operation_admission import OperationAdmission
from .access import authorized
from .openapi import describe
from .protocol import ApiError, no_body
from .query import Query


_ROOT = "/api/v2/data/sources/{source}"
_SHA = re.compile(r"[0-9a-f]{40}")


def _revision(value):
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _remote_revision():
    raw = domain_sources.fetch_v2fly_revision().strip()
    if _revision(raw):
        return raw
    payload = json.loads(raw)
    value = payload.get("sha") if isinstance(payload, dict) else None
    revision = _revision(value)
    if revision is None:
        raise ValueError("No usable source revision")
    return revision


def _upstream_error(error):
    if core_supply.is_rate_limit(error):
        return ApiError(503, "upstream_rate_limited", "Внешний источник ограничил запросы")
    return ApiError(502, "upstream_error", "Не удалось получить данные внешнего источника")


def _page(items, offset, limit):
    return {
        "items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit,
    }


def register_sources(app: FastAPI, state_dir: Path, *, admission: OperationAdmission | None = None) -> None:
    if admission is None:
        admission = OperationAdmission()
    authorize = authorized(state_dir)
    source_lock = Lock()

    def local(source):
        if source != "v2fly":
            raise ApiError(404, "not_found", "Источник не найден")
        manifest, error = domain_sources.read_v2fly_group_manifest(state_dir)
        if manifest and set(manifest["categories"]) != set(domain_sources._categories_from_group_dir(state_dir)):
            return None, {"kind": "local_storage"}
        return manifest, error

    def status(source):
        manifest, error = local(source)
        return {
            "source": source,
            "state": "error" if error else "ready" if manifest else "missing",
            "installed_revision": _revision(manifest["revision"]) if manifest else None,
            "error": {"code": "local_storage", "message": "Локальная копия недоступна"} if error else None,
        }

    def require_ready(source):
        manifest, error = local(source)
        if error or not manifest:
            raise ApiError(404, "source_not_ready", "Локальная копия источника не готова")
        return manifest

    @app.get(_ROOT, **describe("getSource"))
    @authorize
    def get_source(request: Request, source: str):
        Query(request, set()).check()
        with source_lock:
            return status(source)

    @app.get(_ROOT + "/categories", **describe("listCategories"))
    @authorize
    def categories(request: Request, source: str):
        query = Query(request, {"offset", "limit"})
        offset, limit = query.page()
        query.check()
        with source_lock:
            manifest = require_ready(source)
            return _page([{"id": name, "name": name} for name in manifest["categories"]], offset, limit)

    @app.get(_ROOT + "/categories/{category}/domains", **describe("listCategoryDomains"))
    @authorize
    def domains(request: Request, source: str, category: str):
        query = Query(request, {"offset", "limit"})
        offset, limit = query.page()
        query.check()
        with source_lock:
            manifest = require_ready(source)
            if category not in manifest["categories"]:
                raise ApiError(404, "not_found", "Категория источника не найдена")
            try:
                raw = domain_sources.fetch_v2fly_category_local(state_dir, category)
            except (OSError, ValueError) as error:
                raise ApiError(404, "source_not_ready", "Локальная копия источника не готова") from error
        return _page(domain_sources.parse_v2fly_domains(raw), offset, limit)

    @app.get(_ROOT + "/update", **describe("checkSourceUpdate"))
    @authorize
    def check_update(request: Request, source: str):
        Query(request, set()).check()
        with source_lock:
            installed = status(source)
        try:
            revision = _remote_revision()
            if installed["state"] != "missing" and installed["installed_revision"] is None:
                raise ValueError("Unknown installed source revision")
        except (OSError, ValueError) as error:
            raise _upstream_error(error) from error
        return {
            "source": source,
            "installed_revision": installed["installed_revision"],
            "available_revision": revision,
            "update_available": installed["installed_revision"] != revision,
        }

    @app.post(_ROOT + "/update", **describe("updateSource"))
    @authorize
    def update(request: Request, source: str):
        no_body(request)
        if source != "v2fly":
            raise ApiError(404, "not_found", "Источник не найден")
        with admission.claim("source_update"):
            try:
                revision = _remote_revision()
                payload = domain_sources.fetch_v2fly_archive(revision)
                with source_lock:
                    domain_sources.prepare_v2fly_local_storage(
                        state_dir, archive_fetcher=lambda: payload, revision_fetcher=lambda: revision,
                    )
                    return status(source)
            except (OSError, ValueError, tarfile.TarError) as error:
                raise _upstream_error(error) from error
