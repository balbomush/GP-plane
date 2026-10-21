from tests.support.auth_http import *
from tests.support import auth_http as support
globals().update({key:value for key,value in vars(support).items() if key.startswith("_") and not key.startswith("__")})

class BearerAuthHttpTests(OwnedHttpCase):
    def test_clean_install_vault_http_rejects_noncanonical_ids_before_restore(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=root / "state"))
            vault = root / "install-user" / ".local" / "share" / "gp-control-plane" / "clean-install-vault"
            vault.parents[3].mkdir()
            handoff = vault / "handoff.json"
            valid_vault_id = "a" * 32
            with (
                mock.patch("gp_control_plane.backups.clean_install_vault_dir", return_value=vault),
                mock.patch("gp_control_plane.backups.clean_install_handoff_path", return_value=handoff),
                mock.patch("gp_control_plane.core_api.restore_clean_install_vault") as restore_payload,
            ):
                port = _start_server(serve, config, ui_enabled=False)
                login_status, _headers, login_body = _request(
                    port,
                    "/api/auth/login",
                    method="POST",
                    body=_json_bytes({"username": "admin", "password": "admin"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(login_status, 200)
                bearer = {"Authorization": f"Bearer {json.loads(login_body)['access_token']}", "Content-Type": "application/json"}

                invalid_status_values = (
                    f"%20{valid_vault_id}",
                    valid_vault_id.upper(),
                    valid_vault_id[:-1],
                )
                for value in invalid_status_values:
                    with self.subTest(endpoint="status", vault_id=value):
                        status, _headers, body = _request(
                            port,
                            f"/api/core/clean-install-vaults/status?vault_id={value}",
                            headers={"Authorization": bearer["Authorization"]},
                        )
                        self.assertEqual(status, 400, body.decode("utf-8"))
                        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")

                repeated_status, _headers, repeated_body = _request(
                    port,
                    f"/api/core/clean-install-vaults/status?vault_id={valid_vault_id}&vault_id={valid_vault_id}",
                    headers={"Authorization": bearer["Authorization"]},
                )
                self.assertEqual(repeated_status, 400, repeated_body.decode("utf-8"))
                self.assertEqual(json.loads(repeated_body)["error"]["code"], "invalid_request")

                for raw_vault_id in (f" {valid_vault_id}", valid_vault_id.upper(), valid_vault_id[:-1], [valid_vault_id]):
                    with self.subTest(endpoint="restore", vault_id=repr(raw_vault_id)):
                        status, _headers, body = _request(
                            port,
                            "/api/core/clean-install-vaults/restore",
                            method="POST",
                            body=_json_bytes({"vault_id": raw_vault_id}),
                            headers=bearer,
                        )
                        self.assertEqual(status, 400, body.decode("utf-8"))
                        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")
                self.assertFalse(restore_payload.called, "strict HTTP validation must reject raw noncanonical vault_id values")


    def test_interrupted_cleanup_http_contract_exposes_only_complete_public_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            target_state = root / "target-state"
            created = create_clean_install_vault(source_state, target_home=home)
            vault = clean_install_vault_dir(home)
            entry = vault / "entry.json"
            original_unlink = Path.unlink

            def interrupt_entry_unlink(path: Path, *args: object, **kwargs: object) -> None:
                if path == entry:
                    raise OSError("simulated interrupted entry cleanup")
                original_unlink(path, *args, **kwargs)

            with self.assertRaisesRegex(OSError, "interrupted entry cleanup"):
                with mock.patch.object(Path, "unlink", interrupt_entry_unlink):
                    restore_clean_install_vault(target_state, target_home=home, vault_id=created["vault_id"])
            self.assertTrue((vault / "archive.zip").is_file())
            self.assertTrue(entry.is_file())
            self.assertTrue(clean_install_handoff_path(home).is_file())


    def test_completed_clean_install_vault_is_not_exposed_as_pending_api_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=root / "state"))
            vault = root / "install-user" / ".local" / "share" / "gp-control-plane" / "clean-install-vault"
            vault.parents[3].mkdir()
            handoff = vault / "handoff.json"
            with (
                mock.patch("gp_control_plane.backups.clean_install_vault_dir", return_value=vault),
                mock.patch("gp_control_plane.backups.clean_install_handoff_path", return_value=handoff),
            ):
                port = _start_server(serve, config, ui_enabled=False)
                login_status, _headers, login_body = _request(
                    port,
                    "/api/auth/login",
                    method="POST",
                    body=_json_bytes({"username": "admin", "password": "admin"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(login_status, 200)
                bearer = {"Authorization": f"Bearer {json.loads(login_body)['access_token']}", "Content-Type": "application/json"}

                create_status, _headers, create_body = _request(
                    port,
                    "/api/core/clean-install-vaults/create",
                    method="POST",
                    body=b"{}",
                    headers=bearer,
                )
                self.assertEqual(create_status, 201, create_body.decode("utf-8"))
                created = json.loads(create_body)
                self.assertEqual(set(created), {"vault_id", "archive_sha256", "archive_size_bytes", "schema_version", "semantic_manifest"})
                self.assertNotIn("handoff_secret", create_body.decode("utf-8"))
                self.assertNotIn("confirmation_token", create_body.decode("utf-8"))

                restore_status, _headers, restore_body = _request(
                    port,
                    "/api/core/clean-install-vaults/restore",
                    method="POST",
                    body=_json_bytes({"vault_id": created["vault_id"], "confirm_restore": True}),
                    headers=bearer,
                )
                self.assertEqual(restore_status, 200, restore_body.decode("utf-8"))
                self.assertTrue(json.loads(restore_body)["completed"])

                list_status, _headers, list_body = _request(
                    port, "/api/core/clean-install-vaults/list", headers={"Authorization": bearer["Authorization"]}
                )
                self.assertEqual(list_status, 200)
                listed = json.loads(list_body)
                self.assertEqual(listed, {"vaults": []})
                self.assertNotIn("handoff_secret", list_body.decode("utf-8"))

                status_status, _headers, status_body = _request(
                    port,
                    f"/api/core/clean-install-vaults/status?vault_id={created['vault_id']}",
                    headers={"Authorization": bearer["Authorization"]},
                )
                self.assertEqual(status_status, 404)
                self.assertIn("error", json.loads(status_body))


    def test_strategy_candidate_export_handles_storage_errors_before_and_after_ndjson_headers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            core = _start_managed_server(serve, config, ui_enabled=False)
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

                def unavailable_before_first_line(*_args: object, **_kwargs: object) -> object:
                    raise sqlite3.OperationalError("disk i/o error")
                    yield b""  # pragma: no cover - makes this a generator

                with mock.patch(
                    "gp_control_plane.core_api.iter_strategy_candidates_export_lines",
                    side_effect=unavailable_before_first_line,
                ):
                    status, response_headers, body = _request(
                        core.port, "/api/core/strategy-candidates/export", headers=headers
                    )
                self.assertEqual(status, 503, body)
                self.assertEqual(response_headers["content-type"], "application/json; charset=utf-8")
                self.assertEqual(json.loads(body)["error"]["code"], "storage_unavailable")

                def unavailable_after_first_line(*_args: object, **_kwargs: object) -> object:
                    yield b'{"id":"first"}\n'
                    raise _chained_storage_unavailable_517()

                with mock.patch(
                    "gp_control_plane.core_api.iter_strategy_candidates_export_lines",
                    side_effect=unavailable_after_first_line,
                ), self.assertLogs("gp_control_plane.web.api_server", level="WARNING") as logged:
                    status, response_headers, body = _request(
                        core.port, "/api/core/strategy-candidates/export", headers=headers
                    )
                self.assertEqual(status, 200, body)
                self.assertEqual(response_headers["content-type"], "application/x-ndjson; charset=utf-8")
                self.assertEqual(body, b'{"id":"first"}\n')
                self.assertNotIn(b"HTTP/", body)
                self.assertNotIn(b"disk i/o error", body)
                record = "\n".join(logged.output)
                self.assertIn("operation=GET", record)
                self.assertIn("route=/api/core/strategy-candidates/export", record)
                self.assertIn("sqlite_primary_code=5", record)
                self.assertIn("sqlite_extended_code=517", record)
                self.assertIn("sqlite_errorname=SQLITE_BUSY_SNAPSHOT", record)
                self.assertNotIn("/private/path", record)
            finally:
                core.close()


    def test_sse_storage_failure_after_headers_emits_one_normalized_event_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            core = _start_managed_server(serve, config, ui_enabled=False)
            connection = response = None
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

                with mock.patch(
                    "gp_control_plane.web.api_server._event_payloads",
                    side_effect=[{"status": {"state": "ready"}}, _chained_storage_unavailable_517()],
                ), mock.patch("gp_control_plane.web.api_server.time.sleep", return_value=None), self.assertLogs(
                    "gp_control_plane.web.api_server", level="WARNING"
                ) as logged:
                    connection, response = _open_sse(core.port, headers, path="/api/internal/web-events/stream")
                    stream = response.read()

                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
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
                core.close()


    def test_core_authorization_maps_storage_unavailable_to_503(self) -> None:
        for error in (
            StorageUnavailableError("storage is temporarily unavailable"),
            sqlite3.OperationalError("database is locked"),
        ):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as raw:
                config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
                core = _start_managed_server(serve, config, ui_enabled=False)
                try:
                    with mock.patch(
                        "gp_control_plane.web.api_server.require_bearer_token",
                        side_effect=error,
                    ):
                        status, _headers, body = _request(core.port, "/api/core/runs/history")
                    self.assertEqual(status, 503, body)
                    self.assertEqual(json.loads(body)["error"]["code"], "storage_unavailable")
                finally:
                    core.close()
