from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.backups import _validate_vault_id
from tests.support.legacy_http import docs as web_docs
from tests.support.legacy_http import routes as web_routes


OPENAPI_PATH = Path(__file__).resolve().parents[1] / "openapi.json"
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
BEARER_SECURITY = [{"bearerAuth": []}]


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_openapi_source() -> dict[str, Any]:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"), object_pairs_hook=_without_duplicate_keys)


def _documented_operations(contract: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (path, method.upper())
        for path, path_item in contract["paths"].items()
        for method in path_item
        if method.lower() in HTTP_METHODS
    }


class OpenApiAuthenticationContractTests(unittest.TestCase):
    def test_clean_install_vault_id_schemas_and_examples_match_runtime_contract(self) -> None:
        contract = _load_openapi_source()
        vault_id_pattern = "^[a-f0-9]{32}$"
        vault_id = re.compile(vault_id_pattern)
        schemas = contract["components"]["schemas"]

        for schema_name in (
            "CleanInstallVaultCreateResponse",
            "CleanInstallVaultStatus",
            "CleanInstallVaultRestoreRequest",
            "CleanInstallVaultRestoreResponse",
        ):
            with self.subTest(schema_name=schema_name):
                definition = schemas[schema_name]["properties"]["vault_id"]
                self.assertEqual(definition["type"], "string")
                self.assertEqual(definition["pattern"], vault_id_pattern)

        examples = contract["components"]["examples"]
        matched_examples = 0
        for example_name, example in examples.items():
            if not example_name.startswith("CleanInstallVault"):
                continue
            value = example.get("value") or {}
            if "vault_id" not in value:
                continue
            matched_examples += 1
            with self.subTest(example_name=example_name):
                self.assertIsInstance(value["vault_id"], str)
                self.assertIsNotNone(vault_id.fullmatch(value["vault_id"]))
                self.assertEqual(_validate_vault_id(value["vault_id"]), value["vault_id"])
        self.assertGreater(matched_examples, 0)

        list_example_vault_id = examples["CleanInstallVaultListResponse"]["value"]["vaults"][0]["vault_id"]
        self.assertIsNotNone(vault_id.fullmatch(list_example_vault_id))
        self.assertEqual(_validate_vault_id(list_example_vault_id), list_example_vault_id)

        status_parameters = contract["paths"]["/api/core/clean-install-vaults/status"]["get"]["parameters"]
        status_vault_id = next(parameter for parameter in status_parameters if parameter["name"] == "vault_id")
        self.assertEqual(status_vault_id["schema"]["pattern"], vault_id_pattern)
        self.assertIsNotNone(vault_id.fullmatch(status_vault_id["example"]))
        self.assertEqual(_validate_vault_id(status_vault_id["example"]), status_vault_id["example"])

    def test_source_openapi_matches_registered_server_routes_and_auth_policy(self) -> None:
        contract = _load_openapi_source()
        expected_operations = web_routes.openapi_operations()

        self.assertEqual(expected_operations, _documented_operations(contract))
        self.assertEqual(BEARER_SECURITY, contract["security"])
        self.assertEqual(
            {"type": "http", "scheme": "bearer", "bearerFormat": "opaque"},
            contract["components"]["securitySchemes"]["bearerAuth"],
        )

        for path, method in expected_operations:
            route = web_routes.route_for(method, path)
            self.assertIsNotNone(route, f"missing server route for {method} {path}")
            operation = contract["paths"][path][method.lower()]
            expected_security = [] if not route.auth_required else BEARER_SECURITY
            actual_security = operation.get("security", contract["security"])
            self.assertEqual(expected_security, actual_security, f"{method} {path}")

    def test_auth_operations_describe_server_payloads_errors_and_restore_completion(self) -> None:
        contract = _load_openapi_source()
        schemas = contract["components"]["schemas"]
        responses = contract["components"]["responses"]
        paths = contract["paths"]

        serialized = json.dumps(contract, sort_keys=True)
        self.assertNotIn("confirmation_token", serialized)
        self.assertNotIn("handoff_secret", serialized)
        self.assertEqual(["vault_id", "confirm_restore"], schemas["CleanInstallVaultRestoreRequest"]["required"])
        self.assertEqual({"vault_id", "confirm_restore"}, set(schemas["CleanInstallVaultRestoreRequest"]["properties"]))
        self.assertNotIn("confirmation_token", schemas["CleanInstallVaultCreateResponse"]["properties"])
        self.assertNotIn("handoff_secret", schemas["CleanInstallVaultCreateResponse"]["properties"])

        self.assertEqual(["username", "password"], schemas["LoginRequest"]["required"])
        self.assertEqual(["current_password", "new_password"], schemas["ChangePasswordRequest"]["required"])
        self.assertEqual(
            [{"const": "admin"}, {"minLength": 8}],
            schemas["ChangePasswordRequest"]["properties"]["new_password"]["anyOf"],
        )
        self.assertEqual(["access_token", "token_type", "expires_in"], schemas["BearerToken"]["required"])
        self.assertEqual("Bearer", schemas["BearerToken"]["properties"]["token_type"]["const"])
        self.assertEqual(86400, schemas["BearerToken"]["properties"]["expires_in"]["const"])

        login = paths["/api/auth/login"]["post"]
        change_password = paths["/api/auth/change-password"]["post"]
        self.assertEqual([], login["security"])
        self.assertEqual(BEARER_SECURITY, change_password.get("security", contract["security"]))
        for operation in (login, change_password):
            self.assertEqual(
                {"$ref": "#/components/schemas/BearerToken"},
                operation["responses"]["200"]["content"]["application/json"]["schema"],
            )
            self.assertEqual({"$ref": "#/components/responses/Error"}, operation["responses"]["400"])
            self.assertEqual({"$ref": "#/components/responses/AuthenticationRequired"}, operation["responses"]["401"])
            self.assertEqual({"$ref": "#/components/responses/Error"}, operation["responses"]["503"])
        self.assertEqual("Bearer", responses["AuthenticationRequired"]["headers"]["WWW-Authenticate"]["schema"]["const"])

        restore = paths["/api/core/backups/restore"]["post"]
        self.assertIn("synchronously", restore["description"])
        self.assertIn("completed", restore["responses"]["202"]["description"])
        self.assertEqual("success", schemas["BackupRestoreAccepted"]["properties"]["status"]["const"])
        self.assertEqual("success", contract["components"]["examples"]["BackupRestoreAccepted"]["value"]["status"])

        last_snapshot = schemas["CoreStatus"]["properties"]["last_snapshot"]
        self.assertEqual("#/components/schemas/PostRunSnapshotOutcome", last_snapshot["$ref"])
        success, failure = schemas["PostRunSnapshotOutcome"]["oneOf"]
        self.assertEqual("success", success["properties"]["status"]["const"])
        self.assertEqual("failed", failure["properties"]["status"]["const"])
        self.assertEqual(
            ["kind", "status", "completed_at", "error_code", "error_message"],
            failure["required"],
        )
        self.assertEqual(512, failure["properties"]["error_message"]["maxLength"])

    def test_served_openapi_keeps_bearer_security_usable_by_swagger(self) -> None:
        contract = json.loads(web_docs.openapi_json_bytes().decode("utf-8"), object_pairs_hook=_without_duplicate_keys)

        self.assertEqual(BEARER_SECURITY, contract["security"])
        self.assertEqual("http", contract["components"]["securitySchemes"]["bearerAuth"]["type"])
        self.assertEqual("bearer", contract["components"]["securitySchemes"]["bearerAuth"]["scheme"])
        for path, method in web_routes.openapi_operations():
            route = web_routes.route_for(method, path)
            operation = contract["paths"][path][method.lower()]
            self.assertEqual(
                [] if not route.auth_required else BEARER_SECURITY,
                operation.get("security", contract["security"]),
                f"{method} {path}",
            )

    def test_served_openapi_preserves_source_auth_contract(self) -> None:
        source_bytes = OPENAPI_PATH.read_bytes()
        served_bytes = web_docs.openapi_json_bytes()
        source = json.loads(source_bytes.decode("utf-8"), object_pairs_hook=_without_duplicate_keys)
        served = json.loads(served_bytes.decode("utf-8"), object_pairs_hook=_without_duplicate_keys)

        self.assertEqual(source_bytes, served_bytes)
        self.assertEqual(source["security"], served["security"])
        self.assertEqual(source["components"]["securitySchemes"], served["components"]["securitySchemes"])

        for path in ("/api/auth/login", "/api/auth/change-password"):
            self.assertEqual(source["paths"][path]["post"], served["paths"][path]["post"], path)

        for schema in ("LoginRequest", "ChangePasswordRequest", "BearerToken"):
            self.assertEqual(source["components"]["schemas"][schema], served["components"]["schemas"][schema], schema)

        for response in ("Error", "AuthenticationRequired"):
            self.assertEqual(source["components"]["responses"][response], served["components"]["responses"][response], response)

    def test_headless_openapi_is_a_route_filtered_projection_of_the_source_contract(self) -> None:
        source = _load_openapi_source()
        headless = json.loads(
            web_docs.openapi_json_bytes(core_only=True).decode("utf-8"),
            object_pairs_hook=_without_duplicate_keys,
        )
        expected_operations = web_routes.openapi_operations(core_only=True)

        self.assertIn(("/api/core/clean-install-vaults/restore", "POST"), expected_operations)
        self.assertNotIn(("/api/web/events", "GET"), expected_operations)

        self.assertEqual(expected_operations, _documented_operations(headless))
        self.assertEqual(source["security"], headless["security"])
        self.assertEqual(source["components"]["securitySchemes"], headless["components"]["securitySchemes"])
        self.assertEqual(
            {key: value for key, value in source.items() if key not in {"info", "paths", "tags"}},
            {key: value for key, value in headless.items() if key not in {"info", "paths", "tags"}},
        )
        self.assertEqual(
            {**source["info"], **web_docs.CORE_ONLY_OPENAPI_INFO},
            headless["info"],
        )

        expected_paths = {
            path: {
                name: value
                for name, value in path_item.items()
                if name.lower() not in HTTP_METHODS or (path, name.upper()) in expected_operations
            }
            for path, path_item in source["paths"].items()
            if any((path, method.upper()) in expected_operations for method in path_item)
        }
        self.assertEqual(expected_paths, headless["paths"])

        used_tags = {
            tag
            for path_item in expected_paths.values()
            for method, operation in path_item.items()
            if method.lower() in HTTP_METHODS
            for tag in operation.get("tags", [])
        }
        self.assertEqual(
            [tag for tag in source.get("tags", []) if tag.get("name") in used_tags],
            headless.get("tags", []),
        )

        for path, method in expected_operations:
            operation = headless["paths"][path][method.lower()]
            self.assertEqual(source["paths"][path][method.lower()], operation, f"{method} {path}")
            route = web_routes.route_for(method, path)
            self.assertEqual(
                [] if not route.auth_required else BEARER_SECURITY,
                operation.get("security", headless["security"]),
                f"{method} {path}",
            )

        self.assertEqual(
            source["paths"]["/api/core/clean-install-vaults/restore"]["post"]["requestBody"],
            headless["paths"]["/api/core/clean-install-vaults/restore"]["post"]["requestBody"],
        )
