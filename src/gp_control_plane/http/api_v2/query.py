"""Parse query fields without silently discarding invalid or duplicate values."""

import re

from fastapi import Request

from .protocol import detail, invalid_parameters, no_body_error, query_fields


class Query:
    def __init__(self, request: Request, allowed: set[str], *, accepts_body: bool = False):
        self.data, self.errors = query_fields(request, allowed)
        if not accepts_body and (problem := no_body_error(request)) is not None:
            self.errors.append(problem)

    def text(self, key: str, *, default=None, values=None):
        items = self.data.get(key)
        if items is None:
            return default
        if len(items) != 1:
            self.errors.append(detail(f"query:{key}", "duplicate_parameter", "Нужно одно значение"))
        elif values is not None and items[0] not in values:
            self.errors.append(detail(f"query:{key}", "invalid_value", "Недопустимое значение"))
        else:
            return items[0]
        return default

    def integer(self, key: str, *, default=None, minimum=0, maximum=2**63 - 1):
        value = self.text(key)
        if value is None:
            return default
        if re.fullmatch(r"[0-9]{1,19}", value) and minimum <= int(value) <= maximum:
            return int(value)
        self.errors.append(detail(f"query:{key}", "invalid_value", "Число вне допустимого диапазона"))
        return default

    def page(self) -> tuple[int, int]:
        return self.integer("offset", default=0), self.integer("limit", default=50, minimum=1, maximum=200)

    def check(self) -> None:
        invalid_parameters(self.errors)


def path_id(name: str, value: str, errors: list[dict]) -> int | None:
    if re.fullmatch(r"[0-9]{1,19}", value) and 1 <= int(value) < 2**63:
        return int(value)
    errors.append(detail(f"path:{name}", "invalid_value", "Нужен положительный целочисленный ID"))
    return None
