"""Core-owned route/schema/cursor assertions retained from mixed UI suite."""
import ast,json,tempfile,unittest
from pathlib import Path
from unittest import mock
from gp_control_plane.config import AppConfig,OutputConfig
from gp_control_plane.http import api_server as web_app,docs as web_docs,routes as web_routes
from gp_control_plane.strategy_finder import upsert_candidates
class CoreHttpContractTests(unittest.TestCase):
    def test_openapi_contract_matches_routes_auth_errors_and_identifiers(self) -> None:
            for core_only in (False, True):
                contract = json.loads(web_docs.openapi_json_bytes(core_only=core_only))
                documented = {
                    (path, method.upper())
                    for path, operations in contract["paths"].items()
                    for method in operations
                }
                expected = {
                    (spec.path, method)
                    for spec in web_routes.ROUTES
                    if spec.openapi and (not core_only or spec.allowed_in_core)
                    for method in spec.methods
                    if method != "HEAD"
                }
                self.assertEqual(expected, documented)

                for path, method in documented:
                    route = web_routes.route_for(method, path)
                    self.assertIsNotNone(route)
                    operation = contract["paths"][path][method.lower()]
                    expected_security = [] if not route.auth_required else [{"bearerAuth": []}]
                    self.assertEqual(expected_security, operation.get("security", contract["security"]))
                    for status, response in operation["responses"].items():
                        if status == "default" or not status.startswith("2"):
                            self.assertIn("$ref", response)
                            self.assertIn(
                                response["$ref"],
                                {f"#/components/responses/{name}" for name in contract["components"]["responses"]},
                            )

                error_schema = contract["components"]["schemas"]["ErrorResponse"]["properties"]["error"]
                self.assertEqual(["code", "message", "details"], error_schema["required"])
                self.assertIn("saving", contract["components"]["schemas"]["RunStatus"]["enum"])
                self.assertEqual(
                    "#/components/schemas/RunAccepted",
                    contract["paths"]["/api/core/strategy-discovery/stop-current-run"]["post"]["responses"]["202"]["content"][
                        "application/json"
                    ]["schema"]["$ref"],
                )
                self.assertIn("run_id", contract["components"]["schemas"]["RunAccepted"]["properties"])
                self.assertNotIn("/api/service/releases/install", contract["paths"])
                self.assertNotIn("/api/service/releases/install-plan", contract["paths"])
                self.assertNotIn("job_id", json.dumps(contract))

            swagger_html = web_docs.swagger_ui_html()
            self.assertIn("persistAuthorization: true", swagger_html)
            self.assertIn("tryItOutEnabled: true", swagger_html)

    def test_route_registry_defines_runtime_boundaries(self) -> None:
            self.assertEqual("core", web_routes.route_for("GET", "/api/core/status").namespace)
            self.assertEqual("core", web_routes.route_for("GET", "/api/core/backups/download-archive").namespace)
            self.assertIsNone(web_routes.route_for("POST", "/api/service/releases/install"))
            self.assertIsNone(web_routes.route_for("GET", "/api/service/releases/install-plan"))
            self.assertEqual("web", web_routes.route_for("GET", "/api/web/status").namespace)
            self.assertEqual("web", web_routes.route_for("GET", "/api/web/run-preferences").namespace)
            self.assertEqual("web", web_routes.route_for("GET", "/api/web/candidate-domain-index-page").namespace)
            self.assertEqual("web", web_routes.route_for("GET", "/api/web/presets").namespace)
            self.assertEqual("web", web_routes.route_for("POST", "/api/web/presets/save").namespace)
            self.assertEqual("web", web_routes.route_for("GET", "/api/web/events/stream").namespace)
            self.assertIsNone(web_routes.route_for("GET", "/api/status"))
            self.assertIsNone(web_routes.route_for("GET", "/api/core/backups/download-file"))
            self.assertIsNone(web_routes.route_for("POST", "/api/settings"))
            self.assertFalse(any(spec.namespace == "legacy" for spec in web_routes.ROUTES))
            self.assertFalse(web_routes.route_for("GET", "/").allowed_in_core)
            self.assertFalse(web_routes.route_for("GET", "/api/web/run-preferences").allowed_in_core)
            self.assertFalse(web_routes.route_for("GET", "/api/web/events/stream").allowed_in_core)
            web_only_paths = {spec.path for spec in web_routes.ROUTES if spec.namespace == "web"}
            self.assertEqual(
                web_only_paths,
                {spec.path for spec in web_routes.ROUTES if not spec.allowed_in_core},
            )
            self.assertTrue(
                all(spec.allowed_in_core for spec in web_routes.ROUTES if spec.namespace in {"core", "service", "openapi"})
            )
            self.assertIn("/api/core/status", web_routes.JSON_GET_ROUTE_PATHS)
            self.assertNotIn("/api/core/backups/download-file", web_routes.route_paths(method="GET"))
            self.assertIn("/api/service/status", web_routes.JSON_GET_ROUTE_PATHS)
            self.assertNotIn("/api/service/releases/install-plan", web_routes.JSON_GET_ROUTE_PATHS)
            self.assertIn("/api/web/status", web_routes.JSON_GET_ROUTE_PATHS)
            self.assertIn("/api/web/run-preferences", web_routes.JSON_GET_ROUTE_PATHS)
            self.assertIn("/api/web/candidate-domain-index-page", web_routes.JSON_GET_ROUTE_PATHS)
            self.assertIn("/api/web/presets", web_routes.JSON_GET_ROUTE_PATHS)
            self.assertIn("/api/web/presets/domains", web_routes.JSON_GET_ROUTE_PATHS)
            self.assertNotIn("/api/status", web_routes.JSON_GET_ROUTE_PATHS)
            self.assertNotIn("/api/status", web_routes.JSON_HEAD_ROUTE_PATHS)
            self.assertIn("/api/core/strategy-discovery/start-run", web_routes.JSON_POST_ROUTE_PATHS)
            self.assertIn("/api/web/presets/save", web_routes.JSON_POST_ROUTE_PATHS)
            self.assertIn("/api/web/presets/delete-user-lists", web_routes.JSON_POST_ROUTE_PATHS)
            self.assertEqual({"/api/core/backups/upload"}, web_routes.UPLOAD_ROUTE_PATHS)

    def test_app_api_literals_are_registered_routes(self) -> None:
            source_path = Path(web_app.__file__)
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            api_literals = {
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith("/api/")
                and not node.value.endswith("/")
            }
            registered = {spec.path for spec in web_routes.ROUTES if spec.path.startswith("/api/")}
            self.assertFalse(api_literals - registered)

    def test_core_server_does_not_import_web_app_runtime(self) -> None:
            from gp_control_plane.web import core_server

            source = Path(core_server.__file__).read_text(encoding="utf-8")
            self.assertNotIn("from .app import", source)
            self.assertNotIn("gp_control_plane.web.app", source)

    def test_core_server_uses_api_server_entrypoint(self) -> None:
            from gp_control_plane.web import api_server, core_server

            config = AppConfig(output=OutputConfig(state_dir=Path("unused-state")))
            with mock.patch.object(api_server, "serve") as serve_mock:
                core_server.serve_core(config, host="127.0.0.1", port=18081)

            serve_mock.assert_called_once_with(config, host="127.0.0.1", port=18081, ui_enabled=False)

    def test_core_and_web_events_have_separate_payloads_and_cursors(self) -> None:
            with tempfile.TemporaryDirectory() as raw:
                config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
                with web_app._EVENT_CURSOR_LOCK:
                    web_app._EVENT_CURSOR_STATE.clear()

                core_events = web_app._events_response_payload(config, {}, stream="core")
                web_events = web_app._events_response_payload(config, {}, stream="web")

                core_types = {event["type"] for event in core_events["events"]}
                web_types = {event["type"] for event in web_events["events"]}
                self.assertIn("core.status", core_types)
                self.assertIn("strategy-discovery.progress", core_types)
                self.assertIn("strategy-candidates", core_types)
                self.assertNotIn("status", core_types)
                self.assertIn("status", web_types)
                self.assertIn("runs", web_types)
                self.assertIn("candidates", web_types)
                self.assertNotIn("core.status", web_types)
                self.assertTrue(all(str(event["event_id"]).startswith("core:") for event in core_events["events"]))
                self.assertTrue(all(str(event["event_id"]).startswith("web:") for event in web_events["events"]))

                unchanged = web_app._events_response_payload(
                    config,
                    {"after_id": [str(core_events["next_after_id"])]},
                    stream="core",
                )
                self.assertEqual([], unchanged["events"])
                self.assertEqual(core_events["next_after_id"], unchanged["next_after_id"])

                web_app.save_run_settings(config, {"curl_parallelism_default": 17})
                changed = web_app._events_response_payload(
                    config,
                    {"after_id": [str(core_events["next_after_id"])]},
                    stream="core",
                )

                self.assertEqual(["run-settings"], [event["type"] for event in changed["events"]])
                self.assertTrue(str(changed["events"][0]["event_id"]).startswith("core:"))

                upsert_candidates(
                    config.output.state_dir,
                    {
                        "candidates": [
                            {
                                "protocol": "tcp",
                                "args": "--dpi-desync=fake",
                                "domain": "youtube.com",
                                "test": "https",
                                "ip_version": "4",
                            }
                        ],
                        "common_candidates": [],
                    },
                    {"id": "run_1"},
                )
                candidate_changed = web_app._events_response_payload(
                    config,
                    {"after_id": [str(changed["next_after_id"])]},
                    stream="core",
                )

                self.assertEqual(["strategy-candidates"], [event["type"] for event in candidate_changed["events"]])
