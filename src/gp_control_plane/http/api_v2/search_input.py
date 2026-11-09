"""Request models and wire errors for the two search engines."""

from pydantic import ValidationError

from ...application.search_spec import (
    PROTOCOLS, BlockcheckGpStartRequest, BlockcheckStockStartRequest, has_protocol,
)
from ...discovery_domains import classify_domain_input
from .protocol import detail, validation_details


def _classify_domains(value: object, errors: list[dict[str, str]]) -> list[str]:
    if not isinstance(value, list):
        return []
    domains: dict[str, None] = {}
    malformed = {problem["field"] for problem in errors}
    for index, item in enumerate(value):
        if not isinstance(item, str):
            continue  # Structural validation already reported this item.
        field = f"body:/domains/{index}"
        if field in malformed:
            continue
        parsed = classify_domain_input(item)
        if not parsed["valid"]:
            errors.append(detail(field, "invalid_value", "Недопустимый домен"))
        elif parsed["domain"] in domains:
            errors.append(detail(field, "duplicate_domain", "Домен уже указан"))
        else:
            domains[parsed["domain"]] = None
    return list(domains)


def validate_start(engine: str, payload: object) -> tuple[dict | None, list[dict]]:
    model: type[BlockcheckStockStartRequest] | type[BlockcheckGpStartRequest]
    if engine == "blockcheck2":
        model = BlockcheckStockStartRequest
    elif engine == "blockcheck2-gp":
        model = BlockcheckGpStartRequest
    else:
        raise ValueError("Unknown engine")
    errors: list[dict] = []
    parsed = None
    try:
        parsed = model.model_validate(payload)
    except ValidationError as error:
        errors = validation_details(error)
        for problem in errors:
            if problem["field"] == "body:/domains" and problem["code"] == "empty":
                problem["message"] = "Нужен хотя бы один домен"
        errors = [problem for problem in errors if not (
            problem["field"].startswith("body:/domains/") and problem["code"] == "empty"
        )]
        if isinstance(payload, dict):
            # Literal validation conflates a value outside the enum and a wrong type.
            parameters = payload.get("parameters")
            if isinstance(parameters, dict) and "scan_level" in parameters and not isinstance(parameters["scan_level"], str):
                for problem in errors:
                    if problem["field"] == "body:/parameters/scan_level":
                        problem.update(code="invalid_type", message="Недопустимый тип значения")
    if not isinstance(payload, dict):
        return None, errors
    domains = _classify_domains(payload.get("domains"), errors)
    parameters = payload.get("parameters", {})
    if isinstance(parameters, dict) and all(type(parameters.get(key, False)) is bool for key in PROTOCOLS):
        if not has_protocol(parameters):
            errors.append(detail("body:/parameters", "invalid_value", "Нужна хотя бы одна протокольная проверка"))
    if errors or parsed is None:
        return None, errors
    return {"domains": domains, "domain_list_id": parsed.domain_list_id, "parameters": parsed.parameters.model_dump()}, []
