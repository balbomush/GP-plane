"""Documentation metadata for the live FastAPI routes."""

from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
import json
from threading import Lock

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from pydantic.json_schema import models_json_schema

from . import models, search_input


_PREFIX = "/api/v2"


class ApiJSONResponse(JSONResponse):
    def __init__(self, content, status_code=200, headers=None, media_type=None, background=None):
        super().__init__(
            content, status_code, {**(headers or {}), "Cache-Control": "no-store"}, media_type, background,
        )


@lru_cache
def _metadata():
    return json.loads(files(__package__).joinpath("api-docs.json").read_bytes())


def _model(name):
    return getattr(search_input if name.startswith("Blockcheck") else models, name)


def describe(operation_id, *, response_class=None):
    metadata = deepcopy(_metadata()["operations"][operation_id])
    responses = metadata.pop("responses")
    for status, response in responses.items():
        name = response.pop("model", None)
        if name is None:
            continue
        if status.startswith("2"):
            response["model"] = _model(name)
        else:
            response["content"]["application/json"]["schema"] = {"$ref": f"#/components/schemas/{name}"}
    code = min(int(status) for status in responses if status.startswith("2"))
    primary = responses[str(code)]
    json_model = primary.get("model")
    return {
        "operation_id": operation_id,
        "status_code": code,
        "summary": metadata.pop("summary"),
        "description": metadata.pop("description"),
        "tags": metadata.pop("tags"),
        "responses": responses,
        "response_model": json_model,
        "response_model_exclude_unset": True,
        "response_class": response_class or (ApiJSONResponse if json_model else Response),
        "openapi_extra": metadata,
    }


def _run_schema(schemas, model, fields):
    """Express a specialized run as a refinement of the public RunStatus."""
    derived = schemas[model]["properties"]
    return {"allOf": [
        {"$ref": "#/components/schemas/RunStatus"},
        {"type": "object", "properties": {field: derived[field] for field in fields},
         **({"required": ["finished_at"]} if "finished_at" in fields else {})},
    ]}


def _preserve_search_union_shapes(schemas):
    # Pydantic emits referenced branches. Keep the draft.33 inline refinements
    # for schema consumers while deriving their fields from the same models.
    current = schemas.get("CurrentSearch")
    if current:
        branches = [deepcopy(schemas[branch["$ref"].rsplit("/", 1)[-1]]) for branch in current["oneOf"]]
        branches[1]["properties"]["run"] = _run_schema(schemas, "ActiveSearchRun", ("status", "engine"))
        current["oneOf"] = branches
    stop = schemas.get("StopSearchResult")
    if stop:
        stop["properties"]["run"]["anyOf"][0] = _run_schema(
            schemas, "StopSearchResultRun1", ("status", "finished_at"),
        )


def _remove_generated_titles(schema):
    # Titles synthesized from Python names are internal and make structural
    # union comparisons report a change even when the branches are equal.
    if isinstance(schema, dict):
        schema.pop("title", None)
        for value in schema.values():
            _remove_generated_titles(value)
    elif isinstance(schema, list):
        for value in schema:
            _remove_generated_titles(value)


def configure_openapi(app: FastAPI) -> None:
    original = app.openapi
    lock = Lock()
    complete = None

    def generate():
        schema = original()
        # Route paths are generated from the actual registrations. Describe
        # their common prefix using the existing server URL convention.
        schema["paths"] = {path.removeprefix(_PREFIX): operations for path, operations in schema["paths"].items()}
        for operations in schema["paths"].values():
            for operation in operations.values():
                operation["responses"].pop("422", None)
                parameters = operation.get("parameters", [])
                # Starlette supplies raw path values. The documented format
                # is the constraint checked after auth by the handler.
                if parameters:
                    operation["parameters"] = list({
                        (parameter["in"], parameter["name"]): parameter for parameter in parameters
                    }.values())
        request_models = [
            models.LoginRequest, models.PasswordRequest, models.DomainListWrite, models.VersionRequest,
            models.UpdateRequest, search_input.BlockcheckStockStartRequest, search_input.BlockcheckGpStartRequest,
            models.ExportManifest, models.CurrentSearch, models.ErrorResponse, models.RunStatus,
        ]
        _, extra = models_json_schema([(model, "validation") for model in request_models],
                                      ref_template="#/components/schemas/{model}")
        components = schema.setdefault("components", {})
        for name, definition in extra["$defs"].items():
            components.setdefault("schemas", {}).setdefault(name, definition)
        _preserve_search_union_shapes(components["schemas"])
        _remove_generated_titles(components["schemas"])
        components["responses"] = {"Error": _metadata()["error"]}
        components["securitySchemes"] = {"bearerAuth": {
            "type": "http", "scheme": "bearer",
            "description": "Токен на 604800 секунд, без автоматического продления. Не в URL.",
        }}
        schema["security"] = [{"bearerAuth": []}]
        return schema

    def openapi():
        nonlocal complete
        with lock:
            if complete is None:
                try:
                    complete = generate()
                except BaseException:
                    app.openapi_schema = None
                    raise
            return complete

    app.openapi = openapi
