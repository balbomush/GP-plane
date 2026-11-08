"""Start parameters are validated strictly against the two pinned schemas."""

import json
from pathlib import Path
import unittest

from gp_control_plane.http import api_v2


SPEC = json.loads((Path(__file__).parent / "fixtures" / "api_v2" / "openapi-draft33.json").read_text(encoding="utf-8"))


class SearchParameterTests(unittest.TestCase):
    def test_defaults_match_both_schemas(self):
        validate = getattr(api_v2, "validate_start", None)
        self.assertIsNotNone(validate, "search parameter validation is missing")
        for engine, schema in (
            ("blockcheck2", "BlockcheckStockParameters"),
            ("blockcheck2-gp", "BlockcheckGpParameters"),
        ):
            defaults = {name: item["default"] for name, item in SPEC["components"]["schemas"][schema]["properties"].items()}
            value, errors = validate(engine, {"domains": ["TEST.example."]})
            self.assertEqual(errors, [])
            self.assertEqual(value, {"domains": ["test.example"], "domain_list_id": None, "parameters": defaults})

    def test_reports_independent_errors_without_coercion(self):
        validate = getattr(api_v2, "validate_start", None)
        self.assertIsNotNone(validate, "search parameter validation is missing")
        value, errors = validate("blockcheck2", {
            "domains": ["EXAMPLE.com", "example.com.", 2], "domain_list_id": True, "mode": "standard",
            "parameters": {"curl_parallelism": 3, "repeats": "2", "enable_http": None, "curl_max_time": 0},
        })
        self.assertIsNone(value)
        self.assertEqual({(item["field"], item["code"]) for item in errors}, {
            ("body:/domains/1", "duplicate_domain"), ("body:/domains/2", "invalid_type"),
            ("body:/domain_list_id", "invalid_type"), ("body:/mode", "unknown_parameter"),
            ("body:/parameters/curl_parallelism", "unknown_parameter"),
            ("body:/parameters/repeats", "invalid_type"), ("body:/parameters/enable_http", "invalid_type"),
            ("body:/parameters/curl_max_time", "invalid_value"),
        })
