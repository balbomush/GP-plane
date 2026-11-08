"""Domain-list HTTP methods and strict full-list input validation."""

from pathlib import Path

from bottle import Bottle, HTTPResponse

from ...application.operation_admission import OperationAdmission, OperationBusy
from ...discovery_domains import classify_domain_input
from ...repositories import domain_lists
from ...resource_budget import JSON_REQUEST_MAX_BYTES
from ...storage import connect_api2
from .access import authorized
from .protocol import ApiError, detail, invalid_parameters, json_body, json_response, object_fields
from .query import Query, path_id


_ROOT = "/api/v2/data/domain-lists"


def _body(value: object) -> tuple[str | None, list[str], list[dict]]:
    body, errors = object_fields(value, {"name", "domains"})
    name = body.get("name")
    domains = []
    seen = set()
    if not isinstance(value, dict):
        return name, domains, errors
    for key, expected in (("name", str), ("domains", list)):
        if key not in body:
            errors.append(detail(f"body:/{key}", "required", "Обязательный параметр"))
        elif not isinstance(body[key], expected):
            errors.append(detail(f"body:/{key}", "invalid_type", "Недопустимый тип значения"))
    if isinstance(name, str) and (not name.strip() or any(0xD800 <= ord(char) <= 0xDFFF for char in name)):
        errors.append(detail("body:/name", "invalid_value", "Нужно непустое название"))
    if isinstance(body.get("domains"), list):
        for index, entry in enumerate(body["domains"]):
            prefix = f"body:/domains/{index}"
            item, problems = object_fields(entry, {"domain"}, prefix)
            errors.extend(problems)
            if not isinstance(entry, dict):
                continue
            if "domain" not in item:
                errors.append(detail(prefix + "/domain", "required", "Обязательный параметр"))
            elif not isinstance(item["domain"], str):
                errors.append(detail(prefix + "/domain", "invalid_type", "Нужна строка"))
            else:
                parsed = classify_domain_input(item["domain"])
                domain = parsed["domain"]
                if not parsed["valid"]:
                    errors.append(detail(prefix + "/domain", "invalid_value", "Недопустимый домен"))
                elif domain in seen:
                    errors.append(detail(prefix + "/domain", "duplicate_domain", "Домен уже указан"))
                else:
                    seen.add(domain)
                    domains.append(domain)
    return name, domains, errors


def _resource(conn, key, *, writable=False):
    row = domain_lists.read(conn, key)
    if row is None:
        raise ApiError(404, "not_found", "Список доменов не найден")
    if writable and row["kind"] != "user":
        raise ApiError(403, "read_only_resource", "Встроенный список нельзя менять")
    return row


def register_domain_lists(
    app: Bottle, state_dir: Path, *, admission: OperationAdmission | None = None,
    json_limit: int = JSON_REQUEST_MAX_BYTES,
) -> None:
    if admission is None:
        admission = OperationAdmission()
    auth = authorized(state_dir)

    @app.get(_ROOT)
    @auth
    def list_all():
        query = Query({"kind", "offset", "limit"})
        kind = query.text("kind", values={"user", "builtin"})
        offset, limit = query.page()
        query.check()
        with connect_api2(state_dir) as conn:
            conn.execute("BEGIN")
            return json_response(domain_lists.page(conn, kind, offset, limit))

    @app.get(_ROOT + "/<domain_list_id>")
    @auth
    def get(domain_list_id):
        query = Query(set())
        key = path_id("domain_list_id", domain_list_id, query.errors)
        query.check()
        with connect_api2(state_dir) as conn:
            return json_response(domain_lists.view(_resource(conn, key)))

    @app.get(_ROOT + "/<domain_list_id>/domains")
    @auth
    def entries(domain_list_id):
        query = Query({"offset", "limit"})
        offset, limit = query.page()
        key = path_id("domain_list_id", domain_list_id, query.errors)
        query.check()
        with connect_api2(state_dir) as conn:
            conn.execute("BEGIN")
            _resource(conn, key)
            return json_response(domain_lists.entries(conn, key, offset, limit))

    @app.post(_ROOT)
    @app.put(_ROOT + "/<domain_list_id>")
    @auth
    def write(domain_list_id=None):
        query = Query(set(), accepts_body=True)
        key = path_id("domain_list_id", domain_list_id, query.errors) if domain_list_id is not None else None
        name, domains, errors = _body(json_body(json_limit))
        invalid_parameters([*query.errors, *errors])
        try:
            with admission.user_write(), connect_api2(state_dir) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if key is not None:
                    _resource(conn, key, writable=True)
                payload = domain_lists.write(conn, key, name, domains)
        except OperationBusy as error:
            raise ApiError(409, "state_conflict", "Пользовательские данные восстанавливаются") from error
        return json_response(payload, 201 if key is None else 200)

    @app.delete(_ROOT + "/<domain_list_id>")
    @auth
    def delete(domain_list_id):
        query = Query(set())
        key = path_id("domain_list_id", domain_list_id, query.errors)
        query.check()
        try:
            with admission.user_write(), connect_api2(state_dir) as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = domain_lists.read(conn, key)
                if row is not None and row["kind"] != "user":
                    raise ApiError(403, "read_only_resource", "Встроенный список нельзя менять")
                conn.execute("DELETE FROM domain_presets WHERE id=?", (key,))
        except OperationBusy as error:
            raise ApiError(409, "state_conflict", "Пользовательские данные восстанавливаются") from error
        return HTTPResponse(status=204, headers={"Cache-Control": "no-store"})
