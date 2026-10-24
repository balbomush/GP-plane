from tests.support.auth_http import *
from tests.support import auth_http as support
globals().update({key:value for key,value in vars(support).items() if key.startswith("_") and not key.startswith("__")})

class BearerAuthHttpTests(OwnedHttpCase):
    def test_independent_web_public_allowlist_and_protected_transport_routes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            core_port = _start_server(serve, config, ui_enabled=False)
            port = _start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}")

            for path in ("/", "/swagger", "/swagger/", "/openapi.json", "/api/health"):
                status, _headers, _body = _request(port, path)
                self.assertEqual(status, 200, path)
            status, _headers, _body = _request(port, "/api/health", method="HEAD")
            self.assertEqual(status, 200)

            login_status, _headers, login_body = _request(
                port,
                "/api/auth/login",
                method="POST",
                body=_json_bytes({"username": "admin", "password": "admin"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(login_status, 200)
            token = json.loads(login_body)["access_token"]
            bearer = {"Authorization": f"Bearer {token}"}

            anonymous_requests = (
                ("GET", "/api/core/strategy-discovery/current-run-progress", None, {}),
                ("POST", "/api/core/strategy-discovery/stop-current-run", _json_bytes({"dry_run": True}), {"Content-Type": "application/json"}),
                ("POST", "/api/core/backups/upload", b"not-a-zip", {"Content-Type": "application/zip"}),
                ("GET", "/api/core/backups/download-archive?snapshot_id=missing", None, {}),
                ("POST", "/api/core/clean-install-vaults/create", b"{}", {"Content-Type": "application/json"}),
                ("GET", "/api/core/clean-install-vaults/list", None, {}),
                ("GET", "/api/core/clean-install-vaults/status?vault_id=missing", None, {}),
                (
                    "POST",
                    "/api/core/clean-install-vaults/restore",
                    _json_bytes({"vault_id": "missing"}),
                    {"Content-Type": "application/json"},
                ),
                ("HEAD", "/api/core/strategy-candidates/export", None, {}),
                ("GET", "/api/web/events/stream", None, {"Accept": "text/event-stream"}),
            )
            for method, path, body, headers in anonymous_requests:
                status, response_headers, response_body = _request(port, path, method=method, body=body, headers=headers)
                self.assertEqual(status, 401, f"{method} {path}")
                self.assertEqual(response_headers.get("content-type"), "application/json; charset=utf-8")
                if method != "HEAD":
                    self.assertIn("error", json.loads(response_body))

            status, _headers, body = _request(port, "/api/core/strategy-discovery/current-run-progress", headers=bearer)
            self.assertEqual(status, 200)
            self.assertIn("status", json.loads(body))
            status, _headers, body = _request(port, "/api/web/run-preferences", headers=bearer)
            self.assertEqual(status, 200)
            self.assertIn("run_preferences", json.loads(body))

            status, _headers, body = _request(
                port,
                "/api/auth/change-password",
                method="POST",
                body=_json_bytes({"current_password": "admin", "new_password": "short"}),
                headers={**bearer, "Content-Type": "application/json"},
            )
            self.assertEqual(status, 400)
            self.assertIn("error", json.loads(body))

            status, _headers, body = _request(port, "/openapi.json")
            self.assertEqual(status, 200)
            contract = json.loads(body)
            self.assertEqual(contract["components"]["securitySchemes"]["bearerAuth"]["scheme"], "bearer")
            self.assertEqual(contract["security"], [{"bearerAuth": []}])
            for path, operations in contract["paths"].items():
                for method, operation in operations.items():
                    if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}:
                        continue
                    expected = [] if (path, method.lower()) in {
                        ("/api/health", "get"),
                        ("/api/auth/login", "post"),
                    } else [{"bearerAuth": []}]
                    self.assertEqual(operation.get("security", contract["security"]), expected, f"{method.upper()} {path}")


    def test_split_proxy_forwards_auth_and_rotates_shared_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            core_port = _start_server(serve, config, ui_enabled=False)
            proxy_port = _start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}")

            for path in ("/", "/swagger", "/openapi.json", "/api/health"):
                status, _headers, _body = _request(proxy_port, path)
                self.assertEqual(status, 200, path)
            status, _headers, _body = _request(proxy_port, "/api/web/events/stream")
            self.assertEqual(status, 401)
            status, _headers, _body = _request(proxy_port, "/api/core/strategy-discovery/current-run-progress")
            self.assertEqual(status, 401)

            status, _headers, body = _request(
                proxy_port,
                "/api/auth/login",
                method="POST",
                body=_json_bytes({"username": "admin", "password": "admin"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200)
            old_token = json.loads(body)["access_token"]
            old_bearer = {"Authorization": f"Bearer {old_token}"}
            status, _headers, _body = _request(proxy_port, "/api/core/strategy-discovery/current-run-progress", headers=old_bearer)
            self.assertEqual(status, 200)
            status, _headers, body = _request(
                proxy_port,
                "/api/auth/change-password",
                method="POST",
                body=_json_bytes({"current_password": "admin", "new_password": "newpass8"}),
                headers={**old_bearer, "Content-Type": "application/json"},
            )
            self.assertEqual(status, 200)
            new_token = json.loads(body)["access_token"]
            status, _headers, _body = _request(proxy_port, "/api/core/strategy-discovery/current-run-progress", headers=old_bearer)
            self.assertEqual(status, 401)
            status, _headers, _body = _request(
                proxy_port,
                "/api/core/strategy-discovery/current-run-progress",
                headers={"Authorization": f"Bearer {new_token}"},
            )
            self.assertEqual(status, 200)
            status, _headers, body = _request(
                proxy_port,
                "/api/auth/change-password",
                method="POST",
                body=_json_bytes({"current_password": "newpass8", "new_password": "admin"}),
                headers={"Authorization": f"Bearer {new_token}", "Content-Type": "application/json"},
            )
            self.assertEqual(status, 200, body)
            reset_token = json.loads(body)["access_token"]
            self.assertEqual(
                _request(proxy_port, "/api/core/strategy-discovery/current-run-progress", headers={"Authorization": f"Bearer {new_token}"})[0],
                401,
            )
            self.assertEqual(
                _request(proxy_port, "/api/auth/login", method="POST", body=_json_bytes({"username": "admin", "password": "admin"}), headers={"Content-Type": "application/json"})[0],
                200,
            )
            self.assertEqual(
                _request(proxy_port, "/api/core/strategy-discovery/current-run-progress", headers={"Authorization": f"Bearer {reset_token}"})[0],
                200,
            )


    def test_invalid_current_password_is_400_without_revoking_bearer_until_successful_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            core = _start_managed_server(serve, config, ui_enabled=False)
            proxy = _start_managed_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}")
            try:
                status, _headers, body = _request(
                    core.port,
                    "/api/auth/login",
                    method="POST",
                    body=_json_bytes({"username": "admin", "password": "admin"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, body)
                old_bearer = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}

                for port in (core.port, proxy.port):
                    status, _headers, body = _request(
                        port,
                        "/api/auth/change-password",
                        method="POST",
                        body=_json_bytes({"current_password": "wrong", "new_password": "newpass8"}),
                        headers={**old_bearer, "Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 400, body)
                    self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")
                    self.assertEqual(
                        _request(port, "/api/core/strategy-discovery/current-run-progress", headers=old_bearer)[0], 200
                    )

                status, _headers, body = _request(
                    core.port,
                    "/api/auth/change-password",
                    method="POST",
                    body=_json_bytes({"current_password": "admin", "new_password": "newpass8"}),
                    headers={**old_bearer, "Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, body)
                for port in (core.port, proxy.port):
                    self.assertEqual(
                        _request(port, "/api/core/strategy-discovery/current-run-progress", headers=old_bearer)[0], 401
                    )
            finally:
                proxy.close()
                core.close()


    def test_spawned_core_and_proxy_share_password_rotation_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            config = AppConfig(output=OutputConfig(state_dir=state_dir))
            context = multiprocessing.get_context("spawn")
            core_port = _free_port()
            core_ready = context.Event()
            core_stop = context.Event()
            core_errors = context.Queue()
            core_process = context.Process(
                target=_serve_core_in_process,
                args=(str(state_dir), core_port, core_ready, core_stop, core_errors),
                name="spawned-core-server",
            )
            proxy: _ManagedServer | None = None
            core_process.start()
            try:
                self.assertTrue(core_ready.wait(timeout=_PROCESS_TIMEOUT_SECONDS))
                try:
                    child_error = core_errors.get_nowait()
                except queue.Empty:
                    child_error = None
                self.assertIsNone(child_error, child_error)
                self.assertEqual(_request(core_port, "/api/health")[0], 200)
                proxy = _start_managed_server(
                    serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}"
                )
                status, _headers, body = _request(
                    core_port,
                    "/api/auth/login",
                    method="POST",
                    body=_json_bytes({"username": "admin", "password": "admin"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, body)
                old_bearer = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}
                for port in (core_port, proxy.port):
                    self.assertEqual(
                        _request(port, "/api/core/strategy-discovery/current-run-progress", headers=old_bearer)[0], 200
                    )

                status, _headers, body = _request(
                    core_port,
                    "/api/auth/change-password",
                    method="POST",
                    body=_json_bytes({"current_password": "admin", "new_password": "newpass8"}),
                    headers={**old_bearer, "Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, body)
                fresh_bearer = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}
                for port in (core_port, proxy.port):
                    self.assertEqual(
                        _request(port, "/api/core/strategy-discovery/current-run-progress", headers=old_bearer)[0], 401
                    )
                    self.assertEqual(
                        _request(port, "/api/core/strategy-discovery/current-run-progress", headers=fresh_bearer)[0], 200
                    )
            finally:
                if proxy is not None:
                    proxy.close()
                core_stop.set()
                _join_process(self, core_process)


    def test_active_discovery_can_be_authorized_and_stopped_during_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            job_started = threading.Event()
            cancellation_observed = threading.Event()
            cancel_hook_called = threading.Event()
            allow_terminal_persistence = threading.Event()

            def controlled_discovery(_config: AppConfig, _payload: dict[str, Any], stop: Any, _run_id: str) -> dict[str, str]:
                job_started.set()
                if not stop.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
                    raise TimeoutError("test stop endpoint did not cancel the active job")
                cancellation_observed.set()
                if not allow_terminal_persistence.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
                    raise TimeoutError("test did not release the writer lock for terminal persistence")
                append_run(
                    _config.output.state_dir,
                    {
                        "id": _run_id,
                        "kind": "multi-domain-discovery",
                        "status": "stopped",
                        "timestamp": "test-stopped",
                        "started_at": "test-started",
                        "completed_at": "test-stopped",
                        "domains": list(_payload.get("domains") or []),
                    },
                )
                return {"status": "stopped"}

            with mock.patch(
                "gp_control_plane.web.api_server._job_zapret_multi_domain_discovery",
                side_effect=controlled_discovery,
            ), mock.patch(
                "gp_control_plane.web.api_server.cleanup_nft_blockcheck_tables",
                side_effect=cancel_hook_called.set,
            ):
                core = _start_managed_server(serve, config, ui_enabled=False)
                proxy = _start_managed_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}")
                try:
                    for port in (core.port, proxy.port):
                        job_started.clear()
                        cancellation_observed.clear()
                        cancel_hook_called.clear()
                        allow_terminal_persistence.clear()
                        status, _headers, body = _request(
                            port,
                            "/api/auth/login",
                            method="POST",
                            body=_json_bytes({"username": "admin", "password": "admin"}),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(status, 200, body)
                        start_bearer = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}
                        status, _headers, body = _request(
                            port,
                            "/api/core/strategy-discovery/start-run",
                            method="POST",
                            body=_json_bytes({"mode": "multi_domain", "domains": ["example.test"], "protocols": ["tcp"]}),
                            headers={**start_bearer, "Content-Type": "application/json"},
                        )
                        self.assertEqual(status, 202, body)
                        run_id = str(json.loads(body)["run_id"])
                        self.assertTrue(run_id)
                        self.assertTrue(job_started.wait(timeout=_PROCESS_TIMEOUT_SECONDS))

                        context = multiprocessing.get_context("spawn")
                        entered = context.Event()
                        release = context.Event()
                        holder_errors = context.Queue()
                        holder = context.Process(
                            target=_hold_sqlite_immediate_transaction_in_process,
                            args=(str(config.output.state_dir), entered, release, holder_errors),
                            name="sqlite-auth-write-lock-holder",
                        )
                        holder.start()
                        try:
                            self.assertTrue(entered.wait(timeout=_PROCESS_TIMEOUT_SECONDS))
                            for headers in ({}, {"Authorization": "Bearer malformed-token"}):
                                status, _headers, body = _request(
                                    port,
                                    "/api/core/strategy-discovery/current-run-progress",
                                    headers=headers,
                                )
                                self.assertEqual(status, 401, body)

                            status, _headers, body = _request(
                                port,
                                "/api/auth/login",
                                method="POST",
                                body=_json_bytes({"username": "admin", "password": "admin"}),
                                headers={"Content-Type": "application/json"},
                            )
                            self.assertEqual(status, 200, body)
                            locked_bearer = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}
                            status, _headers, body = _request(
                                port,
                                "/api/core/strategy-discovery/stop-current-run",
                                method="POST",
                                body=_json_bytes({}),
                                headers={**locked_bearer, "Content-Type": "application/json"},
                            )
                            self.assertEqual(status, 202, body)
                            self.assertEqual(json.loads(body), {"accepted": True, "run_id": run_id, "status": "stopping"})
                            self.assertTrue(cancel_hook_called.wait(timeout=_PROCESS_TIMEOUT_SECONDS))
                        finally:
                            release.set()
                            _join_process(self, holder)
                            allow_terminal_persistence.set()
                        try:
                            holder_error = holder_errors.get_nowait()
                        except queue.Empty:
                            holder_error = None
                        self.assertIsNone(holder_error, holder_error)
                        self.assertTrue(cancellation_observed.wait(timeout=_PROCESS_TIMEOUT_SECONDS))
                        _wait_for_stopped_run(self, core.port, locked_bearer, run_id)
                finally:
                    proxy.close()
                    core.close()


    def test_live_candidate_writer_does_not_block_core_or_web_authorization(self) -> None:
        """A real candidate batch may be open while both API roles stay usable."""
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            job_started = threading.Event()
            writer_entered = threading.Event()
            release_writer = threading.Event()
            discovery_finished = threading.Event()
            recorder_holder: list[_LiveStdoutRecorder] = []

            def controlled_discovery(_config: AppConfig, payload: dict[str, Any], stop: Any, run_id: str) -> dict[str, str]:
                logs = _config.output.state_dir / "strategy-finder" / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                stdout_log = logs / f"{run_id}.stdout.log"
                stdout_log.write_text("candidate writer is live\n", encoding="utf-8")
                append_run(
                    _config.output.state_dir,
                    {
                        "id": run_id,
                        "kind": "multi-domain-discovery",
                        "status": "running",
                        "timestamp": "test-running",
                        "started_at": "test-started",
                        "domains": list(payload.get("domains") or []),
                        "stdout_log": str(stdout_log),
                    },
                )
                recorder = _LiveStdoutRecorder(
                    _config.output.state_dir,
                    {"id": run_id, "kind": "standard-discovery", "status": "running", "domains": list(payload.get("domains") or [])},
                )
                recorder_holder.append(recorder)
                recorder.record_line("* script : standard/10-test.sh")
                recorder.record_line("- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=live")
                recorder.record_line("!!!!! AVAILABLE !!!!!")
                job_started.set()
                if not stop.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
                    raise TimeoutError("test stop endpoint did not cancel the active job")
                if not release_writer.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
                    raise TimeoutError("test did not release the candidate writer")
                recorder.close()
                append_run(
                    _config.output.state_dir,
                    {
                        "id": run_id,
                        "kind": "multi-domain-discovery",
                        "status": "stopped",
                        "timestamp": "test-stopped",
                        "started_at": "test-started",
                        "completed_at": "test-stopped",
                        "domains": list(payload.get("domains") or []),
                        "stdout_log": str(stdout_log),
                    },
                )
                discovery_finished.set()
                return {"status": "stopped"}

            def hold_real_candidate_write(conn: Any, **event: Any) -> None:
                storage_upsert_candidate_event_conn(conn, **event)
                writer_entered.set()
                if not release_writer.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
                    raise TimeoutError("test did not release the candidate writer")

            with mock.patch(
                "gp_control_plane.web.api_server._job_zapret_multi_domain_discovery",
                side_effect=controlled_discovery,
            ), mock.patch(
                "gp_control_plane.strategy_finder.upsert_candidate_event_conn",
                side_effect=hold_real_candidate_write,
            ), mock.patch(
                "gp_control_plane.strategy_finder.LIVE_CANDIDATE_FLUSH_SIZE",
                1,
            ), mock.patch(
                "gp_control_plane.web.api_server.create_post_run_snapshot",
                return_value={"kind": "snapshot", "status": "success", "snapshot_id": "test-snapshot", "completed_at": "test-stopped"},
            ):
                core = _start_managed_server(serve, config, ui_enabled=False)
                proxy = _start_managed_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}")
                try:
                    status, _headers, body = _request(
                        core.port,
                        "/api/auth/login",
                        method="POST",
                        body=_json_bytes({"username": "admin", "password": "admin"}),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 200, body)
                    headers = {"Authorization": f"Bearer {json.loads(body)['access_token']}", "Content-Type": "application/json"}
                    status, _headers, body = _request(
                        core.port,
                        "/api/core/strategy-discovery/start-run",
                        method="POST",
                        body=_json_bytes({"mode": "multi_domain", "domains": ["youtube.com"], "protocols": ["tcp"]}),
                        headers=headers,
                    )
                    self.assertEqual(status, 202, body)
                    run_id = str(json.loads(body)["run_id"])
                    self.assertTrue(job_started.wait(timeout=_PROCESS_TIMEOUT_SECONDS))
                    self.assertTrue(writer_entered.wait(timeout=_PROCESS_TIMEOUT_SECONDS))

                    for port in (core.port, proxy.port):
                        status, _headers, body = _request(
                            port,
                            "/api/auth/login",
                            method="POST",
                            body=_json_bytes({"username": "admin", "password": "admin"}),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(status, 200, body)
                        bearer = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}
                        status, _headers, body = _request(port, "/api/core/status", headers=bearer)
                        self.assertEqual(status, 200, body)
                        self.assertEqual(json.loads(body)["current_run"], {"run_id": run_id, "status": "running"})
                        status, _headers, body = _request(port, "/api/core/runs/history", headers=bearer)
                        self.assertEqual(status, 200, body)
                        self.assertTrue(any(item.get("run_id") == run_id for item in json.loads(body)["runs"]))
                        status, _headers, body = _request(
                            port, "/api/core/strategy-discovery/current-run-latest-log", headers=bearer
                        )
                        self.assertEqual(status, 200, body)
                        log = json.loads(body)
                        self.assertEqual(log["run_id"], run_id)
                        self.assertTrue(log["stdout_tail"])

                    release_writer.set()
                    status, _headers, body = _request(
                        proxy.port,
                        "/api/core/strategy-discovery/stop-current-run",
                        method="POST",
                        body=_json_bytes({}),
                        headers=headers,
                    )
                    self.assertEqual(status, 202, body)
                    self.assertEqual(json.loads(body), {"accepted": True, "run_id": run_id, "status": "stopping"})
                    release_writer.set()
                    self.assertTrue(discovery_finished.wait(timeout=_PROCESS_TIMEOUT_SECONDS))
                    _wait_for_stopped_run(self, core.port, headers, run_id)
                finally:
                    release_writer.set()
                    for recorder in recorder_holder:
                        recorder.close()
                    proxy.close()
                    core.close()


    def test_password_rotation_revokes_open_sse_streams(self) -> None:
        for topology in ("core", "proxy"):
            with self.subTest(topology=topology), tempfile.TemporaryDirectory() as raw:
                config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
                servers: list[_ManagedServer] = []
                connection = response = new_connection = new_response = None
                try:
                    core = _start_managed_server(serve, config, ui_enabled=False)
                    servers.append(core)
                    if topology == "core":
                        port = core.port
                    else:
                        proxy = _start_managed_server(
                            serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}"
                        )
                        servers.append(proxy)
                        port = proxy.port

                    status, _headers, body = _request(
                        port, "/api/auth/login", method="POST",
                        body=_json_bytes({"username": "admin", "password": "admin"}),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 200)
                    old_bearer = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}

                    stream_path = "/api/internal/web-events/stream" if topology == "core" else "/api/web/events/stream"
                    connection, response = _open_sse(port, old_bearer, path=stream_path)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
                    status, _headers, body = _request(
                        port, "/api/auth/change-password", method="POST",
                        body=_json_bytes({"current_password": "admin", "new_password": "newpass8"}),
                        headers={**old_bearer, "Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 200)
                    new_token = json.loads(body)["access_token"]
                    status, _headers, _body = _request(port, stream_path, headers=old_bearer)
                    self.assertEqual(status, 401)
                    self.assertIsInstance(response.read(), bytes)
                    self.assertTrue(response.isclosed())

                    new_connection, new_response = _open_sse(port, {"Authorization": f"Bearer {new_token}"}, path=stream_path)
                    self.assertEqual(new_response.status, 200)
                finally:
                    _close_sse(new_connection, new_response)
                    _close_sse(connection, response)
                    for server in reversed(servers):
                        server.close()


    def test_sse_terminal_eof_after_revocation_does_not_require_a_fast_heartbeat(self) -> None:
        """A4-RV-01: terminal EOF follows auth recheck, not a shortened heartbeat."""
        from gp_control_plane.web import api_server

        self.assertEqual(api_server._SSE_HEARTBEAT_SECONDS, 15.0)
        for topology in ("core", "proxy"):
            with self.subTest(topology=topology), tempfile.TemporaryDirectory() as raw:
                config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
                servers: list[_ManagedServer] = []
                connection = response = None
                sleep_entered = threading.Event()
                release_next_loop = threading.Event()
                sleep_delays: list[float] = []

                class ControlledTerminal:
                    def __init__(self):
                        self.event=threading.Event()
                    def set(self):self.event.set()
                    def is_set(self):return self.event.is_set()
                    def wait(self,delay: float):
                        controlled_wait(delay)
                        return self.event.is_set()

                def controlled_wait(delay: float) -> None:
                    sleep_delays.append(delay)
                    sleep_entered.set()
                    if not release_next_loop.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
                        raise TimeoutError("test did not release the next SSE authorization check")

                try:
                    core = _start_managed_server(serve, config, ui_enabled=False)
                    servers.append(core)
                    port = core.port
                    if topology == "proxy":
                        proxy = _start_managed_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}")
                        servers.append(proxy)
                        port = proxy.port

                    status, _headers, body = _request(
                        port,
                        "/api/auth/login",
                        method="POST",
                        body=_json_bytes({"username": "admin", "password": "admin"}),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 200, body)
                    old_bearer = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}

                    # SSE-LIFETIME-FIX replaced non-interruptible sleep with a
                    # listener-owned terminal Event. Instrument only that Core
                    # module's Event factory, never global time/threading APIs.
                    from gp_control_plane.http import core_app
                    controlled_threading=mock.Mock(wraps=threading)
                    controlled_threading.Event.side_effect=ControlledTerminal
                    with mock.patch.object(core_app,"threading",controlled_threading):
                        stream_path = "/api/internal/web-events/stream" if topology == "core" else "/api/web/events/stream"
                        connection, response = _open_sse(port, old_bearer, path=stream_path)
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.readline(), b"event: status\n")
                        self.assertTrue(response.readline().startswith(b"data: "))
                        self.assertEqual(response.readline(), b"\n")
                        for _ in range(20):
                            line = response.readline()
                            if line == b": keepalive\n":
                                self.assertEqual(response.readline(), b"\n")
                                break
                        else:
                            self.fail("initial SSE heartbeat did not arrive before the controlled loop sleep")
                        self.assertTrue(sleep_entered.wait(timeout=_PROCESS_TIMEOUT_SECONDS))

                        status, _headers, body = _request(
                            port,
                            "/api/auth/change-password",
                            method="POST",
                            body=_json_bytes({"current_password": "admin", "new_password": "newpass8"}),
                            headers={**old_bearer, "Content-Type": "application/json"},
                        )
                        self.assertEqual(status, 200, body)
                        release_next_loop.set()
                        self.assertEqual(response.read(), b"")
                        self.assertTrue(response.isclosed())

                    self.assertEqual(sleep_delays, [1])
                finally:
                    release_next_loop.set()
                    _close_sse(connection, response)
                    for server in reversed(servers):
                        server.close()


    def test_storage_unavailable_is_a_normalized_503_from_core_and_proxy(self) -> None:
        for error in (
            StorageUnavailableError("storage is temporarily unavailable"),
            sqlite3.OperationalError("database is locked"),
            sqlite3.OperationalError("database is busy"),
            sqlite3.OperationalError("disk i/o error"),
        ):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as raw:
                config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
                core = _start_managed_server(serve, config, ui_enabled=False)
                proxy = _start_managed_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}")
                try:
                    status, _headers, body = _request(
                        core.port,
                        "/api/auth/login",
                        method="POST",
                        body=_json_bytes({"username": "admin", "password": "admin"}),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 200)
                    headers = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}
                    with mock.patch("gp_control_plane.core_api.read_runs", side_effect=error):
                        for port in (core.port, proxy.port):
                            status, _headers, body = _request(port, "/api/core/runs/history", headers=headers)
                            self.assertEqual(status, 503, body)
                            self.assertEqual(json.loads(body)["error"]["code"], "storage_unavailable")
                    self.assertEqual(_request(core.port, "/api/health")[0], 200)
                finally:
                    proxy.close()
                    core.close()


    def test_core_and_proxy_auth_storage_diagnostics_keep_public_503_generic(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            core = _start_managed_server(serve, config, ui_enabled=False)
            proxy = _start_managed_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}")
            try:
                status, _headers, body = _request(
                    core.port,
                    "/api/auth/login",
                    method="POST",
                    body=_json_bytes({"username": "admin", "password": "admin"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, body)
                headers = {"Authorization": f"Bearer {json.loads(body)['access_token']}?must-not-log"}
                expected = {"error": {"code": "storage_unavailable", "message": "Storage is temporarily unavailable.", "details": {}}}
                for port in (core.port, proxy.port):
                    error = sqlite3.OperationalError("database is locked: /private/path SELECT secret")
                    error.sqlite_errorcode = 517
                    error.sqlite_errorname = "SQLITE_BUSY_SNAPSHOT"
                    with mock.patch(
                        "gp_control_plane.web.api_server.require_bearer_token", side_effect=error
                    ), self.assertLogs("gp_control_plane.web.api_server", level="WARNING") as logged:
                        status, _response_headers, response_body = _request(port, "/api/core/status?secret=query", headers=headers)
                    self.assertEqual(status, 503, response_body)
                    self.assertEqual(json.loads(response_body), expected)
                    record = "\n".join(logged.output)
                    self.assertIn("operation=GET", record)
                    self.assertIn("route=/api/core/status", record)
                    self.assertIn("sqlite_primary_code=5", record)
                    self.assertIn("sqlite_extended_code=517", record)
                    self.assertIn("sqlite_errorname=SQLITE_BUSY_SNAPSHOT", record)
                    self.assertIn("exception_type=OperationalError", record)
                    self.assertNotIn("secret=query", record)
                    self.assertNotIn("must-not-log", record)
                    self.assertNotIn("/private/path", record)
            finally:
                proxy.close()
                core.close()


    def test_core_and_proxy_auth_state_permission_error_is_a_generic_503(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            core = _start_managed_server(serve, config, ui_enabled=False)
            proxy = _start_managed_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}")
            try:
                status, _headers, body = _request(
                    core.port,
                    "/api/auth/login",
                    method="POST",
                    body=_json_bytes({"username": "admin", "password": "admin"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, body)
                headers = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}
                with mock.patch(
                    "gp_control_plane.web.api_server.require_bearer_token",
                    side_effect=PermissionError("owned state path is temporarily inaccessible"),
                ):
                    for port in (core.port, proxy.port):
                        status, _response_headers, response_body = _request(port, "/api/core/status", headers=headers)
                        self.assertEqual(status, 503, response_body)
                        self.assertEqual(json.loads(response_body)["error"]["code"], "storage_unavailable")
                self.assertEqual(_request(core.port, "/api/health")[0], 200)
            finally:
                proxy.close()
                core.close()


    def test_non_transient_sqlite_operational_error_is_not_a_storage_unavailable_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            core = _start_managed_server(serve, config, ui_enabled=False)
            proxy = _start_managed_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}")
            try:
                status, _headers, body = _request(
                    core.port,
                    "/api/auth/login",
                    method="POST",
                    body=_json_bytes({"username": "admin", "password": "admin"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 200)
                headers = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}
                error = sqlite3.OperationalError("no such table: runs")
                # A6's WSGI listener reports an unexpected Core exception as
                # an honest 500 response instead of BaseHTTP's socket drop.
                # It must still never be mistaken for storage-unavailable or
                # a successful proxy payload.
                with mock.patch("gp_control_plane.core_api.read_runs", side_effect=error):
                    core_status, _core_headers, core_body = _request(core.port, "/api/core/runs/history", headers=headers)
                    proxy_status, _proxy_headers, proxy_body = _request(
                        proxy.port, "/api/core/runs/history", headers=headers
                    )

                self.assertEqual(core_status, 500, core_body)
                self.assertEqual(proxy_status, 500, proxy_body)
                self.assertNotEqual(proxy_status, 503)
                self.assertNotEqual(proxy_status, 200)
            finally:
                proxy.close()
                core.close()


    def test_proxy_authorization_maps_storage_unavailable_to_503(self) -> None:
        for error in (
            StorageUnavailableError("storage is temporarily unavailable"),
            sqlite3.OperationalError("database is locked"),
        ):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as raw:
                config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
                core = _start_managed_server(serve, config, ui_enabled=False)
                proxy = _start_managed_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}")
                try:
                    with mock.patch(
                        "gp_control_plane.web.api_server.require_bearer_token",
                        side_effect=error,
                    ):
                        status, _headers, body = _request(proxy.port, "/api/core/runs/history")
                    self.assertEqual(status, 503, body)
                    self.assertEqual(json.loads(body)["error"]["code"], "storage_unavailable")
                finally:
                    proxy.close()
                    core.close()


    def test_proxy_web_json_get_maps_storage_unavailable_to_503(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            core = _start_managed_server(serve, config, ui_enabled=False)
            proxy = _start_managed_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}")
            try:
                status, _headers, body = _request(
                    proxy.port,
                    "/api/auth/login",
                    method="POST",
                    body=_json_bytes({"username": "admin", "password": "admin"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 200)
                headers = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}
                with mock.patch(
                    "gp_control_plane.application.web_views.get_payload",
                    side_effect=StorageUnavailableError("storage is temporarily unavailable"),
                ):
                    status, response_headers, body = _request(proxy.port, "/api/web/run-preferences", headers=headers)

                self.assertEqual(status, 503, body)
                self.assertEqual(response_headers["content-type"], "application/json; charset=utf-8")
                self.assertEqual(json.loads(body)["error"]["code"], "storage_unavailable")
            finally:
                proxy.close()
                core.close()


    def test_proxy_web_json_post_maps_storage_unavailable_to_503(self) -> None:
        for error in (
            StorageUnavailableError("storage is temporarily unavailable"),
            sqlite3.OperationalError("database is locked"),
        ):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as raw:
                config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
                core = _start_managed_server(serve, config, ui_enabled=False)
                proxy = _start_managed_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}")
                try:
                    status, _headers, body = _request(
                        proxy.port,
                        "/api/auth/login",
                        method="POST",
                        body=_json_bytes({"username": "admin", "password": "admin"}),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 200)
                    headers = {
                        "Authorization": f"Bearer {json.loads(body)['access_token']}",
                        "Content-Type": "application/json",
                    }
                    with mock.patch(
                        "gp_control_plane.application.web_views.post_response",
                        side_effect=error,
                    ):
                        status, response_headers, body = _request(
                            proxy.port,
                            "/api/web/run-preferences",
                            method="POST",
                            body=_json_bytes({"run_preferences": {}}),
                            headers=headers,
                        )

                    self.assertEqual(status, 503, body)
                    self.assertEqual(response_headers["content-type"], "application/json; charset=utf-8")
                    self.assertEqual(json.loads(body)["error"]["code"], "storage_unavailable")
                finally:
                    proxy.close()
                    core.close()


    def test_proxy_rejects_pre_rotation_bearer_during_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            core = _start_managed_server(serve, config, ui_enabled=False)
            proxy = _start_managed_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}")
            try:
                status, _headers, body = _request(
                    proxy.port,
                    "/api/auth/login",
                    method="POST",
                    body=_json_bytes({"username": "admin", "password": "admin"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 200)
                old_bearer = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}
                self.assertEqual(_request(proxy.port, "/api/core/runs/history", headers=old_bearer)[0], 200)

                status, _headers, body = _request(
                    core.port,
                    "/api/auth/change-password",
                    method="POST",
                    body=_json_bytes({"current_password": "admin", "new_password": "newpass8"}),
                    headers={**old_bearer, "Content-Type": "application/json"},
                )
                self.assertEqual(status, 200)

                context = multiprocessing.get_context("spawn")
                entered = context.Event()
                release = context.Event()
                holder_errors = context.Queue()
                holder = context.Process(
                    target=_hold_sqlite_immediate_transaction_in_process,
                    args=(str(config.output.state_dir), entered, release, holder_errors),
                    name="proxy-stale-token-write-lock-holder",
                )
                holder.start()
                try:
                    self.assertTrue(entered.wait(timeout=_PROCESS_TIMEOUT_SECONDS))
                    status, _headers, body = _request(proxy.port, "/api/core/runs/history", headers=old_bearer)
                    self.assertEqual(status, 401, body)
                    self.assertEqual(json.loads(body)["error"]["code"], "authentication_required")
                finally:
                    release.set()
                    _join_process(self, holder)
                try:
                    holder_error = holder_errors.get_nowait()
                except queue.Empty:
                    holder_error = None
                self.assertIsNone(holder_error, holder_error)
                self.assertEqual(_request(proxy.port, "/api/core/runs/history", headers=old_bearer)[0], 401)
            finally:
                proxy.close()
                core.close()


    def test_proxy_sse_storage_failure_after_headers_emits_one_normalized_event_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            core = _start_managed_server(serve, config, ui_enabled=False)
            proxy = _start_managed_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}")
            connection = response = None
            try:
                status, _headers, body = _request(
                    proxy.port,
                    "/api/auth/login",
                    method="POST",
                    body=_json_bytes({"username": "admin", "password": "admin"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 200)
                headers = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}

                with mock.patch(
                    "gp_control_plane.web.api_server._event_payloads",
                    side_effect=[{"status": {"state": "ready"}}, _chained_storage_unavailable_517()],
                ), mock.patch("gp_control_plane.web.api_server.time.sleep", return_value=None), self.assertLogs(
                    "gp_control_plane.web.api_server", level="WARNING"
                ) as logged:
                    connection, response = _open_sse(proxy.port, headers)
                    stream = response.read()

                self.assertEqual(response.status, 200)
                frames = [frame for frame in stream.decode("utf-8").split("\n\n") if frame]
                error_frames = [frame for frame in frames if frame.startswith("event: event-error\n")]
                self.assertEqual(len(error_frames), 1, stream)
                error_data = json.loads(error_frames[0].split("data: ", 1)[1])
                self.assertEqual(error_data["error"], "storage_unavailable")
                self.assertNotIn("disk i/o error", stream.decode("utf-8"))
                self.assertNotIn("HTTP/", stream.decode("utf-8"))
                record = "\n".join(logged.output)
                self.assertIn("operation=GET", record)
                self.assertIn("route=/api/internal/web-events/stream", record)
                self.assertIn("sqlite_primary_code=5", record)
                self.assertIn("sqlite_extended_code=517", record)
                self.assertIn("sqlite_errorname=SQLITE_BUSY_SNAPSHOT", record)
                self.assertNotIn("/private/path", record)
            finally:
                _close_sse(connection, response)
                proxy.close()
                core.close()
