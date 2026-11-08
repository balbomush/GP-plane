"""API 2 catalog and validated ZIP exports."""

from collections.abc import Iterable
from pathlib import Path
from tempfile import TemporaryFile

from bottle import Bottle, HTTPResponse

from ...application.operation_admission import OperationAdmission, OperationBusy
from ...discovery_domains import classify_domain_input
from ...repositories import catalog
from ...storage import connect_api2
from ... import strategy_assets
from ..stream_admission import ClosingIterator
from .access import authorized
from .protocol import ApiError, detail, json_response
from .query import Query, path_id


_ROOT = "/api/v2/data/"


def _select_milestone(conn, requested):
    chosen = catalog.milestone(conn, requested)
    if chosen is None:
        raise ApiError(404, "not_found", "Веха не найдена")
    return chosen


def _resource(conn, kind, key):
    row = catalog.read(conn, kind, key)
    if row is None:
        raise ApiError(404, "not_found", "Запись каталога не найдена")
    return row


def _strategy_key(value, errors):
    return value


def _bundle_key(value, errors):
    return path_id("bundle_id", value, errors)


def _domains(query):
    result = {}
    for domain in query.data.get("domains", ()):
        parsed = classify_domain_input(domain)
        if not parsed["valid"]:
            query.errors.append(detail("query:domains", "invalid_value", "Недопустимый домен"))
        else:
            result[parsed["domain"]] = None
    return tuple(result)


def _files(assets_root, kind, row, *, verify):
    reference = row["assets_ref"] if kind == "strategy" else row["files_ref"]
    return strategy_assets.load(
        assets_root, reference, row["fingerprint"],
        expected_args=row["args"] if kind == "strategy" else None, verify=verify,
    )


def _stream_file(file) -> Iterable[bytes]:
    while block := file.read(64 * 1024):
        yield block


def register_catalog(app: Bottle, state_dir: Path, *, admission: OperationAdmission | None = None) -> None:
    if admission is None:
        admission = OperationAdmission()
    authorize = authorized(state_dir)
    assets_root = state_dir / "strategy-finder" / "assets"

    def available(kind, row):
        try:
            _files(assets_root, kind, row, verify=False)
        except (OSError, ValueError):
            return False
        return True

    def register_kind(kind, route, id_field, parse_key):
        is_strategy = kind == "strategy"

        @app.get(_ROOT + route)
        @authorize
        def list_all():
            common = {"domains", "match", "milestone_id", "offset", "limit"}
            if is_strategy:
                query = Query(common | {"protocol", "family", "q", "engine_code", "sort"})
                filters = {key: query.text(key) for key in ("protocol", "family", "q", "engine_code")}
                filters["sort"] = query.text("sort", default="id", values={"id", "family"})
            else:
                query = Query(common | {"view"})
                filters = {"view": query.text("view", default="confirmed", values={"confirmed", "all"})}
            filters["domains"] = _domains(query)
            filters["match"] = query.text("match", default="any", values={"any", "all"})
            requested = query.integer("milestone_id")
            offset, limit = query.page()
            query.check()
            with connect_api2(state_dir) as conn:
                conn.execute("BEGIN")
                chosen = _select_milestone(conn, requested)
                return json_response(catalog.page(conn, kind, chosen, filters, offset, limit, available=available))

        @app.get(_ROOT + route + "/<" + id_field + ">")
        @authorize
        def get(**path):
            query = Query({"milestone_id"})
            key = parse_key(path[id_field], query.errors)
            requested = query.integer("milestone_id")
            query.check()
            with connect_api2(state_dir) as conn:
                conn.execute("BEGIN")
                row = _resource(conn, kind, key)
                chosen = _select_milestone(conn, requested)
                return json_response(catalog.view(conn, kind, row, chosen, available=available))

        @app.get(_ROOT + route + "/<" + id_field + ">/archive")
        @authorize
        def archive(**path):
            query = Query(set())
            key = parse_key(path[id_field], query.errors)
            query.check()
            operation = admission.claim("archive_download")
            try:
                operation.__enter__()
            except OperationBusy as error:
                raise ApiError(409, "busy", "Core выполняет другую операцию", [
                    {"operation": error.operation},
                ]) from error

            file = None

            def release():
                try:
                    if file is not None:
                        file.close()
                finally:
                    operation.__exit__(None, None, None)

            try:
                with connect_api2(state_dir) as conn:
                    row = _resource(conn, kind, key)
                    try:
                        args, files = _files(assets_root, kind, row, verify=True)
                    except (OSError, ValueError) as error:
                        raise ApiError(409, "archive_incomplete", "Полный рабочий состав недоступен") from error
                file = TemporaryFile()
                strategy_assets.write_archive(file, kind, args, files)
                length = file.tell()
                file.seek(0)
                return HTTPResponse(ClosingIterator(_stream_file(file), release), headers={
                    "Content-Type": "application/zip", "Cache-Control": "no-store",
                    "Content-Length": str(length), "Content-Disposition": f'attachment; filename="{kind}.zip"',
                })
            except BaseException:
                release()
                raise

    register_kind("strategy", "strategies", "strategy_id", _strategy_key)
    register_kind("bundle", "bundles", "bundle_id", _bundle_key)
