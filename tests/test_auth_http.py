from __future__ import annotations

import http.client
import json
import multiprocessing
import queue
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.backups import (  # noqa: E402
    clean_install_handoff_path,
    clean_install_vault_dir,
    create_clean_install_vault,
    restore_clean_install_vault,
)
from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.storage import StorageUnavailableError, append_run, db_path
from gp_control_plane.strategy_finder import _LiveStdoutRecorder, upsert_candidate_event_conn as storage_upsert_candidate_event_conn
from gp_control_plane.web.api_server import serve
from gp_control_plane_web.proxy import serve_web_proxy


_PROCESS_TIMEOUT_SECONDS = 15


def _chained_storage_unavailable_517() -> StorageUnavailableError:
    cause = sqlite3.OperationalError("database is locked: /private/path SELECT secret")
    cause.sqlite_errorcode = 517
    cause.sqlite_errorname = "SQLITE_BUSY_SNAPSHOT"
    error = StorageUnavailableError("storage is temporarily unavailable")
    error.__cause__ = cause
    return error


def _serve_core_in_process(
    state_dir_raw: str,
    port: int,
    ready: Any,
    stop: Any,
    errors: Any,
) -> None:
    """Serve a core role in a spawned interpreter, stopping through an Event."""
    import gp_control_plane.web.api_server as api_server

    config = AppConfig(output=OutputConfig(state_dir=Path(state_dir_raw)))
    try:
        from gp_control_plane.web.core_runtime import create_core_runtime

        runtime = create_core_runtime(config, "127.0.0.1", port, ui_enabled=False)
        thread = threading.Thread(target=runtime.serve_forever, name="auth-core-cheroot", daemon=True)
        thread.start()
        deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Core Cheroot child did not become ready")
                time.sleep(0.01)
        ready.set()
        if not stop.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
            raise TimeoutError("parent did not stop Core Cheroot child")
        runtime.stop()
        thread.join(timeout=5)
        if thread.is_alive():
            raise TimeoutError("Core Cheroot child did not stop")
    except BaseException as error:  # noqa: BLE001 - report child failures to the parent test
        errors.put(repr(error))
        ready.set()


def _hold_sqlite_immediate_transaction_in_process(
    state_dir_raw: str,
    entered: Any,
    release: Any,
    errors: Any,
) -> None:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path(Path(state_dir_raw)), timeout=0)
        conn.execute("BEGIN IMMEDIATE")
        entered.set()
        if not release.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
            raise TimeoutError("parent did not release the SQLite write lock")
        conn.rollback()
    except BaseException as error:  # noqa: BLE001 - report child failures to the parent test
        errors.put(repr(error))
    finally:
        if conn is not None:
            conn.close()


def _join_process(test: unittest.TestCase, process: multiprocessing.Process) -> None:
    process.join(timeout=_PROCESS_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        test.fail(f"child process {process.name} did not stop")
    test.assertEqual(process.exitcode, 0, f"child process {process.name} exited unexpectedly")


class BearerAuthHttpTests(unittest.TestCase):
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

    def test_monolith_public_allowlist_and_protected_transport_routes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            port = _start_server(serve, config)

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
                    core = _start_managed_server(serve, config, ui_enabled=topology == "core")
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

                    connection, response = _open_sse(port, old_bearer)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
                    status, _headers, body = _request(
                        port, "/api/auth/change-password", method="POST",
                        body=_json_bytes({"current_password": "admin", "new_password": "newpass8"}),
                        headers={**old_bearer, "Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 200)
                    new_token = json.loads(body)["access_token"]
                    status, _headers, _body = _request(port, "/api/web/events/stream", headers=old_bearer)
                    self.assertEqual(status, 401)
                    self.assertIsInstance(response.read(), bytes)
                    self.assertTrue(response.isclosed())

                    new_connection, new_response = _open_sse(port, {"Authorization": f"Bearer {new_token}"})
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

                def controlled_sleep(delay: float) -> None:
                    sleep_delays.append(delay)
                    sleep_entered.set()
                    if not release_next_loop.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
                        raise TimeoutError("test did not release the next SSE authorization check")

                try:
                    core = _start_managed_server(serve, config, ui_enabled=topology == "core")
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

                    with mock.patch.object(api_server.time, "sleep", side_effect=controlled_sleep):
                        connection, response = _open_sse(port, old_bearer)
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
            core = _start_managed_server(serve, config, ui_enabled=True)
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
                    connection, response = _open_sse(core.port, headers)
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
                self.assertIn("route=/api/web/events/stream", record)
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


class _ManagedServer:
    def __init__(self, port: int, server: Any, thread: threading.Thread) -> None:
        self.port = port
        self._server = server
        self._thread = thread

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise AssertionError(f"server thread did not stop on port {self.port}")


def _start_managed_server(function: Any, config: AppConfig, **kwargs: Any) -> _ManagedServer:
    port = _free_port()
    module = sys.modules[function.__module__]
    if function.__name__ == "serve_web_proxy" and "core_url" in kwargs:
        from gp_control_plane_web.proxy import create_web_runtime

        runtime = create_web_runtime(config, "127.0.0.1", port, **kwargs)
        startup_errors: list[BaseException] = []

        def run_cheroot() -> None:
            try:
                runtime.serve_forever()
            except BaseException as error:
                startup_errors.append(error)

        thread = threading.Thread(target=run_cheroot, daemon=True)
        thread.start()
        managed = _ManagedServer(port, runtime, thread)
        try:
            _wait_for_server(port)
        except BaseException:
            managed.close()
            raise
        if startup_errors:
            managed.close()
            raise AssertionError(f"Cheroot server on port {port} failed during startup") from startup_errors[0]
        return managed

    if function.__name__ in {"serve", "serve_core"} and function.__module__ in {
        "gp_control_plane.web.api_server",
        "gp_control_plane.web.core_server",
    }:
        from gp_control_plane.web.core_runtime import create_core_runtime

        runtime = create_core_runtime(
            config,
            "127.0.0.1",
            port,
            ui_enabled=bool(kwargs.get("ui_enabled", function.__name__ == "serve")),
        )
        startup_errors: list[BaseException] = []

        def run_cheroot() -> None:
            try:
                runtime.serve_forever()
            except BaseException as error:
                startup_errors.append(error)

        thread = threading.Thread(target=run_cheroot, daemon=True)
        thread.start()
        managed = _ManagedServer(port, runtime, thread)
        try:
            _wait_for_server(port)
        except BaseException:
            managed.close()
            raise
        if startup_errors:
            managed.close()
            raise AssertionError(f"Cheroot Core on port {port} failed during startup") from startup_errors[0]
        return managed

    server_type = getattr(module, "ThreadingHTTPServer")
    server_created = threading.Event()
    server_holder: dict[str, Any] = {}
    startup_errors: list[BaseException] = []

    class CapturingThreadingHTTPServer(server_type):
        def __init__(self, *args: Any, **server_kwargs: Any) -> None:
            super().__init__(*args, **server_kwargs)
            server_holder["server"] = self
            server_created.set()

    def run() -> None:
        try:
            function(config, "127.0.0.1", port, **kwargs)
        except BaseException as error:
            startup_errors.append(error)
            server_created.set()

    thread = threading.Thread(target=run, daemon=True)
    with mock.patch.object(module, "ThreadingHTTPServer", CapturingThreadingHTTPServer):
        thread.start()
        if not server_created.wait(timeout=5):
            raise AssertionError(f"server on port {port} did not construct")

    if startup_errors:
        raise AssertionError(f"server on port {port} failed during startup") from startup_errors[0]
    server = server_holder.get("server")
    if server is None:
        raise AssertionError(f"server on port {port} was not captured")
    managed = _ManagedServer(port, server, thread)
    try:
        _wait_for_server(port)
    except BaseException:
        managed.close()
        raise
    return managed


def _close_sse(
    connection: http.client.HTTPConnection | None, response: http.client.HTTPResponse | None
) -> None:
    try:
        if response is not None:
            response.close()
    finally:
        if connection is not None:
            connection.close()



def _start_server(function: Any, config: AppConfig, **kwargs: Any) -> int:
    port = _free_port()
    thread = threading.Thread(target=function, args=(config, "127.0.0.1", port), kwargs=kwargs, daemon=True)
    thread.start()
    _wait_for_server(port)
    return port


def _request(
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def _open_sse(port: int, headers: dict[str, str]) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", "/api/web/events/stream", headers=headers)
    return connection, connection.getresponse()


def _wait_for_server(port: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            status, _headers, _body = _request(port, "/api/health")
        except OSError:
            time.sleep(0.02)
            continue
        if status == 200:
            return
        time.sleep(0.02)
    raise AssertionError(f"server on port {port} did not become ready")


def _wait_for_stopped_run(
    test: unittest.TestCase, port: int, headers: dict[str, str], run_id: str
) -> None:
    """Wait for the real JobRunner cancellation path to reach idle/stopped."""
    deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status, _response_headers, body = _request(port, "/api/core/runs/history", headers=headers)
        if status == 200:
            history = json.loads(body).get("runs", [])
            stopped = next(
                (item for item in history if item.get("run_id") == run_id and item.get("status") == "stopped"),
                None,
            )
            status_code, _status_headers, status_body = _request(port, "/api/core/status", headers=headers)
            if stopped is not None and status_code == 200 and json.loads(status_body).get("state") == "idle":
                return
        time.sleep(0.02)
    test.fail(f"run {run_id} did not reach stopped/idle through the API")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
