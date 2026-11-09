"""Domain-list HTTP methods and strict full-list input validation."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import ValidationError

from ...application.operation_admission import OperationAdmission, OperationBusy
from ...discovery_domains import classify_domain_input
from ...repositories import domain_lists
from ...resource_budget import JSON_REQUEST_MAX_BYTES
from ...storage import open_api2
from .access import authorized
from .models import DomainListWrite
from .openapi import describe
from .protocol import ApiError, detail, invalid_parameters, json_body, validation_details
from .query import Query, path_id


_ROOT = "/api/v2/data/domain-lists"


def _body(value: object) -> tuple[str | None, list[str], list[dict]]:
    errors = []
    try:
        DomainListWrite.model_validate(value)
    except ValidationError as error:
        errors.extend(
            detail(item["field"], "invalid_value", "Нужно непустое название")
            if item["field"] == "body:/name" and item["code"] == "empty" else item
            for item in validation_details(error) if item["code"] != "empty" or item["field"] == "body:/name"
        )
    if not isinstance(value, dict):
        return None, [], errors
    name = value.get("name")
    if isinstance(name, str) and name and (not name.strip() or any(0xD800 <= ord(char) <= 0xDFFF for char in name)):
        errors.append(detail("body:/name", "invalid_value", "Нужно непустое название"))
    domains = {}
    malformed = {problem["field"] for problem in errors}
    if isinstance(value.get("domains"), list):
        for index, entry in enumerate(value["domains"]):
            if not isinstance(entry, dict) or not isinstance(entry.get("domain"), str):
                continue
            field = f"body:/domains/{index}/domain"
            if field in malformed:
                continue
            parsed = classify_domain_input(entry["domain"])
            domain = parsed["domain"]
            if not parsed["valid"]:
                errors.append(detail(field, "invalid_value", "Недопустимый домен"))
            elif domain in domains:
                errors.append(detail(field, "duplicate_domain", "Домен уже указан"))
            else:
                domains[domain] = None
    return name, list(domains), errors


def _resource(conn, key, *, writable=False):
    row = domain_lists.read(conn, key)
    if row is None:
        raise ApiError(404, "not_found", "Список доменов не найден")
    if writable and row["kind"] != "user":
        raise ApiError(403, "read_only_resource", "Встроенный список нельзя менять")
    return row


def register_domain_lists(
    app: FastAPI, state_dir: Path, *, admission: OperationAdmission | None = None,
    json_limit: int = JSON_REQUEST_MAX_BYTES,
) -> None:
    if admission is None:
        admission = OperationAdmission()
    auth = authorized(state_dir)

    @app.get(_ROOT, **describe("listDomainLists"))
    @auth
    def list_all(request: Request):
        query = Query(request, {"kind", "offset", "limit"})
        kind = query.text("kind", values={"user", "builtin"})
        offset, limit = query.page()
        query.check()
        with open_api2(state_dir) as conn:
            conn.execute("BEGIN")
            return domain_lists.page(conn, kind, offset, limit)

    @app.get(_ROOT + "/{domain_list_id}", **describe("getDomainList"))
    @auth
    def get(request: Request, domain_list_id: str):
        query = Query(request, set())
        key = path_id("domain_list_id", domain_list_id, query.errors)
        query.check()
        with open_api2(state_dir) as conn:
            return domain_lists.view(_resource(conn, key))

    @app.get(_ROOT + "/{domain_list_id}/domains", **describe("getDomainListEntries"))
    @auth
    def entries(request: Request, domain_list_id: str):
        query = Query(request, {"offset", "limit"})
        offset, limit = query.page()
        key = path_id("domain_list_id", domain_list_id, query.errors)
        query.check()
        with open_api2(state_dir) as conn:
            conn.execute("BEGIN")
            _resource(conn, key)
            return domain_lists.entries(conn, key, offset, limit)

    @app.post(_ROOT, **describe("createDomainList"))
    @app.put(_ROOT + "/{domain_list_id}", **describe("replaceDomainList"))
    @auth
    def write(request: Request):
        domain_list_id = request.path_params.get("domain_list_id")
        query = Query(request, set(), accepts_body=True)
        key = path_id("domain_list_id", domain_list_id, query.errors) if domain_list_id is not None else None
        name, domains, errors = _body(json_body(request, json_limit))
        invalid_parameters([*query.errors, *errors])
        try:
            with admission.user_write(), open_api2(state_dir) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if key is not None:
                    _resource(conn, key, writable=True)
                payload = domain_lists.write(conn, key, name, domains)
        except OperationBusy as error:
            raise ApiError(409, "state_conflict", "Пользовательские данные восстанавливаются") from error
        return payload

    @app.delete(_ROOT + "/{domain_list_id}", **describe("deleteDomainList"))
    @auth
    def delete(request: Request, domain_list_id: str):
        query = Query(request, set())
        key = path_id("domain_list_id", domain_list_id, query.errors)
        query.check()
        try:
            with admission.user_write(), open_api2(state_dir) as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = domain_lists.read(conn, key)
                if row is not None and row["kind"] != "user":
                    raise ApiError(403, "read_only_resource", "Встроенный список нельзя менять")
                conn.execute("DELETE FROM domain_presets WHERE id=?", (key,))
        except OperationBusy as error:
            raise ApiError(409, "state_conflict", "Пользовательские данные восстанавливаются") from error
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
