"""Strict start validation for the two existing engines."""

from ...discovery_domains import classify_domain_input
from .protocol import detail, object_fields


_STOCK = {
    "include_quic": True,
    "enable_http": False,
    "enable_tls12": True,
    "enable_tls13": False,
    "enable_ipv6": False,
    "scan_level": "standard",
    "repeats": 1,
    "repeat_parallel": False,
    "skip_dnscheck": True,
    "skip_ipblock": True,
    "curl_max_time": 2,
    "curl_max_time_quic": 2,
    "curl_max_time_doh": 2,
    "debug_stdout": False,
}
_PROTOCOLS = ("include_quic", "enable_http", "enable_tls12", "enable_tls13")


def _domains(value, errors):
    if not isinstance(value, list):
        errors.append(detail("body:/domains", "invalid_type", "Нужен массив доменов"))
        return []
    if not value:
        errors.append(detail("body:/domains", "empty", "Нужен хотя бы один домен"))
    result = {}
    for index, item in enumerate(value):
        field = f"body:/domains/{index}"
        if not isinstance(item, str):
            errors.append(detail(field, "invalid_type", "Нужна строка"))
            continue
        parsed = classify_domain_input(item)
        if not parsed["valid"]:
            errors.append(detail(field, "invalid_value", "Недопустимый домен"))
        elif parsed["domain"] in result:
            errors.append(detail(field, "duplicate_domain", "Домен уже указан"))
        else:
            result[parsed["domain"]] = None
    return list(result)


def _parameters(engine, value, errors):
    defaults = {**_STOCK, "curl_parallelism": 4} if engine == "blockcheck2-gp" else _STOCK
    supplied, unknown = object_fields(value, set(defaults), "body:/parameters")
    errors.extend(unknown)
    effective = dict(defaults)
    invalid = set()
    for key, default in defaults.items():
        if key not in supplied:
            continue
        candidate = supplied[key]
        field = f"body:/parameters/{key}"
        if type(candidate) is not type(default):
            errors.append(detail(field, "invalid_type", "Недопустимый тип значения"))
        elif isinstance(default, str) and candidate not in {"quick", "standard", "force"}:
            errors.append(detail(field, "invalid_value", "Недопустимый режим перебора"))
        elif type(default) is int and (candidate < 1 or key == "curl_parallelism" and candidate > 10):
            errors.append(detail(field, "invalid_value", "Число вне допустимого диапазона"))
        else:
            effective[key] = candidate
            continue
        invalid.add(key)
    if not invalid.intersection(_PROTOCOLS) and not any(effective[key] for key in _PROTOCOLS):
        errors.append(detail("body:/parameters", "invalid_value", "Нужна хотя бы одна протокольная проверка"))
    return effective


def validate_start(engine: str, payload: object) -> tuple[dict | None, list[dict]]:
    if engine not in ("blockcheck2", "blockcheck2-gp"):
        raise ValueError("Unknown engine")
    body, errors = object_fields(payload, {"domains", "parameters", "domain_list_id"})
    if not isinstance(payload, dict):
        return None, errors
    domains = []
    if "domains" in body:
        domains = _domains(body["domains"], errors)
    else:
        errors.append(detail("body:/domains", "required", "Обязательный параметр"))
    list_id = body.get("domain_list_id")
    if "domain_list_id" in body:
        if type(list_id) is not int:
            errors.append(detail("body:/domain_list_id", "invalid_type", "Нужен целочисленный ID"))
        elif not 1 <= list_id < 2**63:
            errors.append(detail("body:/domain_list_id", "invalid_value", "ID вне допустимого диапазона"))
    parameters = _parameters(engine, body.get("parameters", {}), errors)
    if errors:
        return None, errors
    return {"domains": domains, "domain_list_id": list_id, "parameters": parameters}, []
