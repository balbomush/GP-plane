"""Full public route and proxy parity using separate installed artifacts."""
from __future__ import annotations
import contextlib,http.client,json,socket,tempfile,threading,time,unittest
from pathlib import Path
from typing import Any
from unittest import mock
from gp_control_plane.config import AppConfig,OutputConfig
from gp_control_plane.state import read_state
from gp_control_plane.http import api_server as web_app,routes as web_routes
from gp_control_plane.http.core_server import serve_core
from tests import test_core_application_http as original
from tests.test_core_application_http import _http_request,_authenticated_headers,_bearer_authorization_for_state,_reserved_unavailable_loopback_port,_json_object_without_duplicate_keys,_JobRunnerThreadTracker,_openapi_test_request,_assert_openapi_response_contract,_http_sse_first_event
from tests.support.web_process import serve_web_proxy,start_web_process

@contextlib.contextmanager
def _captured_server_temporary_directory():
    with original._captured_server_temporary_directory() as (raw,start_core),contextlib.ExitStack() as web_cleanup:
        def start(function,config,**kwargs):
            if function is serve_web_proxy:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                owner=start_web_process(port,kwargs['core_url'])
                web_cleanup.callback(owner.close)
                return owner
            return start_core(function,config,**kwargs)
        yield raw,start

class PublicArtifactHttpTests(unittest.TestCase):
    def assertApiError(self,payload,code):
        self.assertEqual(set(payload),{'error'})
        error=payload['error'];self.assertIsInstance(error,dict)
        self.assertEqual(error.get('code'),code);self.assertIsInstance(error.get('message'),str);self.assertEqual(error.get('details'),{})
    def test_openapi_and_swagger_are_served_by_monolith_and_core_mode(self) -> None:
            for target in (serve_web_proxy, serve_core):
                with _captured_server_temporary_directory() as (raw, start_server):
                    tmp = Path(raw)
                    config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                    core = start_server(serve_core, config).port
                    port = core if target is serve_core else start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core}").port

                    status, headers, body = _http_request(port, "/openapi.json")
                    head_status, head_headers, head_body = _http_request(port, "/openapi.json", method="HEAD")
                    self.assertEqual(status, 200)
                    self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
                    self.assertEqual(head_status, 200)
                    self.assertEqual(head_headers.get("content-type"), "application/json; charset=utf-8")
                    self.assertEqual(int(head_headers.get("content-length") or "0"), len(body))
                    self.assertEqual(head_body, b"")
                    openapi_text = body.decode("utf-8")
                    openapi_contract = json.loads(
                        openapi_text,
                        object_pairs_hook=_json_object_without_duplicate_keys,
                    )
                    self.assertEqual(openapi_contract["openapi"], "3.1.0")
                    openapi_operations = {
                        (path, method.upper())
                        for path, operations in openapi_contract["paths"].items()
                        for method in operations
                    }
                    self.assertEqual(openapi_operations, web_routes.openapi_operations(core_only=target is serve_core))
                    if target is serve_core:
                        self.assertEqual(openapi_contract["info"]["title"], "GP Control Plane Core API")
                        self.assertIn("Callable Core/Service/OpenAPI operations", openapi_contract["info"]["description"])
                        self.assertFalse(any(path.startswith("/api/web/") for path in openapi_contract["paths"]))
                    else:
                        self.assertEqual(openapi_contract["info"]["title"], "GP Control Plane API")
                        self.assertIn("/api/web", openapi_text)
                        self.assertTrue(any(path.startswith("/api/web/") for path in openapi_contract["paths"]))
                    self.assertIn("/api/core/backups/download-archive", openapi_contract["paths"])
                    self.assertNotIn("/api/core/backups/download-file", openapi_contract["paths"])
                    self.assertEqual(
                        "downloadBackupArchive",
                        openapi_contract["paths"]["/api/core/backups/download-archive"]["get"]["operationId"],
                    )
                    self.assertNotIn("jsonSchemaDialect", openapi_contract)
                    self.assertEqual(
                        {"type": "http", "scheme": "bearer", "bearerFormat": "opaque"},
                        openapi_contract["components"]["securitySchemes"]["bearerAuth"],
                    )
                    self.assertEqual([{"bearerAuth": []}], openapi_contract["security"])
                    self.assertEqual([], openapi_contract["paths"]["/api/health"]["get"]["security"])
                    self.assertEqual([], openapi_contract["paths"]["/api/auth/login"]["post"]["security"])
                    self.assertEqual([{"url": "/"}], [{"url": server["url"]} for server in openapi_contract["servers"]])
                    self.assertNotIn("localhost", openapi_text)
                    self.assertNotIn("127.0.0.1:8081", openapi_text)
                    examples = openapi_contract["components"]["examples"]
                    self.assertIn("StartRunRequestMultiDomain", examples)
                    self.assertEqual(30, examples["StartRunRequestMultiDomain"]["value"]["curl_parallelism"])
                    self.assertNotIn("mode_settings", examples["StartRunRequestMultiDomain"]["value"])
                    start_schema = openapi_contract["components"]["schemas"]["StartRunRequest"]
                    self.assertEqual(["domains"], start_schema["required"])
                    self.assertNotIn("mode_settings", start_schema["properties"])
                    self.assertFalse(start_schema["properties"]["settings"]["additionalProperties"])
                    self.assertIn("curl_max_time", start_schema["properties"]["settings"]["properties"])
                    self.assertNotIn("mode_settings", start_schema["properties"]["settings"]["properties"])
                    error_schema = openapi_contract["components"]["schemas"]["ErrorResponse"]
                    envelope_schema = error_schema["properties"]["error"]
                    self.assertEqual("object", envelope_schema["type"])
                    self.assertEqual(["code", "message", "details"], envelope_schema["required"])
                    self.assertEqual(
                        {"error": {"code": "invalid_request", "message": "The request is invalid.", "details": {}}},
                        examples["ErrorResponse"]["value"],
                    )
                    self.assertIn(
                        "409",
                        openapi_contract["paths"]["/api/core/strategy-discovery/stop-current-run"]["post"]["responses"],
                    )
                    self.assertNotIn(
                        "404",
                        openapi_contract["paths"]["/api/core/strategy-discovery/stop-current-run"]["post"]["responses"],
                    )
                    category_responses = openapi_contract["paths"]["/api/core/presets/v2fly/category-domains"]["get"]["responses"]
                    self.assertIn("400", category_responses)
                    self.assertNotIn("404", category_responses)
                    self.assertNotIn("503", category_responses)
                    categories_responses = openapi_contract["paths"]["/api/core/presets/v2fly/categories"]["get"]["responses"]
                    self.assertNotIn("503", categories_responses)
                    self.assertNotIn("InstallReleaseRequest", openapi_contract["components"]["schemas"])
                    self.assertNotIn("ReleaseInstallPlan", openapi_contract["components"]["schemas"])
                    self.assertNotIn("ReleaseUpdateInfo", openapi_contract["components"]["schemas"])
                    self.assertNotIn("ReleaseInstallAccepted", openapi_contract["components"]["schemas"])
                    self.assertNotIn("InstallChannel", openapi_contract["components"]["schemas"])
                    self.assertNotIn("InstallReleaseRequest", examples)
                    self.assertNotIn("ReleaseInstallPlanResponse", examples)
                    self.assertNotIn("ReleaseInstallAccepted", examples)
                    self.assertNotIn("InstallChannel", examples)
                    self.assertIn("CoreStatusRunning", examples)
                    self.assertIn("PagedStrategyCandidatesResponse", examples)
                    self.assertIn("PagedCandidateDomainIndexResponse", examples)
                    self.assertIn("WebRunPreferencesResponse", examples)
                    self.assertIn("WebRunPreferencesSaveRequest", examples)
                    self.assertIn("WebRunPreferencesResponse", openapi_contract["components"]["schemas"])
                    self.assertIn("WebRunPreferencesSaveRequest", openapi_contract["components"]["schemas"])
                    self.assertIn("PagedCandidateDomainIndexResponse", openapi_contract["components"]["schemas"])
                    self.assertIn("WebPresetsResponse", openapi_contract["components"]["schemas"])
                    self.assertIn("WebPresetDomainsResponse", openapi_contract["components"]["schemas"])
                    self.assertNotIn("/api/service/releases/install", openapi_contract["paths"])
                    if target is serve_core:
                        self.assertNotIn("/api/web/candidate-domain-index-page", openapi_contract["paths"])
                        self.assertNotIn("/api/web/presets", openapi_contract["paths"])
                        self.assertNotIn("/api/web/presets/domains", openapi_contract["paths"])
                        self.assertNotIn("/api/web/presets/save", openapi_contract["paths"])
                        self.assertNotIn("/api/web/presets/delete-user-lists", openapi_contract["paths"])
                        self.assertNotIn("/api/web/events/stream", openapi_contract["paths"])
                    else:
                        self.assertIn("/api/web/candidate-domain-index-page", openapi_contract["paths"])
                        self.assertIn("/api/web/presets", openapi_contract["paths"])
                        self.assertIn("/api/web/presets/domains", openapi_contract["paths"])
                        self.assertIn("/api/web/presets/save", openapi_contract["paths"])
                        self.assertIn("/api/web/presets/delete-user-lists", openapi_contract["paths"])
                        self.assertIn("/api/web/events/stream", openapi_contract["paths"])
                    web_preset_save_schema = openapi_contract["components"]["schemas"]["WebPresetSaveRequest"]
                    self.assertNotIn("minItems", web_preset_save_schema["properties"]["domains"])
                    runtime_busy_response = openapi_contract["components"]["responses"]["RuntimeBusy"]
                    self.assertEqual(
                        {
                            "error": {
                                "code": "runtime_busy",
                                "message": "The operation is unavailable while discovery is running.",
                                "details": {},
                            }
                        },
                        runtime_busy_response["content"]["application/json"]["examples"]["runtimeBusy"]["value"],
                    )
                    for backup_mutation_path in (
                        "/api/core/backups/create",
                        "/api/core/backups/restore",
                        "/api/core/backups/delete",
                        "/api/core/backups/upload",
                    ):
                        responses = openapi_contract["paths"][backup_mutation_path]["post"]["responses"]
                        self.assertEqual({"$ref": "#/components/responses/RuntimeBusy"}, responses["409"])
                    self.assertEqual(
                        "legacy-root-apis-removed-in-alpha",
                        openapi_contract["x-gp-decisions"]["legacy_api_compatibility"],
                    )
                    self.assertNotIn("/api/service/diagnostics", openapi_contract["paths"])
                    self.assertNotIn("ServiceDiagnostics", openapi_contract["components"]["schemas"])
                    self.assertNotIn("ServiceDiagnostics", examples)
                    log_tail_schema = openapi_contract["components"]["schemas"]["RunLogTail"]
                    self.assertIn("stdout_tail", log_tail_schema["required"])
                    self.assertIn("stderr_tail", log_tail_schema["required"])
                    self.assertIn("progress", log_tail_schema["required"])
                    self.assertNotIn("lines", log_tail_schema["properties"])
                    self.assertNotIn("lines", examples["RunLogTail"]["value"])
                    self.assertIn(
                        "multiDomain30Parallel",
                        openapi_contract["paths"]["/api/core/strategy-discovery/start-run"]["post"]["requestBody"][
                            "content"
                        ]["application/json"]["examples"],
                    )
                    self.assertIn(
                        "plainError",
                        openapi_contract["components"]["responses"]["Error"]["content"]["application/json"]["examples"],
                    )
                    self.assertIn("/api/core/presets/delete-user-domain-list", openapi_contract["paths"])
                    self.assertNotIn("/api/core/presets/delete-user-lists", openapi_contract["paths"])
                    delete_schema = openapi_contract["components"]["schemas"]["DeleteDomainListRequest"]
                    self.assertEqual(["list_ids"], delete_schema["required"])
                    self.assertEqual(1, delete_schema["properties"]["list_ids"]["minItems"])

                    status, headers, body = _http_request(port, "/swagger")
                    swagger_html = body.decode("utf-8")
                    self.assertEqual(status, 200)
                    self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
                    self.assertIn("SwaggerUIBundle", swagger_html)
                    self.assertIn("url: '/openapi.json'", swagger_html)

                    status, headers, body = _http_request(port, "/swagger", method="HEAD")
                    self.assertEqual(status, 200)
                    self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
                    self.assertEqual(body, b"")

    def test_legacy_api_routes_return_not_found_in_alpha_runtimes(self) -> None:
            representative_legacy_requests = [
                ("GET", "/api/status", None, {}),
                ("POST", "/api/settings", {"settings": {"curl_parallelism_max": 12}}, {}),
                ("POST", "/api/settings", b"{", {"Content-Type": "application/json"}),
                ("GET", "/api/events", None, {}),
                ("GET", "/api/diagnostics", None, {}),
                ("POST", "/api/presets/save", {"scope": "finder", "name": "work", "domains": ["youtube.com"]}, {}),
                ("POST", "/api/presets/save", b"01234567890", {"Content-Type": "application/json"}),
                ("POST", "/api/backups/create", {}, {}),
                ("GET", "/api/releases/update-plan", None, {}),
            ]

            with _captured_server_temporary_directory() as (raw, start_server):
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                with mock.patch.object(web_app, "MAX_JSON_REQUEST_BYTES", 10):
                    core_port = start_server(serve_core, config).port
                    proxy_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}").port

                    for port in (core_port, proxy_port):
                        for method, path, payload, headers in representative_legacy_requests:
                            if isinstance(payload, bytes):
                                raw_body = payload
                            elif payload is not None:
                                raw_body = json.dumps(payload).encode("utf-8")
                            else:
                                raw_body = None
                            request_headers = dict(headers)
                            request_headers.setdefault("Authorization", _bearer_authorization_for_state(config.output.state_dir))
                            if raw_body is not None and "Content-Type" not in request_headers:
                                request_headers["Content-Type"] = "application/json"
                            status, response_headers, body = _http_request(
                                port,
                                path,
                                method=method,
                                body=raw_body,
                                headers=request_headers,
                            )
                            message = (port, method, path, body.decode("utf-8", errors="replace"))
                            self.assertEqual(status, 404, message)
                            self.assertEqual(response_headers.get("content-type"), "application/json; charset=utf-8", message)

    def test_openapi_paths_are_callable_through_split_core_and_web_proxy_runtime(self) -> None:
            with _captured_server_temporary_directory() as (raw, start_server):
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                web_app.save_settings(config, {"curl_parallelism_max": 50, "curl_parallelism_default": 10})
                snapshot = web_app.create_snapshot_if_idle(config.output.state_dir)["snapshot"]["id"]
                release = {
                    "channel": "stable",
                    "available_version": "v0.0.0-test",
                    "url": "https://example.invalid/release",
                    "published_at": "",
                }

                def create_post_run_snapshot(_state_dir: Path) -> dict[str, object]:
                    return {
                        "kind": "snapshot",
                        "status": "success",
                        "completed_at": "2026-08-13T00:00:00Z",
                        "snapshot_id": "openapi-contract-post-run-snapshot",
                        "snapshot": {"id": "openapi-contract-post-run-snapshot"},
                    }

                with (
                    mock.patch.object(web_app, "run_multi_domain_discovery", return_value={"status": "success"}),
                    mock.patch.object(web_app, "run_standard_discovery", return_value={"status": "success"}),
                    mock.patch.object(web_app.service_api, "release_channel_info", return_value=release),
                    mock.patch.object(web_app.service_api, "fetch_v2fly_revision", return_value="remote-test-revision"),
                    mock.patch.object(web_app.service_api, "prepare_v2fly_local_storage", return_value={"count": 0}),
                    mock.patch.object(web_app, "create_post_run_snapshot", side_effect=create_post_run_snapshot) as post_run_snapshot,
                    _JobRunnerThreadTracker() as runner_threads,
                ):
                    core_port = start_server(serve_core, config).port
                    web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}").port

                    openapi_contract = json.loads(
                        web_app.openapi_json_bytes().decode("utf-8"),
                        object_pairs_hook=_json_object_without_duplicate_keys,
                    )
                    core_openapi_status, core_openapi_headers, core_openapi_body = _http_request(core_port, "/openapi.json")
                    proxy_openapi_status, proxy_openapi_headers, proxy_openapi_body = _http_request(web_port, "/openapi.json")
                    self.assertEqual(core_openapi_status, 200)
                    self.assertEqual(core_openapi_headers.get("content-type"), "application/json; charset=utf-8")
                    self.assertEqual(proxy_openapi_status, 200)
                    self.assertEqual(proxy_openapi_headers.get("content-type"), "application/json; charset=utf-8")
                    full_openapi_text = web_app.openapi_json_bytes().decode("utf-8")
                    core_openapi_text = core_openapi_body.decode("utf-8")
                    proxy_openapi_text = proxy_openapi_body.decode("utf-8")
                    self.assertIn("/api/web", full_openapi_text)
                    self.assertIn("/api/web", proxy_openapi_text)
                    core_openapi_contract = json.loads(
                        core_openapi_text,
                        object_pairs_hook=_json_object_without_duplicate_keys,
                    )
                    proxy_openapi_contract = json.loads(
                        proxy_openapi_text,
                        object_pairs_hook=_json_object_without_duplicate_keys,
                    )
                    core_openapi_operations = {
                        (path, method.upper())
                        for path, operations in core_openapi_contract["paths"].items()
                        for method in operations
                    }
                    proxy_openapi_operations = {
                        (path, method.upper())
                        for path, operations in proxy_openapi_contract["paths"].items()
                        for method in operations
                    }
                    self.assertEqual(core_openapi_operations, web_routes.openapi_operations(core_only=True))
                    self.assertFalse(any(path.startswith("/api/web/") for path in core_openapi_contract["paths"]))
                    self.assertEqual(proxy_openapi_operations, web_routes.openapi_operations())
                    self.assertTrue(any(path.startswith("/api/web/") for path in proxy_openapi_contract["paths"]))
                    expected = {(method.upper(), path) for path, ops in openapi_contract["paths"].items() for method in ops}
                    web_expected = {
                        (method, path)
                        for method, path in expected
                        if web_routes.route_for(method, path) and web_routes.route_for(method, path).namespace == "web"
                    }
                    core_checked: set[tuple[str, str]] = set()
                    proxy_checked: set[tuple[str, str]] = set()
                    successful_start_requests = 0
                    for runtime, port in (("core", core_port), ("proxy", web_port)):
                        for path, operations in openapi_contract["paths"].items():
                            for raw_method, operation in operations.items():
                                method = raw_method.upper()
                                request_path, raw_body, request_headers = _openapi_test_request(
                                    openapi_contract,
                                    path,
                                    method,
                                    operation,
                                    snapshot,
                                )
                                route = web_routes.route_for(method, path)
                                if runtime == "core" and route and route.namespace == "web":
                                    status, _headers, response_body = _http_request(
                                        port,
                                        request_path,
                                        method=method,
                                        body=raw_body,
                                        headers=request_headers,
                                    )
                                    message = (runtime, method, request_path, status, response_body.decode("utf-8", errors="replace")[:500])
                                    self.assertEqual(status, 404, message)
                                    continue
                                if path == "/api/web/events/stream" and method == "GET":
                                    status, headers, first_line, second_line = _http_sse_first_event(port, request_path)
                                    message = (runtime, method, request_path, status, first_line, second_line)
                                    proxy_checked.add((method, path))
                                    self.assertEqual(status, 200, message)
                                    self.assertEqual(headers.get("content-type"), "text/event-stream; charset=utf-8", message)
                                    self.assertEqual(first_line, "event: status", message)
                                    self.assertTrue(second_line.startswith("data:"), message)
                                    continue
                                status, headers, response_body = _http_request(
                                    port,
                                    request_path,
                                    method=method,
                                    body=raw_body,
                                    headers=request_headers,
                                )
                                message = (runtime, method, request_path, status, response_body.decode("utf-8", errors="replace")[:500])
                                if runtime == "core":
                                    core_checked.add((method, path))
                                else:
                                    proxy_checked.add((method, path))
                                _assert_openapi_response_contract(
                                    self,
                                    openapi_contract,
                                    operation,
                                    status,
                                    headers,
                                    response_body,
                                    context=message,
                                )
                                if path == "/api/core/backups/download-archive" and status == 200:
                                    self.assertGreater(len(response_body), 0, message)
                                if path == "/api/core/strategy-discovery/start-run" and method == "POST":
                                    self.assertEqual(status, 202, message)
                                    successful_start_requests += 1
                                    runner_threads.join_tracked()

                    core_head_status, core_head_headers, core_head_body = _http_request(
                        core_port,
                        "/api/web/events/stream",
                        method="HEAD",
                    )
                    proxy_head_status, proxy_head_headers, proxy_head_body = _http_request(
                        web_port,
                        "/api/web/events/stream",
                        method="HEAD",
                    )
                    self.assertEqual(core_head_status, 404)
                    self.assertEqual(core_head_headers.get("content-type"), "application/json; charset=utf-8")
                    self.assertEqual(core_head_body, b"")
                    self.assertEqual(proxy_head_status, 200)
                    self.assertEqual(proxy_head_headers.get("content-type"), "text/event-stream; charset=utf-8")
                    self.assertEqual(proxy_head_body, b"")
                    self.assertEqual(expected - web_expected, core_checked)
                    self.assertEqual(expected, proxy_checked)
                    self.assertEqual(successful_start_requests, 2)
                    self.assertEqual(runner_threads.tracked_count, successful_start_requests)
                    self.assertEqual(post_run_snapshot.call_count, successful_start_requests)

                    for runtime, port in (("core", core_port), ("proxy", web_port)):
                        old_status, old_headers, old_body = _http_request(
                            port,
                            f"/api/core/backups/download-file?snapshot_id={snapshot}",
                        )
                        self.assertEqual(old_status, 404, runtime)
                        self.assertEqual(old_headers.get("content-type"), "application/json; charset=utf-8", runtime)
                        self.assertIn("not found", old_body.decode("utf-8"), runtime)

    def test_web_proxy_serves_ui_and_forwards_api_to_core(self) -> None:
            with _captured_server_temporary_directory() as (raw, start_server):
                tmp = Path(raw)
                config = AppConfig(
                    output=OutputConfig(
                        state_dir=tmp / "state",
                    ),
                )
                core_port = start_server(serve_core, config).port
                web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}").port

                connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=5)
                connection.request("GET", "/")
                root_response = connection.getresponse()
                root_body = root_response.read().decode("utf-8")
                connection.close()

                core_status, _core_headers, core_body = _http_request(web_port, "/api/core/status")
                service_status, _service_headers, service_body = _http_request(web_port, "/api/service/status")
                web_system_status, _web_system_headers, web_system_body = _http_request(web_port, "/api/web/status")
                web_status, _web_headers, web_body = _http_request(web_port, "/api/web/run-preferences")
                legacy_status, _legacy_headers, legacy_body = _http_request(web_port, "/api/status")

                self.assertEqual(root_response.status, 200)
                self.assertIn("<!doctype html>", root_body.lower())
                self.assertEqual(core_status, 200)
                self.assertIn('"state"', core_body.decode("utf-8"))
                self.assertEqual(service_status, 200)
                self.assertIn('"version"', service_body.decode("utf-8"))
                self.assertEqual(web_system_status, 200)
                self.assertIn('"zapret2"', web_system_body.decode("utf-8"))
                self.assertEqual(web_status, 200)
                self.assertIn('"run_preferences"', web_body.decode("utf-8"))
                self.assertEqual(legacy_status, 404)
                self.assertIn("not found", legacy_body.decode("utf-8"))

    def test_web_proxy_forwards_post_body_and_query_to_core(self) -> None:
            with _captured_server_temporary_directory() as (raw, start_server):
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                core_port = start_server(serve_core, config).port
                web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}").port

                status, headers, body = _http_request(
                    web_port,
                    "/api/core/run-settings/save",
                    method="POST",
                    body=json.dumps({"curl_parallelism_default": 17, "curl_parallelism_max": 50}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))
                self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
                self.assertEqual(17, json.loads(body.decode("utf-8"))["curl_parallelism_default"])

                status, _headers, body = _http_request(web_port, "/api/web/runs/history-page?limit=1&offset=2")
                self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))
                page = json.loads(body.decode("utf-8"))
                self.assertEqual(1, page["limit"])
                self.assertEqual(2, page["offset"])

    def test_web_proxy_rejects_oversize_api_body_without_forwarding_to_core(self) -> None:
            with _captured_server_temporary_directory() as (raw, start_server):
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                authorization = _bearer_authorization_for_state(config.output.state_dir)
                with contextlib.nullcontext():
                    core_port = start_server(serve_core, config).port
                    web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}").port

                    status, headers, body = _http_request(
                        web_port,
                        "/api/core/run-settings/save",
                        method="POST",
                        body=b"x" * (1024 * 1024 + 1),
                        headers={"Content-Type": "application/json", "Authorization": authorization},
                    )

                self.assertEqual(status, 413)
                self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
                self.assertApiError(json.loads(body.decode("utf-8")), "request_too_large")
                self.assertNotIn("settings", read_state(config.output.state_dir))

    def test_web_proxy_serves_head_requests_without_body(self) -> None:
            with _captured_server_temporary_directory() as (raw, start_server):
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                core_port = start_server(serve_core, config).port
                web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}").port

                root_status, root_headers, root_body = _http_request(web_port, "/", method="HEAD")
                openapi_status, openapi_headers, openapi_body = _http_request(web_port, "/openapi.json", method="HEAD")

                self.assertEqual(root_status, 200)
                self.assertEqual(root_headers.get("content-type"), "text/html; charset=utf-8")
                self.assertGreater(int(root_headers.get("content-length") or "0"), 0)
                self.assertEqual(root_body, b"")
                self.assertEqual(openapi_status, 200)
                self.assertEqual(openapi_headers.get("content-type"), "application/json; charset=utf-8")
                self.assertGreater(int(openapi_headers.get("content-length") or "0"), 0)
                self.assertEqual(openapi_body, b"")

    def test_web_proxy_reports_bad_gateway_when_core_is_unavailable(self) -> None:
            with (
                _captured_server_temporary_directory() as (raw, start_server),
                _reserved_unavailable_loopback_port() as unused_core_port,
            ):
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{unused_core_port}").port
                protected_headers = {"Authorization": _bearer_authorization_for_state(config.output.state_dir)}

                openapi_status, openapi_headers, openapi_body = _http_request(web_port, "/openapi.json")
                swagger_status, swagger_headers, swagger_body = _http_request(web_port, "/swagger")
                core_status, _, core_body = _http_request(web_port, "/api/core/status", headers=protected_headers)
                web_system_status, web_system_headers, web_system_body = _http_request(
                    web_port, "/api/web/status", headers=protected_headers
                )
                web_status, web_headers, web_body = _http_request(web_port, "/api/web/run-preferences", headers=protected_headers)
                legacy_status, legacy_headers, legacy_body = _http_request(web_port, "/api/status", headers=protected_headers)
                unknown_status, unknown_headers, unknown_body = _http_request(web_port, "/api/unknown", headers=protected_headers)

                self.assertEqual(openapi_status, 200)
                self.assertEqual(openapi_headers.get("content-type"), "application/json; charset=utf-8")
                self.assertIn("/api/web/run-preferences", json.loads(openapi_body.decode("utf-8"))["paths"])
                self.assertEqual(swagger_status, 200)
                self.assertEqual(swagger_headers.get("content-type"), "text/html; charset=utf-8")
                self.assertIn("SwaggerUIBundle", swagger_body.decode("utf-8"))
                self.assertEqual(core_status, 502)
                self.assertApiError(json.loads(core_body.decode("utf-8")), "core_unavailable")
                self.assertEqual(web_system_status, 502)
                self.assertEqual(web_system_headers.get("content-type"), "application/json; charset=utf-8")
                self.assertApiError(json.loads(web_system_body.decode("utf-8")), "core_unavailable")
                self.assertEqual(web_status, 502)
                self.assertEqual(web_headers.get("content-type"), "application/json; charset=utf-8")
                self.assertApiError(json.loads(web_body.decode("utf-8")), "core_unavailable")
                self.assertEqual(legacy_status, 502)
                self.assertEqual(legacy_headers.get("content-type"), "application/json; charset=utf-8")
                self.assertApiError(json.loads(legacy_body.decode("utf-8")), "core_unavailable")
                self.assertEqual(unknown_status, 502)
                self.assertEqual(unknown_headers.get("content-type"), "application/json; charset=utf-8")
                self.assertApiError(json.loads(unknown_body.decode("utf-8")), "core_unavailable")

    def test_web_proxy_reports_core_unavailable_for_unknown_protected_routes_with_dead_core(self) -> None:
            with (
                _captured_server_temporary_directory() as (raw, start_server),
                _reserved_unavailable_loopback_port() as unused_core_port,
            ):
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                authorization = _bearer_authorization_for_state(config.output.state_dir)
                with contextlib.nullcontext():
                    web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{unused_core_port}").port

                    cases = (
                        ("GET", "/api/core/not-a-route", None, {}),
                        ("GET", "/api/service/not-a-route", None, {}),
                        ("POST", "/api/core/not-a-route", b"{bad", {"Content-Type": "application/json"}),
                        ("POST", "/api/service/not-a-route", b"{bad", {"Content-Type": "application/json"}),
                        ("POST", "/api/core/not-a-route", b"x" * (1024 * 1024 + 1), {"Content-Type": "application/json"}),
                        ("POST", "/api/service/not-a-route", b"x" * (1024 * 1024 + 1), {"Content-Type": "application/json"}),
                    )
                    for method, path, request_body, request_headers in cases:
                        request_headers["Authorization"] = authorization
                        status, response_headers, response_body = _http_request(
                            web_port,
                            path,
                            method=method,
                            body=request_body,
                            headers=request_headers,
                        )
                        message = (method, path, response_body.decode("utf-8", errors="replace"))
                        self.assertEqual(status, 502, message)
                        self.assertEqual(response_headers.get("content-type"), "application/json; charset=utf-8")
                        self.assertApiError(json.loads(response_body.decode("utf-8")), "core_unavailable")
                        self.assertNotIn("core api is unavailable", response_body.decode("utf-8"), message)
                        self.assertNotIn("request body is too large", response_body.decode("utf-8"), message)
