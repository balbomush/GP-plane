from __future__ import annotations

import http.client
import ast
import contextlib
import errno
import io
import importlib
import json
import os
import re
import socket
import socketserver
import struct
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

if os.environ.get("GP_TEST_INSTALLED") != "1":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.config import AppConfig, InstallConfig, OutputConfig
from gp_control_plane.application import web_views
import gp_control_plane.jobs as jobs
from gp_control_plane.state import read_state, write_state
from gp_control_plane.storage import SCHEMA_VERSION, append_run, read_app_setting
import gp_control_plane.strategy_finder as strategy_finder
from gp_control_plane.strategy_finder import upsert_candidates
from gp_control_plane.http import api_server as web_app
from gp_control_plane.http import docs as web_docs
from gp_control_plane.http import routes as web_routes
from gp_control_plane.http.core_server import serve_core
serve = serve_core


def _json_object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate OpenAPI key: {key}")
        result[key] = value
    return result


LEGACY_UI_API_MARKERS = (
    "API_ENDPOINTS.legacy",
    "LEGACY_API_ALLOWLIST",
    "legacyEndpoint",
    "legacyUrl",
    "/api/web/bootstrap",
)

LEGACY_API_ROUTE_LITERALS = (
    "/api/status",
    "/api/settings",
    "/api/run-preferences",
    "/api/events",
    "/api/diagnostics",
    "/api/backups/create",
    "/api/backups/list",
    "/api/backups/restore",
    "/api/backups/restore-preview",
    "/api/backups/delete",
    "/api/backups/download",
    "/api/backups/upload",
    "/api/releases",
    "/api/releases/update",
    "/api/releases/update-plan",
    "/api/presets",
    "/api/presets/save",
    "/api/presets/delete-users-lists",
    "/api/presets/domains",
    "/api/presets/domain-enabled",
    "/api/domain-sources",
    "/api/domain-sources/v2fly/preview",
    "/api/domain-sources/v2fly/import",
    "/api/strategy-finder/latest-log",
)


class CoreApplicationHttpTests(unittest.TestCase):
    def assertNoLegacyUiApi(self, html: str) -> None:
        for marker in (*LEGACY_UI_API_MARKERS, *LEGACY_API_ROUTE_LITERALS):
            self.assertNotIn(marker, html)


    def assertApiError(self, payload: dict[str, Any], code: str) -> None:
        self.assertEqual(set(payload), {"error"})
        error = payload["error"]
        self.assertIsInstance(error, dict)
        self.assertEqual(error.get("code"), code)
        self.assertIsInstance(error.get("message"), str)
        self.assertEqual(error.get("details"), {})


    def test_core_status_reports_sqlite_storage_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))

            payload = web_app.core_api.status_payload(config)

            self.assertTrue(payload["storage"]["ready"])
            self.assertEqual(payload["storage"]["schema_version"], SCHEMA_VERSION)
            self.assertNotIn("last_snapshot", payload)

            expected_snapshot = {
                "kind": "snapshot",
                "status": "success",
                "completed_at": "2026-08-12T00:00:00Z",
                "snapshot_id": "snapshot-1",
                "snapshot": {"id": "snapshot-1"},
            }
            write_state(config.output.state_dir, {"last_snapshot": expected_snapshot})

            payload = web_app.core_api.status_payload(config)

            self.assertEqual(payload["last_snapshot"], expected_snapshot)


    def test_web_status_includes_zapret_diagnostics_for_the_system_card(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            expected = {"nfqws2_found": True, "blockcheck_found": True, "root_helper_ready": True}

            with mock.patch.object(web_views, "check_install_cached", return_value=expected):
                payload = web_app.web_json_get_payload(config, "/api/web/status", {})

            self.assertEqual(payload["zapret2"], expected)


    def test_core_status_uses_lightweight_storage_runtime_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.object(
                web_app.core_api,
                "storage_runtime_status",
                return_value={
                    "ready": True,
                    "schema_version": str(SCHEMA_VERSION),
                    "expected_schema_version": str(SCHEMA_VERSION),
                    "integrity_check": "not_checked",
                },
            ) as storage_status:
                payload = web_app.core_api.status_payload(config)

            storage_status.assert_called_once_with(config.output.state_dir)
            self.assertTrue(payload["storage"]["ready"])
            self.assertEqual(payload["storage"]["schema_version"], SCHEMA_VERSION)


    def test_clean_install_vault_api_never_exposes_known_local_secret_and_restores_by_id_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            created = {
                "vault_id": "a" * 32,
                "handoff_secret": "SAFE-HANDOFF-001-KNOWN-SECRET",
                "archive_sha256": "b" * 64,
                "archive_size_bytes": 12,
                "schema_version": "7",
                "semantic_manifest": {"history_count": 1},
            }
            info = {
                "exists": True,
                "pending": True,
                "vault_id": "a" * 32,
                "created_at": "2026-08-21T12:00:00Z",
                "schema_version": "7",
                "archive_sha256": "b" * 64,
                "archive_size_bytes": 12,
                "verification": "pending",
            }
            restored = {
                "completed": True,
                "vault_id": "a" * 32,
                "verification": {"verified": True, "storage": {"ready": True, "integrity_check": "ok"}},
                "storage_status": {"ready": True, "integrity_check": "ok"},
                "cleanup": {"completed": True, "source_deleted": True},
            }
            with (
                mock.patch.object(web_app.core_api, "create_clean_install_vault", return_value=created),
                mock.patch.object(web_app.core_api, "clean_install_vault_info", return_value=info),
                mock.patch.object(web_app.core_api, "restore_clean_install_vault", return_value=restored) as restore,
            ):
                server = _start_captured_server(serve, config)
                with server:
                    status, _headers, body = _http_request(
                        server.port,
                        "/api/core/clean-install-vaults/create",
                        method="POST",
                        body=b"{}",
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 201, body)
                    create_payload = json.loads(body)
                    self.assertEqual(
                        set(create_payload),
                        {"vault_id", "archive_sha256", "archive_size_bytes", "schema_version", "semantic_manifest"},
                    )
                    self.assertNotIn("SAFE-HANDOFF-001-KNOWN-SECRET", body.decode("utf-8"))

                    status, _headers, body = _http_request(server.port, "/api/core/clean-install-vaults/list")
                    self.assertEqual(status, 200, body)
                    self.assertNotIn("SAFE-HANDOFF-001-KNOWN-SECRET", body.decode("utf-8"))

                    status, _headers, body = _http_request(
                        server.port, "/api/core/clean-install-vaults/status?vault_id=" + ("a" * 32)
                    )
                    self.assertEqual(status, 200, body)
                    self.assertNotIn("SAFE-HANDOFF-001-KNOWN-SECRET", body.decode("utf-8"))

                    status, _headers, body = _http_request(
                        server.port,
                        "/api/core/clean-install-vaults/restore",
                        method="POST",
                        body=json.dumps({"vault_id": "a" * 32, "confirm_restore": True}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 200, body)
                    self.assertNotIn("SAFE-HANDOFF-001-KNOWN-SECRET", body.decode("utf-8"))
                    self.assertTrue(json.loads(body)["completed"])
                    restore.assert_called_once_with(config.output.state_dir, vault_id="a" * 32)


    def test_core_status_keeps_saving_lifecycle_until_post_run_snapshot_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            worker_started = threading.Event()
            release_worker = threading.Event()
            snapshot_started = threading.Event()
            release_snapshot = threading.Event()
            snapshot_finished = threading.Event()

            def active_run(
                _config: AppConfig, _payload: dict[str, Any], _stop_event: threading.Event, _run_id: str
            ) -> dict[str, str]:
                worker_started.set()
                self.assertTrue(release_worker.wait(timeout=2))
                return {"status": "success"}

            def snapshot_after_run(_state_dir: Path) -> dict[str, object]:
                snapshot_started.set()
                self.assertTrue(release_snapshot.wait(timeout=2))
                try:
                    return {
                        "kind": "snapshot",
                        "status": "success",
                        "completed_at": "2026-08-12T00:00:00Z",
                        "snapshot_id": "post-run-snapshot",
                        "snapshot": {"id": "post-run-snapshot"},
                    }
                finally:
                    snapshot_finished.set()

            with (
                mock.patch.object(web_app, "_job_zapret_standard_discovery", side_effect=active_run),
                mock.patch.object(web_app, "create_post_run_snapshot", side_effect=snapshot_after_run),
            ):
                server = _start_captured_server(serve, config)
                with server, _JobRunnerThreadTracker() as runner_threads:
                    runner_threads.release_barrier(release_worker)
                    runner_threads.release_barrier(release_snapshot, "release post-run snapshot barrier")
                    status, _headers, body = _http_request(
                        server.port,
                        "/api/core/strategy-discovery/start-run",
                        method="POST",
                        body=json.dumps({"mode": "standard", "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                            "utf-8"
                        ),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 202, body.decode("utf-8", errors="replace"))
                    self.assertTrue(worker_started.wait(timeout=2))
                    release_worker.set()
                    self.assertTrue(snapshot_started.wait(timeout=2))

                    status, _headers, body = _http_request(server.port, "/api/core/status")

                    self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))
                    self.assertEqual(json.loads(body.decode("utf-8"))["current_run"]["status"], "saving")
                    self.assertEqual(runner_threads.tracked_count, 1)

                self.assertTrue(snapshot_finished.is_set())
                self.assertEqual(
                    read_state(config.output.state_dir).get("last_snapshot"),
                    {
                        "kind": "snapshot",
                        "status": "success",
                        "completed_at": "2026-08-12T00:00:00Z",
                        "snapshot_id": "post-run-snapshot",
                        "snapshot": {"id": "post-run-snapshot"},
                    },
                )


    def test_active_run_http_contract_keeps_one_public_run_id_for_ui(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            started = threading.Event()
            release = threading.Event()
            snapshot_completed = threading.Event()
            log_dir = tmp / "logs"
            log_dir.mkdir()

            def active_run(_config: AppConfig, _payload: dict[str, Any], stop_event: threading.Event, run_id: str) -> dict[str, str]:
                stdout_log = log_dir / f"{run_id}.stdout.log"
                stdout_log.write_text("active run\n", encoding="utf-8")
                append_run(
                    config.output.state_dir,
                    {
                        "id": run_id,
                        "kind": "zapret-standard-discovery",
                        "status": "running",
                        "timestamp": "2026-08-10T00:00:00Z",
                        "stdout_log": str(stdout_log),
                        "progress": {"phase": "strategy_discovery"},
                    },
                )
                started.set()
                release.wait(timeout=2)
                return {"id": run_id, "status": "stopped" if stop_event.is_set() else "success"}

            def create_snapshot_after_run(_state_dir: Path) -> dict[str, Any]:
                try:
                    return {
                        "kind": "snapshot",
                        "status": "success",
                        "completed_at": "2026-08-12T00:00:00Z",
                        "snapshot_id": "post-run-snapshot",
                        "snapshot": {"id": "post-run-snapshot"},
                    }
                finally:
                    snapshot_completed.set()

            with (
                mock.patch.object(web_app, "_job_zapret_standard_discovery", side_effect=active_run),
                mock.patch.object(web_app, "create_post_run_snapshot", side_effect=create_snapshot_after_run),
            ):
                server = _start_captured_server(serve, config)
                with server, _JobRunnerThreadTracker() as runner_threads:
                    runner_threads.release_barrier(release)
                    status, _headers, body = _http_request(
                        server.port,
                        "/api/core/strategy-discovery/start-run",
                        method="POST",
                        body=json.dumps({"mode": "standard", "domains": ["youtube.com"], "protocols": ["tcp"]}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 202, body.decode("utf-8", errors="replace"))
                    accepted = json.loads(body.decode("utf-8"))
                    run_id = accepted["run_id"]
                    self.assertTrue(started.wait(timeout=2))

                    status_payload = json.loads(_http_request(server.port, "/api/core/status")[2].decode("utf-8"))
                    progress_payload = json.loads(
                        _http_request(server.port, "/api/core/strategy-discovery/current-run-progress")[2].decode("utf-8")
                    )
                    current_log_payload = json.loads(
                        _http_request(server.port, "/api/core/strategy-discovery/current-run-latest-log")[2].decode("utf-8")
                    )
                    history_payload = json.loads(_http_request(server.port, "/api/core/runs/history")[2].decode("utf-8"))
                    generic_log_payload = json.loads(
                        _http_request(server.port, "/api/core/runs/latest-log")[2].decode("utf-8")
                    )
                    log_payload = json.loads(
                        _http_request(server.port, f"/api/core/runs/latest-log?run_id={run_id}")[2].decode("utf-8")
                    )
                    stop_status, _stop_headers, stop_body = _http_request(
                        server.port,
                        "/api/core/strategy-discovery/stop-current-run",
                        method="POST",
                        body=json.dumps({"dry_run": True}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )

                    self.assertEqual(status_payload["current_run"], {"run_id": run_id, "status": "running"})
                    self.assertEqual(progress_payload["run_id"], run_id)
                    self.assertEqual(current_log_payload["run_id"], run_id)
                    self.assertEqual(history_payload["runs"][0]["run_id"], run_id)
                    self.assertEqual(generic_log_payload["run_id"], run_id)
                    self.assertEqual(log_payload["run_id"], run_id)
                    self.assertEqual(stop_status, 202)
                    self.assertEqual(json.loads(stop_body.decode("utf-8"))["run_id"], run_id)
                    self.assertEqual(runner_threads.tracked_count, 1)

                self.assertIsNone(read_state(config.output.state_dir).get("current_run_id"))
                self.assertTrue(snapshot_completed.is_set())


    def test_current_run_endpoints_do_not_fall_back_to_previous_run_log(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            old_stdout = tmp / "old.stdout.log"
            old_stdout.write_text("old run output\n", encoding="utf-8")
            append_run(
                config.output.state_dir,
                {
                    "id": "old-run",
                    "kind": "zapret-standard-discovery",
                    "status": "success",
                    "timestamp": "2026-08-09T00:00:00Z",
                    "stdout_log": str(old_stdout),
                    "progress": {"phase": "old-phase", "attempts_processed": 42},
                },
            )
            started = threading.Event()
            release = threading.Event()
            worker_finished = threading.Event()
            snapshot_finished = threading.Event()

            def queued_run(
                _config: AppConfig, _payload: dict[str, Any], _stop_event: threading.Event, _run_id: str
            ) -> dict[str, str]:
                started.set()
                try:
                    self.assertTrue(release.wait(timeout=2))
                    return {"status": "success"}
                finally:
                    worker_finished.set()

            def create_snapshot_after_run(_state_dir: Path) -> dict[str, Any]:
                try:
                    return {
                        "kind": "snapshot",
                        "status": "success",
                        "completed_at": "2026-08-12T00:00:00Z",
                        "snapshot_id": "post-run-snapshot",
                        "snapshot": {"id": "post-run-snapshot"},
                    }
                finally:
                    snapshot_finished.set()

            with (
                mock.patch.object(web_app, "_job_zapret_standard_discovery", side_effect=queued_run),
                mock.patch.object(web_app, "create_post_run_snapshot", side_effect=create_snapshot_after_run),
            ):
                server = start_server(serve, config)
                port = server.port
                with _JobRunnerThreadTracker() as runner_threads:
                    runner_threads.release_barrier(release)
                    status, _headers, body = _http_request(
                        port,
                        "/api/core/strategy-discovery/start-run",
                        method="POST",
                        body=json.dumps({"mode": "standard", "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                            "utf-8"
                        ),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 202, body.decode("utf-8", errors="replace"))
                    current_run_id = json.loads(body.decode("utf-8"))["run_id"]

                    current_progress = json.loads(
                        _http_request(port, "/api/core/strategy-discovery/current-run-progress")[2].decode("utf-8")
                    )
                    current_log = json.loads(
                        _http_request(port, "/api/core/strategy-discovery/current-run-latest-log")[2].decode("utf-8")
                    )
                    history = json.loads(_http_request(port, "/api/core/runs/history")[2].decode("utf-8"))
                    requested_log = json.loads(
                        _http_request(port, f"/api/core/runs/latest-log?run_id={current_run_id}")[2].decode("utf-8")
                    )
                    unknown_log = json.loads(
                        _http_request(port, "/api/core/runs/latest-log?run_id=unknown-run")[2].decode("utf-8")
                    )

                    self.assertEqual(current_progress["run_id"], current_run_id)
                    self.assertEqual(current_progress["stage"], "")
                    self.assertEqual(current_progress["current_file"], "")
                    self.assertNotIn("attempts_processed", current_progress)
                    self.assertEqual(current_log["run_id"], current_run_id)
                    self.assertEqual(current_log["stdout_tail"], "")
                    self.assertEqual(current_log["stderr_tail"], "")
                    self.assertNotEqual(current_log["progress"].get("phase"), "old-phase")
                    self.assertIn(current_run_id, [item["run_id"] for item in history["runs"]])
                    self.assertEqual(requested_log["run_id"], current_run_id)
                    self.assertEqual(requested_log["stdout_tail"], "")
                    self.assertEqual(unknown_log["run_id"], "unknown-run")
                    self.assertEqual(unknown_log["stdout_tail"], "")
                    self.assertTrue(started.wait(timeout=2))
                    self.assertEqual(runner_threads.tracked_count, 1)

                self.assertTrue(worker_finished.is_set())
                self.assertTrue(snapshot_finished.is_set())


    def test_serve_clears_stale_current_job_on_start(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            write_state(config.output.state_dir, {"current_run_id": "stale-job", "last_error": None})
            server = _start_captured_server(serve, config)
            with server:
                self.assertIsNone(read_state(config.output.state_dir)["current_run_id"])


    def test_serve_does_not_clear_current_job_when_runtime_lock_exists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            write_state(config.output.state_dir, {"current_run_id": "active-job", "last_error": None})
            config.output.state_dir.mkdir(parents=True, exist_ok=True)
            (config.output.state_dir / "job-runner.lock").write_text(
                json.dumps({"pid": os.getpid(), "run_id": "active-job"}),
                encoding="utf-8",
            )
            server = _start_captured_server(serve, config)
            with server:
                self.assertEqual(read_state(config.output.state_dir)["current_run_id"], "active-job")


    def test_legacy_diagnostics_endpoint_is_removed_from_alpha_api(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/diagnostics", headers=_authenticated_headers(port))
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 404)
            self.assertIn("not found", body)


    def test_core_mode_serves_api_without_web_ui(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve_core, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/")
            root_response = connection.getresponse()
            root_body = root_response.read().decode("utf-8")
            connection.close()

            core_status, _core_headers, core_body = _http_request(port, "/api/core/status")
            web_status, _web_headers, web_body = _http_request(port, "/api/web/run-preferences")

            self.assertEqual(root_response.status, 404)
            self.assertApiError(json.loads(root_body), "not_found")
            self.assertNotIn("<!doctype html>", root_body.lower())
            self.assertEqual(core_status, 200)
            self.assertIn('"state"', core_body.decode("utf-8"))
            self.assertEqual(web_status, 404)
            self.assertIn("not found", web_body.decode("utf-8"))


    def test_history_routes_use_run_id_without_legacy_id(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            append_run(
                config.output.state_dir,
                {
                    "id": "run-contract",
                    "kind": "standard-discovery",
                    "status": "success",
                    "timestamp": "2026-08-10T00:00:00Z",
                },
            )
            authorization = _bearer_authorization_for_state(config.output.state_dir)
            core_port = start_server(serve_core, config).port

            contract = json.loads(web_app.openapi_json_bytes().decode("utf-8"))
            item_schema = contract["components"]["schemas"]["RunHistoryItem"]
            self.assertIn("run_id", item_schema["properties"])
            self.assertNotIn("id", item_schema["properties"])

            for path in ("/api/core/runs/history", "/api/internal/web-views/runs-history-page"):
                status, _headers, body = _http_request(core_port, path, headers={"Authorization": authorization})
                self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))
                item = json.loads(body.decode("utf-8"))["runs"][0]
                self.assertEqual(item["run_id"], "run-contract")
                self.assertNotIn("id", item)


    def test_core_delete_user_domain_lists_requires_explicit_non_empty_ids(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            port = start_server(serve, config).port

            status, _headers, _body = _http_request(
                port,
                "/api/core/presets/delete-user-domain-list",
                method="POST",
                body=json.dumps({"list_ids": []}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 400)

            status, _headers, _body = _http_request(
                port,
                "/api/core/presets/delete-user-lists",
                method="POST",
                body=json.dumps({"dry_run": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 404)

            for name in ("work", "games"):
                status, _headers, body = _http_request(
                    port,
                    "/api/core/presets/save-domain-list",
                    method="POST",
                    body=json.dumps({"kind": "user", "name": name, "domains": [f"{name}.example"]}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))

            status, _headers, body = _http_request(
                port,
                "/api/core/presets/delete-user-domain-list",
                method="POST",
                body=json.dumps({"list_ids": ["user:work", "user:games"]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))
            self.assertEqual({"deleted": 2}, json.loads(body.decode("utf-8")))

            status, _headers, body = _http_request(port, "/api/core/presets/domain-lists")
            self.assertEqual(status, 200)
            list_ids = [item["list_id"] for item in json.loads(body.decode("utf-8"))["lists"]]
            self.assertNotIn("user:work", list_ids)
            self.assertNotIn("user:games", list_ids)


    def test_core_error_contract_returns_plain_error_payloads(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            port = start_server(serve, config).port

            status, headers, body = _http_request(
                port,
                "/api/core/strategy-discovery/stop-current-run",
                method="POST",
                body=json.dumps({}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            payload = json.loads(body.decode("utf-8"))
            self.assertEqual(status, 409)
            self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(payload, "conflict")

            status, headers, body = _http_request(port, "/api/core/presets/v2fly/category-domains?category=missing")
            payload = json.loads(body.decode("utf-8"))
            self.assertEqual(status, 400)
            self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(payload, "invalid_request")


    def test_core_and_service_v2fly_storage_status_use_same_formatter(self) -> None:
        from gp_control_plane.domain_sources import write_v2fly_catalog_cache

        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            write_v2fly_catalog_cache(
                config.output.state_dir,
                {
                    "checked_at": "2026-07-23T10:00:00Z",
                    "remote_revision": "remote-revision",
                    "update_available": True,
                    "categories": ["youtube"],
                },
            )
            raw_status = {
                "data_status": "local",
                "revision": "local-revision",
                "checked_at": "2026-07-22T10:00:00Z",
                "all_count": 12,
            }

            core_payload = web_app.core_api.v2fly_storage_status_payload(config, raw_status)
            service_payload = web_app.service_api.v2fly_storage_status_payload(config, raw_status)

            self.assertIs(web_app.core_api.v2fly_storage_status_payload, web_app.service_api.v2fly_storage_status_payload)
            self.assertEqual(core_payload, service_payload)
            self.assertEqual(core_payload["state"], "ready")
            self.assertEqual(core_payload["source_commit"], "local-revision")
            self.assertEqual(core_payload["last_update_check"]["remote_revision"], "remote-revision")


    def test_service_v2fly_update_conflicts_when_runtime_lock_exists(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            config.output.state_dir.mkdir(parents=True, exist_ok=True)
            (config.output.state_dir / "job-runner.lock").write_text(
                json.dumps({"pid": os.getpid(), "run_id": "lock-only"}),
                encoding="utf-8",
            )
            with mock.patch.object(
                web_app.service_api,
                "prepare_v2fly_local_storage",
                side_effect=AssertionError("storage update must not run while runtime lock exists"),
            ):
                port = start_server(serve_core, config).port

                status, headers, body = _http_request(
                    port,
                    "/api/service/v2fly/update-local-storage",
                    method="POST",
                    body=json.dumps({}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                dry_status, _dry_headers, dry_body = _http_request(
                    port,
                    "/api/service/v2fly/update-local-storage",
                    method="POST",
                    body=json.dumps({"dry_run": True}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )

            payload = json.loads(body.decode("utf-8"))
            dry_payload = json.loads(dry_body.decode("utf-8"))
            self.assertEqual(status, 409)
            self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(payload, "conflict")
            self.assertEqual(dry_status, 200)
            self.assertEqual(dry_payload["status"], "dry_run")
            self.assertEqual(dry_payload["operation_id"], "v2fly-update-local-storage")
            self.assertNotIn("accepted", dry_payload)


    def test_service_v2fly_update_rejects_a_parallel_request_without_second_prepare(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            entered = threading.Event()
            release = threading.Event()
            calls = 0
            calls_lock = threading.Lock()

            def prepare(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                nonlocal calls
                with calls_lock:
                    calls += 1
                entered.set()
                self.assertTrue(release.wait(timeout=3), "test did not release the first v2fly update")
                return {"count": 2, "categories": ["discord", "youtube"]}

            with mock.patch.object(web_app.service_api, "prepare_v2fly_local_storage", side_effect=prepare):
                port = start_server(serve_core, config).port
                first: dict[str, Any] = {}

                def invoke_first() -> None:
                    first["response"] = _http_request(
                        port,
                        "/api/service/v2fly/update-local-storage",
                        method="POST",
                        body=json.dumps({}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )

                worker = threading.Thread(target=invoke_first, daemon=True)
                worker.start()
                self.assertTrue(entered.wait(timeout=3), "first v2fly update did not start")
                second_status, second_headers, second_body = _http_request(
                    port,
                    "/api/service/v2fly/update-local-storage",
                    method="POST",
                    body=json.dumps({}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                release.set()
                worker.join(timeout=3)

            self.assertFalse(worker.is_alive(), "first v2fly update did not finish")
            first_status, _first_headers, _first_body = first["response"]
            self.assertEqual(first_status, 200)
            self.assertEqual(second_status, 409)
            self.assertEqual(second_headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(json.loads(second_body.decode("utf-8")), "conflict")
            self.assertEqual(calls, 1)


    def test_v2fly_catalog_reads_wait_for_an_in_progress_update(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            entered = threading.Event()
            release = threading.Event()
            reader_done = threading.Event()

            def prepare(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                entered.set()
                self.assertTrue(release.wait(timeout=3), "test did not release v2fly update")
                return {"count": 0, "categories": []}

            with mock.patch.object(web_app.service_api, "prepare_v2fly_local_storage", side_effect=prepare):
                port = start_server(serve_core, config).port
                updater = threading.Thread(
                    target=lambda: _http_request(
                        port,
                        "/api/service/v2fly/update-local-storage",
                        method="POST",
                        body=json.dumps({}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    ),
                    daemon=True,
                )
                reader: dict[str, Any] = {}

                def read_categories() -> None:
                    reader["response"] = _http_request(port, "/api/core/presets/v2fly/categories")
                    reader_done.set()

                updater.start()
                self.assertTrue(entered.wait(timeout=3), "v2fly update did not start")
                reader_thread = threading.Thread(target=read_categories, daemon=True)
                reader_thread.start()
                self.assertFalse(reader_done.wait(timeout=0.1), "reader observed storage during v2fly publication")
                release.set()
                updater.join(timeout=3)
                reader_thread.join(timeout=3)

            self.assertFalse(updater.is_alive())
            self.assertFalse(reader_thread.is_alive())
            self.assertEqual(reader["response"][0], 200)


    def test_core_start_run_conflicts_when_state_idle_but_runner_lock_exists(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            config.output.state_dir.mkdir(parents=True, exist_ok=True)
            (config.output.state_dir / "job-runner.lock").write_text(
                json.dumps({"pid": os.getpid(), "run_id": "lock-only"}),
                encoding="utf-8",
            )
            port = start_server(serve_core, config).port

            status, headers, body = _http_request(
                port,
                "/api/core/strategy-discovery/start-run",
                method="POST",
                body=json.dumps({"domains": ["youtube.com"]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            payload = json.loads(body.decode("utf-8"))

            self.assertEqual(status, 409)
            self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(payload, "conflict")
            self.assertIsNone(read_state(config.output.state_dir)["current_run_id"])


    def test_ingress_budget_rejects_oversize_json_without_mutating_state(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            authorization = _bearer_authorization_for_state(config.output.state_dir)
            with mock.patch.object(web_app, "MAX_JSON_REQUEST_BYTES", 10):
                port = start_server(serve, config).port

                status, headers, body = _http_request(
                    port,
                    "/api/core/run-settings/save",
                    method="POST",
                    body=b'{"settings":{"curl_parallelism_max":99}}',
                    headers={"Content-Type": "application/json", "Authorization": authorization},
                )

            self.assertEqual(status, 413)
            self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(json.loads(body.decode("utf-8")), "request_too_large")
            self.assertNotIn("settings", read_state(config.output.state_dir))


    def test_backup_upload_rejects_oversize_before_import(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            with (
                mock.patch.object(web_app, "MAX_BACKUP_UPLOAD_BYTES", 10),
                mock.patch.object(web_app, "import_snapshot_archive") as import_mock,
            ):
                port = start_server(serve, config).port

                status, _headers, body = _http_request(
                    port,
                    "/api/core/backups/upload",
                    method="POST",
                    body=b"01234567890",
                    headers={"Content-Type": "application/zip"},
                )

            self.assertEqual(status, 413)
            self.assertApiError(json.loads(body.decode("utf-8")), "request_too_large")
            import_mock.assert_not_called()


    def test_backup_mutations_return_runtime_busy_while_list_and_download_remain_available(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            snapshot = web_app.create_snapshot_if_idle(config.output.state_dir)["snapshot"]["id"]
            config.output.state_dir.mkdir(parents=True, exist_ok=True)
            (config.output.state_dir / "job-runner.lock").write_text(
                json.dumps({"pid": os.getpid(), "run_id": "active-job"}),
                encoding="utf-8",
            )
            port = start_server(serve_core, config).port

            allowed_requests = [
                ("GET", "/api/core/backups/list", None, {}),
                ("GET", f"/api/core/backups/download-archive?snapshot_id={snapshot}", None, {}),
            ]
            for method, path, body, headers in allowed_requests:
                raw_body = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
                status, _response_headers, response_body = _http_request(
                    port,
                    path,
                    method=method,
                    body=raw_body,
                    headers=headers,
                )
                self.assertEqual(status, 200, (method, path, response_body.decode("utf-8", errors="replace")[:500]))

            blocked_requests = [
                ("POST", "/api/core/backups/create", {}, {"Content-Type": "application/json"}),
                ("POST", "/api/core/backups/restore", {"snapshot_id": snapshot}, {"Content-Type": "application/json"}),
                ("POST", "/api/core/backups/delete", {"snapshot_id": snapshot}, {"Content-Type": "application/json"}),
                ("POST", "/api/core/backups/upload", b"not-a-zip", {"Content-Type": "application/zip"}),
            ]
            for method, path, body, headers in blocked_requests:
                raw_body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                status, response_headers, response_body = _http_request(
                    port,
                    path,
                    method=method,
                    body=raw_body,
                    headers=headers,
                )
                message = (method, path, response_body.decode("utf-8", errors="replace"))
                self.assertEqual(status, 409, message)
                self.assertEqual(response_headers.get("content-type"), "application/json; charset=utf-8", message)
                self.assertApiError(json.loads(response_body.decode("utf-8")), "runtime_busy")


    def test_protected_api_requires_bearer_token_before_app_logic(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            port = start_server(serve, config).port

            anonymous_status, anonymous_headers, anonymous_body = _http_request(
                port,
                "/api/core/backups/list",
                authenticated=False,
            )
            self.assertEqual(anonymous_status, 401)
            self.assertEqual(anonymous_headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(json.loads(anonymous_body.decode("utf-8")), "authentication_required")

            status, _headers, body = _http_request(port, "/api/core/backups/list")
            self.assertEqual(status, 200)
            self.assertIn('"backups"', body.decode("utf-8"))


    def test_captured_server_close_keeps_handler_timeout_bounded(self) -> None:
        server = mock.Mock()
        server.request_handlers_idle.wait.return_value = False
        server.active_request_handler_count = 1
        thread = mock.Mock()
        thread.is_alive.return_value = False
        captured = _CapturedTestServer(12345, server, thread)

        with mock.patch.object(socketserver.TCPServer, "server_close") as base_server_close:
            with self.assertRaisesRegex(AssertionError, "request handlers did not stop on port 12345: 1 still active"):
                captured.close()

        server.request_handlers_idle.wait.assert_called_once()
        wait_args, wait_kwargs = server.request_handlers_idle.wait.call_args
        timeout = wait_args[0] if wait_args else wait_kwargs.get("timeout")
        self.assertNotIsInstance(timeout, bool)
        self.assertIsInstance(timeout, (int, float))
        self.assertGreater(timeout, 0)
        self.assertLess(timeout, float("inf"))
        server.server_close.assert_not_called()
        base_server_close.assert_called_once_with(server)


    def test_captured_server_context_flattens_handler_wait_and_raw_close_leaves(self) -> None:
        server = mock.Mock()
        server.request_handlers_idle.wait.return_value = False
        server.active_request_handler_count = 1
        thread = mock.Mock()
        thread.is_alive.return_value = False
        captured = _CapturedTestServer(12345, server, thread)

        with mock.patch.object(
            socketserver.TCPServer,
            "server_close",
            side_effect=ValueError("raw listener close leaf failure"),
        ):
            with self.assertRaisesRegex(AssertionError, "original test failure") as raised:
                with captured:
                    self.fail("original test failure")

        notes = getattr(raised.exception, "__notes__", ())
        self.assertEqual(len(notes), 2)
        self.assertTrue(
            any(
                "captured HTTP request handler wait listener 1" in note
                and "request handlers did not stop on port 12345: 1 still active" in note
                for note in notes
            )
        )
        self.assertTrue(
            any(
                "captured HTTP raw listener close listener 1" in note
                and "ValueError('raw listener close leaf failure')" in note
                for note in notes
            )
        )


    def test_captured_server_close_retains_each_active_socket_failure_leaf(self) -> None:
        shutdown_failure = mock.Mock()
        shutdown_failure.shutdown.side_effect = OSError(errno.EIO, "socket shutdown leaf failure")
        close_failure = mock.Mock()
        close_failure.close.side_effect = OSError(errno.EIO, "socket close leaf failure")

        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            captured = _captured_active_socket_failure_server((shutdown_failure, close_failure))
            with captured._server._request_handlers_lock:
                captured._server._active_request_sockets.update((shutdown_failure, close_failure))

            with self.assertRaises(_CleanupFailureRecords) as raised:
                captured.close()

        self.assertEqual(len(raised.exception.records), 2)
        descriptions_by_error = {str(error): description for description, error in raised.exception.records}
        self.assertIn("[Errno 5] socket shutdown leaf failure", descriptions_by_error)
        self.assertIn("[Errno 5] socket close leaf failure", descriptions_by_error)
        self.assertIn(
            "shutdown",
            descriptions_by_error["[Errno 5] socket shutdown leaf failure"],
        )
        self.assertIn(
            "close",
            descriptions_by_error["[Errno 5] socket close leaf failure"],
        )


    def test_captured_server_context_flattens_active_socket_failure_leaves(self) -> None:
        shutdown_failure = mock.Mock()
        shutdown_failure.shutdown.side_effect = OSError(errno.EIO, "socket shutdown context failure")
        close_failure = mock.Mock()
        close_failure.close.side_effect = OSError(errno.EIO, "socket close context failure")

        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            captured = _captured_active_socket_failure_server((shutdown_failure, close_failure))
            with self.assertRaisesRegex(AssertionError, "original test failure") as raised:
                with captured:
                    with captured._server._request_handlers_lock:
                        captured._server._active_request_sockets.update((shutdown_failure, close_failure))
                    self.fail("original test failure")

        notes = getattr(raised.exception, "__notes__", ())
        self.assertEqual(len(notes), 2)
        self.assertTrue(
            any(
                "captured HTTP active request socket" in note
                and "shutdown" in note
                and "socket shutdown context failure" in note
                for note in notes
            )
        )
        self.assertTrue(
            any(
                "captured HTTP active request socket" in note
                and "close" in note
                and "socket close context failure" in note
                for note in notes
            )
        )


    def test_captured_server_context_manager_closes_server(self) -> None:
        server = mock.Mock()
        thread = mock.Mock()
        thread.is_alive.return_value = False
        captured = _CapturedTestServer(12345, server, thread)

        with captured as entered:
            self.assertIs(captured, entered)

        server.shutdown.assert_called_once()
        thread.join.assert_called_once_with(timeout=5)
        server.close_active_request_connections.assert_called_once()
        server.server_close.assert_called_once()


    def test_captured_server_startup_failure_closes_constructed_listener(self) -> None:
        constructed_servers: list[Any] = []

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def startup_failure(_config: AppConfig, host: str, port: int) -> None:
            constructed_servers.append(web_app.ThreadingHTTPServer((host, port), NoopRequestHandler))
            raise RuntimeError("intentional startup failure")

        startup_failure.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with self.assertRaisesRegex(AssertionError, "server failed during startup") as raised:
                _start_captured_server(startup_failure, config)

        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertEqual(str(raised.exception.__cause__), "intentional startup failure")
        self.assertEqual(len(constructed_servers), 1)
        self.assertEqual(constructed_servers[0].socket.fileno(), -1)


    def test_captured_server_abort_closes_listener_after_late_registration(self) -> None:
        constructed_servers: list[Any] = []
        construction_threads: list[threading.Thread] = []
        constructor_passed_abandon_check = threading.Event()

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def wait_until_startup_is_abandoned(server: Any, startup_abandoned: threading.Event) -> None:
            constructed_servers.append(server)
            construction_threads.append(threading.current_thread())
            constructor_passed_abandon_check.set()
            self.assertTrue(startup_abandoned.wait(timeout=2))

        def startup_server(_config: AppConfig, host: str, port: int) -> None:
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)

        startup_server.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with self.assertRaisesRegex(AssertionError, "server (did not start serving|exited during startup)"):
                _start_captured_server(
                    startup_server,
                    config,
                    _startup_timeout=0.1,
                    _after_constructor_abandon_check=wait_until_startup_is_abandoned,
                )

        self.assertTrue(constructor_passed_abandon_check.is_set())
        self.assertEqual(len(constructed_servers), 1)
        self.assertEqual(constructed_servers[0].socket.fileno(), -1)
        self.assertEqual(len(construction_threads), 1)
        self.assertFalse(construction_threads[0].is_alive())


    def test_captured_server_startup_failure_closes_two_bound_listeners_in_reverse_order(self) -> None:
        constructed_servers: list[Any] = []
        close_order: list[Any] = []
        raw_server_close = socketserver.TCPServer.server_close

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def record_raw_close(server: Any) -> None:
            close_order.append(server)
            raw_server_close(server)

        def startup_failure(_config: AppConfig, host: str, port: int) -> None:
            constructed_servers.append(web_app.ThreadingHTTPServer((host, port), NoopRequestHandler))
            constructed_servers.append(web_app.ThreadingHTTPServer((host, port), NoopRequestHandler))
            raise RuntimeError("intentional two-listener startup failure")

        startup_failure.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.object(
                socketserver.TCPServer,
                "server_close",
                side_effect=record_raw_close,
            ):
                with self.assertRaisesRegex(AssertionError, "server failed during startup") as raised:
                    _start_captured_server(startup_failure, config)

        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertEqual(len(constructed_servers), 2)
        self.assertEqual(close_order, list(reversed(constructed_servers)))
        self.assertTrue(all(server.socket.fileno() == -1 for server in constructed_servers))


    def test_captured_server_abort_closes_late_listener_after_registration_is_abandoned(self) -> None:
        constructed_servers: list[Any] = []
        first_listener_registered = threading.Event()
        original_server = web_app.ThreadingHTTPServer

        class RecordingServer(original_server):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                constructed_servers.append(self)

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def wait_for_abort(_server: Any, startup_abandoned: threading.Event) -> None:
            if len(constructed_servers) == 1:
                first_listener_registered.set()
                self.assertTrue(startup_abandoned.wait(timeout=2))

        def startup_server(_config: AppConfig, host: str, port: int) -> None:
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)

        startup_server.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with self.assertRaisesRegex(AssertionError, "server did not start serving"):
                _start_captured_server(
                    startup_server,
                    config,
                    _startup_timeout=0.1,
                    _after_constructor_abandon_check=wait_for_abort,
                    _server_type=RecordingServer,
                )

        self.assertTrue(first_listener_registered.is_set())
        self.assertEqual(len(constructed_servers), 2)
        self.assertTrue(all(server.socket.fileno() == -1 for server in constructed_servers))


    def test_captured_listener_registry_claims_late_listener_before_concurrent_pre_join_drain(self) -> None:
        registry = _CapturedListenerRegistry(threading.Event())
        listener = mock.Mock(server_address=("127.0.0.1", 12345), serve_forever_started=None)
        late_registration_complete = threading.Event()
        release_direct_close = threading.Event()
        direct_close_errors: list[BaseException] = []

        registry.abandon()

        def close_late_listener() -> None:
            try:
                self.assertFalse(registry.register(listener))
                late_registration_complete.set()
                self.assertTrue(release_direct_close.wait(timeout=2))
                registry.close_after_abandonment(listener)
            except BaseException as error:
                direct_close_errors.append(error)

        direct_close_thread = threading.Thread(target=close_late_listener)
        with mock.patch.object(socketserver.TCPServer, "server_close") as raw_server_close:
            direct_close_thread.start()
            try:
                self.assertTrue(late_registration_complete.wait(timeout=2))
                registry.close_all("pre-join")
            finally:
                release_direct_close.set()
                direct_close_thread.join(timeout=2)

        self.assertFalse(direct_close_thread.is_alive())
        self.assertEqual(direct_close_errors, [])
        raw_server_close.assert_called_once_with(listener)
        self.assertEqual(registry.snapshot(), ())


    def test_captured_server_abort_retries_late_listener_after_early_close_failure(self) -> None:
        constructed_servers: list[Any] = []
        construction_threads: list[threading.Thread] = []
        close_attempts: list[Any] = []
        first_listener_registered = threading.Event()
        original_server = web_app.ThreadingHTTPServer
        raw_server_close = socketserver.TCPServer.server_close

        class RecordingServer(original_server):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                constructed_servers.append(self)
                construction_threads.append(threading.current_thread())

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def wait_for_abort(_server: Any, startup_abandoned: threading.Event) -> None:
            if len(constructed_servers) == 1:
                first_listener_registered.set()
                self.assertTrue(startup_abandoned.wait(timeout=2))

        def fail_first_late_listener_close(server: Any) -> None:
            close_attempts.append(server)
            if (
                len(constructed_servers) > 1
                and server is constructed_servers[1]
                and close_attempts.count(server) == 1
            ):
                raise OSError(errno.EIO, "late listener early close failure")
            raw_server_close(server)

        def startup_server(_config: AppConfig, host: str, port: int) -> None:
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)

        startup_server.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.object(
                socketserver.TCPServer,
                "server_close",
                side_effect=fail_first_late_listener_close,
            ):
                with self.assertRaisesRegex(AssertionError, "server did not start serving") as raised:
                    _start_captured_server(
                        startup_server,
                        config,
                        _startup_timeout=0.1,
                        _after_constructor_abandon_check=wait_for_abort,
                        _server_type=RecordingServer,
                    )

        self.assertTrue(first_listener_registered.is_set())
        self.assertEqual(len(constructed_servers), 2)
        self.assertEqual(close_attempts.count(constructed_servers[1]), 2)
        self.assertTrue(all(server.socket.fileno() == -1 for server in constructed_servers))
        self.assertTrue(all(not thread.is_alive() for thread in construction_threads))
        self.assertIsInstance(raised.exception.__cause__, _CleanupFailureRecords)
        self.assertEqual(
            [
                (description, type(error), str(error))
                for description, error in raised.exception.__cause__.records
            ],
            [
                (
                    "captured server abandoned listener raw close",
                    OSError,
                    "[Errno 5] late listener early close failure",
                )
            ],
        )


    def test_captured_server_abort_retries_registered_listener_after_pre_join_failure(self) -> None:
        constructed_servers: list[Any] = []
        construction_threads: list[threading.Thread] = []
        close_attempts: list[Any] = []
        listener_registered = threading.Event()
        original_server = web_app.ThreadingHTTPServer
        raw_server_close = socketserver.TCPServer.server_close

        class RecordingServer(original_server):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                constructed_servers.append(self)
                construction_threads.append(threading.current_thread())

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def wait_for_abort(_server: Any, startup_abandoned: threading.Event) -> None:
            listener_registered.set()
            self.assertTrue(startup_abandoned.wait(timeout=2))

        def fail_first_close(server: Any) -> None:
            close_attempts.append(server)
            if close_attempts.count(server) == 1:
                raise OSError(errno.EIO, "pre-join listener close failure")
            raw_server_close(server)

        def startup_server(_config: AppConfig, host: str, port: int) -> None:
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)

        startup_server.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.object(
                socketserver.TCPServer,
                "server_close",
                side_effect=fail_first_close,
            ):
                with self.assertRaisesRegex(AssertionError, "server did not start serving") as raised:
                    _start_captured_server(
                        startup_server,
                        config,
                        _startup_timeout=0.1,
                        _after_constructor_abandon_check=wait_for_abort,
                        _server_type=RecordingServer,
                    )

        self.assertTrue(listener_registered.is_set())
        self.assertEqual(len(constructed_servers), 1)
        self.assertEqual(close_attempts, [constructed_servers[0], constructed_servers[0]])
        self.assertEqual(constructed_servers[0].socket.fileno(), -1)
        self.assertTrue(all(not thread.is_alive() for thread in construction_threads))
        self.assertTrue(
            any(
                "pre-join listener" in note
                and "OSError(5, 'pre-join listener close failure')" in note
                for note in getattr(raised.exception, "__notes__", ())
            )
        )


    def test_captured_server_successfully_owns_all_listeners_and_uses_serving_port(self) -> None:
        constructed_servers: list[Any] = []
        close_order: list[Any] = []
        original_server = web_app.ThreadingHTTPServer
        raw_server_close = socketserver.TCPServer.server_close

        class RecordingServer(original_server):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                constructed_servers.append(self)

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def record_raw_close(server: Any) -> None:
            close_order.append(server)
            raw_server_close(server)

        def serve_second_listener(_config: AppConfig, host: str, port: int) -> None:
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)
            serving_listener = web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)
            serving_listener.serve_forever()

        serve_second_listener.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.object(
                socketserver.TCPServer,
                "server_close",
                autospec=True,
                side_effect=record_raw_close,
            ):
                captured = _start_captured_server(
                    serve_second_listener,
                    config,
                    _server_type=RecordingServer,
                )
                try:
                    self.assertEqual(len(constructed_servers), 2)
                    self.assertEqual(captured.port, constructed_servers[1].server_address[1])
                    self.assertNotEqual(captured.port, constructed_servers[0].server_address[1])
                    self.assertTrue(constructed_servers[1].serve_forever_started.is_set())
                finally:
                    captured.close()

        self.assertEqual(close_order, list(reversed(constructed_servers)))
        self.assertTrue(all(server.socket.fileno() == -1 for server in constructed_servers))


    def test_captured_server_context_preserves_primary_failure_when_cleanup_fails(self) -> None:
        server = mock.Mock()
        server.request_handlers_idle.wait.return_value = True
        server.shutdown.side_effect = RuntimeError("cleanup failure")
        thread = mock.Mock()
        thread.is_alive.return_value = False
        captured = _CapturedTestServer(12345, server, thread)

        with self.assertRaisesRegex(AssertionError, "original test failure") as raised:
            with captured:
                self.fail("original test failure")

        self.assertTrue(any("Cleanup also failed" in note for note in getattr(raised.exception, "__notes__", ())))


    def test_captured_server_context_flattens_nested_cleanup_failures(self) -> None:
        server = mock.Mock()
        server.request_handlers_idle.wait.return_value = True
        server.shutdown.side_effect = RuntimeError("shutdown leaf failure")
        server.server_close.side_effect = ValueError("listener close leaf failure")
        thread = mock.Mock()
        thread.is_alive.return_value = False
        captured = _CapturedTestServer(12345, server, thread)

        with self.assertRaisesRegex(AssertionError, "original test failure") as raised:
            with captured:
                self.fail("original test failure")

        notes = getattr(raised.exception, "__notes__", ())
        self.assertEqual(len(notes), 2)
        self.assertTrue(
            any(
                "captured HTTP server shutdown listener 1" in note
                and "RuntimeError('shutdown leaf failure')" in note
                for note in notes
            )
        )
        self.assertTrue(
            any(
                "captured HTTP listener close listener 1" in note
                and "ValueError('listener close leaf failure')" in note
                for note in notes
            )
        )


    def test_cleanup_scope_preserves_primary_failure_and_all_cleanup_failure_details(self) -> None:
        def fail_first_cleanup() -> None:
            raise RuntimeError("first cleanup failure")

        def fail_second_cleanup() -> None:
            raise ValueError("second cleanup failure")

        with self.assertRaisesRegex(AssertionError, "original test failure") as raised:
            with _cleanup_scope() as cleanup:
                cleanup.add("first cleanup action", fail_first_cleanup)
                cleanup.add("second cleanup action", fail_second_cleanup)
                self.fail("original test failure")

        self.assertEqual(str(raised.exception), "original test failure")
        notes = getattr(raised.exception, "__notes__", ())
        self.assertTrue(any("second cleanup action" in note and "ValueError('second cleanup failure')" in note for note in notes))
        self.assertTrue(any("first cleanup action" in note and "RuntimeError('first cleanup failure')" in note for note in notes))


    def test_cleanup_scope_raises_all_nested_leaf_records_without_primary_error(self) -> None:
        def fail_nested_first_cleanup() -> None:
            raise RuntimeError("nested first failure")

        def fail_nested_second_cleanup() -> None:
            raise ValueError("nested second failure")

        def nested_cleanup() -> None:
            with _cleanup_scope() as cleanup:
                cleanup.add("nested first cleanup action", fail_nested_first_cleanup)
                cleanup.add("nested second cleanup action", fail_nested_second_cleanup)

        with self.assertRaises(_CleanupFailureRecords) as raised:
            with _cleanup_scope() as cleanup:
                cleanup.add("outer nested cleanup", nested_cleanup)

        self.assertEqual(
            [(description, type(error), str(error)) for description, error in raised.exception.records],
            [
                ("nested second cleanup action", ValueError, "nested second failure"),
                ("nested first cleanup action", RuntimeError, "nested first failure"),
            ],
        )


    def test_job_runner_tracker_preserves_primary_failure_when_join_fails(self) -> None:
        stuck_thread = mock.Mock()
        stuck_thread.is_alive.return_value = True

        with self.assertRaisesRegex(AssertionError, "original test failure") as raised:
            with _JobRunnerThreadTracker() as tracker:
                tracker._register(stuck_thread)
                self.fail("original test failure")

        stuck_thread.join.assert_called_once_with(timeout=2)
        self.assertTrue(any("Cleanup also failed" in note for note in getattr(raised.exception, "__notes__", ())))


    def test_job_runner_tracker_raises_join_failure_without_primary_error(self) -> None:
        stuck_thread = mock.Mock()
        stuck_thread.is_alive.return_value = True

        with self.assertRaisesRegex(AssertionError, "JobRunner worker thread did not stop"):
            with _JobRunnerThreadTracker() as tracker:
                tracker._register(stuck_thread)


    def test_job_runner_tracker_joins_worker_registered_during_cleanup(self) -> None:
        worker_finished = threading.Event()
        late_worker: list[threading.Thread] = []

        def start_late_worker(tracker: _JobRunnerThreadTracker) -> None:
            thread = threading.Thread(target=worker_finished.set, daemon=True)
            late_worker.append(thread)
            tracker._register(thread)
            thread.start()

        with _JobRunnerThreadTracker() as tracker:
            tracker.add_release_action("start late JobRunner worker", lambda: start_late_worker(tracker))

        self.assertTrue(worker_finished.is_set())
        self.assertEqual(len(late_worker), 1)
        self.assertFalse(late_worker[0].is_alive())


    def test_split_settings_endpoints_save_runtime_defaults(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps(
                {
                    "settings": {
                        "enable_ipv6": True,
                        "debug_stdout": True,
                        "curl_parallelism_max": 25,
                        "curl_max_time": 1,
                        "curl_max_time_quic": 3,
                        "curl_max_time_doh": 4,
                    }
                }
            )
            connection.request("POST", "/api/core/run-settings/save", body=body, headers=_authenticated_headers(port, {"Content-Type": "application/json"}))
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"enable_ipv6":true', saved)
            self.assertIn('"debug_stdout":true', saved)
            self.assertNotIn('"settings_preset_default"', saved)
            self.assertIn('"curl_parallelism_max":25', saved)
            self.assertIn('"curl_max_time":1', saved)
            self.assertIn('"curl_max_time_quic":3', saved)
            self.assertIn('"curl_max_time_doh":4', saved)
            self.assertNotIn('"update_channel"', saved)

            stored = read_app_setting(config.output.state_dir, "run_settings")
            legacy = read_state(config.output.state_dir).get("settings")
            self.assertIsInstance(stored, dict)
            self.assertEqual(stored["curl_parallelism_max"], 25)
            self.assertNotIn("update_channel", stored)
            self.assertEqual(legacy["curl_parallelism_max"], 25)


    def test_read_settings_migrates_legacy_state_settings_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            config = AppConfig(output=OutputConfig(state_dir=state_dir))
            write_state(
                state_dir,
                {"settings": {"curl_parallelism_max": 33, "enable_ipv6": True, "update_channel": "prerelease"}},
            )

            settings = web_app.read_settings(config)

            self.assertEqual(settings["curl_parallelism_max"], 33)
            self.assertTrue(settings["enable_ipv6"])
            self.assertEqual(settings["update_channel"], "prerelease")
            stored = read_app_setting(state_dir, "run_settings")
            service_stored = read_app_setting(state_dir, "service_settings")
            self.assertIsInstance(stored, dict)
            self.assertIsInstance(service_stored, dict)
            self.assertEqual(stored["curl_parallelism_max"], 33)
            self.assertNotIn("update_channel", stored)
            self.assertEqual(service_stored["update_channel"], "prerelease")
            self.assertIn("settings", read_state(state_dir))


    def test_core_run_settings_save_ignores_service_settings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))

            saved = web_app.save_run_settings(
                config,
                {"curl_parallelism_max": 44, "curl_parallelism_default": 12, "update_channel": "prerelease"},
            )

            stored = read_app_setting(config.output.state_dir, "run_settings")
            service_stored = read_app_setting(config.output.state_dir, "service_settings")
            legacy = read_state(config.output.state_dir).get("settings")
            self.assertEqual(saved["curl_parallelism_max"], 44)
            self.assertEqual(saved["curl_parallelism_default"], 12)
            self.assertNotIn("update_channel", saved)
            self.assertNotIn("update_channel", stored)
            self.assertIsNone(service_stored)
            self.assertEqual(legacy["curl_parallelism_max"], 44)
            self.assertEqual(legacy["update_channel"], "stable")


    def test_resource_budget_constants_are_wired_to_runtime_limits(self) -> None:
        from gp_control_plane import backups as backup_module
        from gp_control_plane import resource_budget

        self.assertEqual(web_app.MAX_BACKUP_UPLOAD_BYTES, resource_budget.BACKUP_UPLOAD_MAX_BYTES)
        self.assertEqual(web_app.MAX_JSON_REQUEST_BYTES, resource_budget.JSON_REQUEST_MAX_BYTES)
        self.assertEqual(resource_budget.JSON_REQUEST_MAX_BYTES, 1 * 1024 * 1024)
        self.assertEqual(resource_budget.BACKUP_UPLOAD_MAX_BYTES, 64 * 1024 * 1024)
        self.assertEqual(backup_module.BACKUP_STREAM_CHUNK_BYTES, resource_budget.BACKUP_STREAM_CHUNK_BYTES)
        self.assertEqual(
            web_app.DEFAULT_SETTINGS["curl_parallelism_max"],
            resource_budget.RASPBERRY_PI2_CURL_PARALLELISM_SAFE_MAX,
        )
        self.assertFalse(resource_budget.DIAGNOSTICS_INCLUDE_HOST_METRICS)


    def test_standard_discovery_job_uses_launch_timeout_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            payload = {
                "domains": ["youtube.com"],
                "curl_max_time": 7,
                "curl_max_time_quic": 8,
                "curl_max_time_doh": 9,
            }

            with mock.patch.object(web_app, "run_standard_discovery", return_value={"status": "success"}) as runner:
                result = web_app._job_zapret_standard_discovery(config, payload, object())

            self.assertEqual({"status": "success"}, result)
            self.assertEqual(7, runner.call_args.kwargs["curl_max_time"])
            self.assertEqual(8, runner.call_args.kwargs["curl_max_time_quic"])
            self.assertEqual(9, runner.call_args.kwargs["curl_max_time_doh"])


    def test_multi_domain_discovery_job_uses_launch_timeout_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            payload = {
                "domains": ["youtube.com", "discord.com"],
                "curl_parallelism": 2,
                "curl_max_time": 11,
                "curl_max_time_quic": 12,
                "curl_max_time_doh": 13,
            }

            with mock.patch.object(web_app, "run_multi_domain_discovery", return_value={"status": "success"}) as runner:
                result = web_app._job_zapret_multi_domain_discovery(config, payload, object())

            self.assertEqual({"status": "success"}, result)
            self.assertEqual(11, runner.call_args.kwargs["curl_max_time"])
            self.assertEqual(12, runner.call_args.kwargs["curl_max_time_quic"])
            self.assertEqual(13, runner.call_args.kwargs["curl_max_time_doh"])
            self.assertEqual(2, runner.call_args.kwargs["curl_parallelism"])


    def test_core_strategy_discovery_start_run_routes_swagger_payload(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            save_settings = web_app.save_settings
            save_settings(config, {"curl_parallelism_max": 50, "curl_parallelism_default": 10})
            finished = threading.Event()
            snapshot_finished = threading.Event()

            def fake_run(*args: object, **kwargs: object) -> dict[str, str]:
                try:
                    return {"status": "success"}
                finally:
                    finished.set()

            def create_snapshot_after_run(*_args: object, **_kwargs: object) -> dict[str, object]:
                try:
                    return {
                        "kind": "snapshot",
                        "status": "success",
                        "completed_at": "2026-08-12T00:00:00Z",
                        "snapshot_id": "post-run-snapshot",
                    }
                finally:
                    snapshot_finished.set()

            with (
                mock.patch.object(web_app, "run_multi_domain_discovery", side_effect=fake_run) as runner,
                mock.patch.object(web_app, "create_post_run_snapshot", side_effect=create_snapshot_after_run),
            ):
                with _JobRunnerThreadTracker() as runner_threads:
                    port = start_server(serve, config).port
                    body = json.dumps(
                        {
                            "mode": "multi_domain",
                            "domains": ["youtube.com", "discord.com", "airhorn.solutions"],
                            "protocols": ["tcp", "quic"],
                            "curl_parallelism": 30,
                            "timeout_seconds": 172800,
                            "settings": {
                                "curl_max_time": 7,
                                "curl_max_time_quic": 7,
                                "enable_ipv6": False,
                                "skip_ipblock": False,
                            },
                        }
                    )
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    connection.request(
                        "POST",
                        "/api/core/strategy-discovery/start-run",
                        body=body,
                        headers=_authenticated_headers(port, {"Content-Type": "application/json", "Accept": "application/json"}),
                    )
                    response = connection.getresponse()
                    raw_response = response.read().decode("utf-8")
                    connection.close()

                    self.assertEqual(response.status, 202)
                    accepted = json.loads(raw_response)
                    self.assertTrue(accepted["accepted"])
                    self.assertTrue(accepted["run_id"])
                    self.assertEqual("queued", accepted["status"])
                    self.assertTrue(finished.wait(timeout=2))
                    runner_threads.join_tracked()
                    self.assertEqual(30, runner.call_args.kwargs["curl_parallelism"])
                    self.assertEqual(7, runner.call_args.kwargs["curl_max_time"])
                    self.assertEqual(7, runner.call_args.kwargs["curl_max_time_quic"])
                    self.assertFalse(runner.call_args.kwargs["enable_ipv6"])
                    self.assertFalse(runner.call_args.kwargs["skip_ipblock"])
                    self.assertTrue(runner.call_args.kwargs["include_quic"])
                    self.assertTrue(runner.call_args.kwargs["enable_tls12"])
                    self.assertEqual(runner_threads.tracked_count, 1)
                    self.assertTrue(snapshot_finished.is_set())


    def test_strategy_discovery_start_installs_one_time_runtime_cleanup_hook(self) -> None:
        def run_cancel_hook_regression(mode: str) -> None:
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                captured_start_kwargs: list[dict[str, object]] = []
                original_runner = web_app.JobRunner
                worker_started = threading.Event()
                worker_cancelled = threading.Event()
                release_worker = threading.Event()
                worker_finished = threading.Event()
                snapshot_completed = threading.Event()
                cleanup_started = threading.Event()

                class CapturingJobRunner(original_runner):
                    def start(self, *args: object, **kwargs: object) -> object:
                        captured_start_kwargs.append(dict(kwargs))
                        return super().start(*args, **kwargs)

                def worker_run(*args: object, **kwargs: object) -> dict[str, str]:
                    stop_event = kwargs["stop_event"]
                    run_id = kwargs["run_id"]
                    self.assertIsInstance(stop_event, threading.Event)
                    self.assertIsInstance(run_id, str)
                    worker_started.set()
                    try:
                        self.assertTrue(stop_event.wait(timeout=2))
                        strategy_finder.signal_registered_process_run(run_id, "TERM")
                        worker_cancelled.set()
                        self.assertTrue(release_worker.wait(timeout=2))
                        return {"status": "stopped"}
                    finally:
                        worker_finished.set()

                def create_snapshot_when_idle(*_args: object, **_kwargs: object) -> dict[str, object]:
                    try:
                        return {
                            "kind": "snapshot",
                            "status": "success",
                            "completed_at": "2026-08-12T00:00:00Z",
                            "snapshot_id": "post-run-snapshot",
                        }
                    finally:
                        snapshot_completed.set()

                with (
                    mock.patch.object(web_app, "JobRunner", CapturingJobRunner),
                    mock.patch.object(web_app, "run_standard_discovery", side_effect=worker_run),
                    mock.patch.object(web_app, "run_multi_domain_discovery", side_effect=worker_run),
                    mock.patch.object(web_app, "create_post_run_snapshot", side_effect=create_snapshot_when_idle),
                    mock.patch.object(strategy_finder, "signal_registered_process_run") as root_signal,
                    mock.patch.object(
                        web_app,
                        "cleanup_nft_blockcheck_tables",
                        side_effect=cleanup_started.set,
                    ) as cleanup,
                ):
                    server = _start_captured_server(serve, config)
                    with server, _JobRunnerThreadTracker() as runner_threads:
                        runner_threads.release_barrier(release_worker)
                        runner_threads.add_release_action(
                            "cancel active JobRunner before releasing worker",
                            lambda: _stop_current_run_if_started(server.port, worker_started, worker_finished),
                        )
                        status, _headers, body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/start-run",
                            method="POST",
                            body=json.dumps({"mode": mode, "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                                "utf-8"
                            ),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(status, 202, body.decode("utf-8", errors="replace"))
                        accepted = json.loads(body.decode("utf-8"))
                        self.assertTrue(accepted["run_id"])
                        self.assertTrue(worker_started.wait(timeout=2))
                        self.assertEqual(len(captured_start_kwargs), 1)
                        self.assertTrue(callable(captured_start_kwargs[0].get("cancel_hook")))

                        for _ in range(2):
                            stop_status, _stop_headers, stop_body = _http_request(
                                server.port,
                                "/api/core/strategy-discovery/stop-current-run",
                                method="POST",
                                body=b"{}",
                                headers={"Content-Type": "application/json"},
                            )
                            self.assertEqual(stop_status, 202, stop_body.decode("utf-8", errors="replace"))
                        self.assertTrue(worker_cancelled.wait(timeout=2))
                        self.assertTrue(cleanup_started.wait(timeout=2))
                        cleanup.assert_called_once_with()
                        root_signal.assert_called_once_with(accepted["run_id"], "TERM")

                        release_worker.set()
                        self.assertTrue(worker_finished.wait(timeout=2))
                        runner_threads.join_tracked()
                        state = read_state(config.output.state_dir)
                        status_code, _status_headers, status_body = _http_request(server.port, "/api/core/status")
                        self.assertEqual(status_code, 200, status_body.decode("utf-8", errors="replace"))
                        status_payload = json.loads(status_body.decode("utf-8"))
                        self.assertIsNone(state["current_run_id"])
                        self.assertEqual("stopped", state["last_run_status"])
                        self.assertEqual("idle", status_payload["state"])
                        self.assertEqual(runner_threads.tracked_count, 1)

                        self.assertTrue(snapshot_completed.is_set())

        for mode in ("standard", "multi_domain"):
            with self.subTest(mode=mode):
                run_cancel_hook_regression(mode)


    def test_strategy_discovery_immediate_stop_finishes_without_privileged_child(self) -> None:
        def run_immediate_stop(mode: str) -> None:
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                original_runner = web_app.JobRunner
                worker_at_barrier = threading.Event()
                release_worker = threading.Event()
                worker_finished = threading.Event()
                cleanup_completed = threading.Event()
                worker_tokens: list[object] = []
                child_popen_commands: list[object] = []
                child_popen_stacks: list[str] = []
                original_popen = strategy_finder.subprocess.Popen

                def reject_blockcheck_popen(command: object, *args: object, **kwargs: object) -> object:
                    if command == ["/test/blockcheck2.sh"]:
                        child_popen_commands.append(command)
                        child_popen_stacks.append("".join(traceback.format_stack()))
                        raise AssertionError("privileged blockcheck child must not start after immediate stop")
                    return original_popen(command, *args, **kwargs)

                class BarrierJobRunner(original_runner):
                    def _run(self, *args: object, **kwargs: object) -> None:
                        worker_tokens.append(args[4])
                        worker_at_barrier.set()
                        try:
                            if not release_worker.wait(timeout=2):
                                raise AssertionError("test did not release discovery worker")
                            super()._run(*args, **kwargs)
                        finally:
                            worker_finished.set()

                with (
                    mock.patch.object(web_app, "JobRunner", BarrierJobRunner),
                    mock.patch.object(web_app, "recover_registered_process_runs", return_value=True),
                    mock.patch.object(
                        web_app,
                        "create_post_run_snapshot",
                        return_value={
                            "kind": "snapshot",
                            "status": "success",
                            "completed_at": "2026-08-12T00:00:00Z",
                            "snapshot_id": "post-run-snapshot",
                        },
                    ),
                    mock.patch.object(strategy_finder.shutil, "which", return_value="/test/blockcheck2.sh"),
                    mock.patch.object(strategy_finder, "_count_script_function_attempts", return_value=1),
                    mock.patch.object(
                        strategy_finder,
                        "root_command",
                        side_effect=AssertionError("root_command must not run after immediate stop"),
                    ) as root_command,
                    mock.patch.object(
                        strategy_finder.subprocess, "Popen", side_effect=reject_blockcheck_popen
                    ),
                    mock.patch.object(
                        strategy_finder,
                        "signal_registered_process_run",
                        side_effect=AssertionError("root signal must not run after immediate stop"),
                    ) as root_signal,
                    mock.patch.object(
                        web_app,
                        "cleanup_nft_blockcheck_tables",
                        side_effect=cleanup_completed.set,
                    ) as cleanup,
                ):
                    server = _start_captured_server(serve, config)
                    with server, _JobRunnerThreadTracker() as runner_threads:
                        runner_threads.release_barrier(release_worker)
                        start_status, _headers, start_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/start-run",
                            method="POST",
                            body=json.dumps({"mode": mode, "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                                "utf-8"
                            ),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(start_status, 202, start_body.decode("utf-8", errors="replace"))
                        accepted = json.loads(start_body.decode("utf-8"))
                        self.assertTrue(worker_at_barrier.wait(timeout=2))
                        self.assertEqual(1, len(worker_tokens))
                        self.assertIsInstance(worker_tokens[0], jobs._CancellationToken)
                        self.assertFalse(worker_tokens[0].is_set())

                        stop_status, _headers, stop_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/stop-current-run",
                            method="POST",
                            body=b"{}",
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(stop_status, 202, stop_body.decode("utf-8", errors="replace"))
                        self.assertEqual(accepted["run_id"], json.loads(stop_body.decode("utf-8"))["run_id"])
                        self.assertTrue(cleanup_completed.wait(timeout=2))
                        cleanup.assert_called_once_with()
                        self.assertTrue(worker_tokens[0].is_set())

                        release_worker.set()
                        self.assertTrue(worker_finished.wait(timeout=2))
                        runner_threads.join_tracked()
                        self.assertEqual([], child_popen_commands, "".join(child_popen_stacks))

                        state = read_state(config.output.state_dir)
                        status_status, _headers, status_body = _http_request(server.port, "/api/core/status")
                        history_status, _headers, history_body = _http_request(server.port, "/api/core/runs/history")
                        self.assertEqual(status_status, 200, status_body.decode("utf-8", errors="replace"))
                        self.assertEqual(history_status, 200, history_body.decode("utf-8", errors="replace"))
                        status_payload = json.loads(status_body.decode("utf-8"))
                        history = json.loads(history_body.decode("utf-8"))

                        self.assertIsNone(state["current_run_id"])
                        self.assertIsNone(state["last_error"])
                        self.assertEqual("stopped", state["last_run_status"])
                        self.assertEqual("idle", status_payload["state"])
                        self.assertEqual(accepted["run_id"], history["runs"][0]["run_id"])
                        self.assertEqual("stopped", history["runs"][0]["status"])
                        root_command.assert_not_called()
                        self.assertEqual([], child_popen_commands)
                        root_signal.assert_not_called()
                        self.assertEqual(runner_threads.tracked_count, 1)

        for mode in ("standard", "multi_domain"):
            with self.subTest(mode=mode):
                run_immediate_stop(mode)


    def test_strategy_discovery_stop_during_root_command_finishes_without_privileged_child(self) -> None:
        def run_stop_during_root_command(mode: str) -> None:
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                original_runner = web_app.JobRunner
                root_command_entered = threading.Event()
                release_root_command = threading.Event()
                worker_finished = threading.Event()
                cleanup_completed = threading.Event()
                worker_tokens: list[object] = []
                child_popen_commands: list[object] = []
                child_popen_stacks: list[str] = []
                original_popen = strategy_finder.subprocess.Popen

                def reject_blockcheck_popen(command: object, *args: object, **kwargs: object) -> object:
                    if command == ["/test/blockcheck2.sh"]:
                        child_popen_commands.append(command)
                        child_popen_stacks.append("".join(traceback.format_stack()))
                        raise AssertionError("privileged blockcheck child must not start after stop during root_command")
                    return original_popen(command, *args, **kwargs)

                class ObservingJobRunner(original_runner):
                    def _run(self, *args: object, **kwargs: object) -> None:
                        worker_tokens.append(args[4])
                        try:
                            super()._run(*args, **kwargs)
                        finally:
                            worker_finished.set()

                def root_command_that_waits(command: list[str], **_kwargs: object) -> list[str]:
                    root_command_entered.set()
                    if not release_root_command.wait(timeout=2):
                        raise AssertionError("test did not release root_command")
                    return command

                with (
                    mock.patch.object(web_app, "JobRunner", ObservingJobRunner),
                    mock.patch.object(web_app, "recover_registered_process_runs", return_value=True),
                    mock.patch.object(
                        web_app,
                        "create_post_run_snapshot",
                        return_value={
                            "kind": "snapshot",
                            "status": "success",
                            "completed_at": "2026-08-12T00:00:00Z",
                            "snapshot_id": "post-run-snapshot",
                        },
                    ),
                    mock.patch.object(strategy_finder.shutil, "which", return_value="/test/blockcheck2.sh"),
                    mock.patch.object(strategy_finder, "_count_script_function_attempts", return_value=1),
                    mock.patch.object(
                        strategy_finder,
                        "root_command",
                        side_effect=root_command_that_waits,
                    ) as root_command,
                    mock.patch.object(
                        strategy_finder.subprocess,
                        "Popen",
                        side_effect=reject_blockcheck_popen,
                    ),
                    mock.patch.object(
                        strategy_finder,
                        "signal_registered_process_run",
                        side_effect=AssertionError("root signal must not run after stop during root_command"),
                    ) as root_signal,
                    mock.patch.object(
                        web_app,
                        "cleanup_nft_blockcheck_tables",
                        side_effect=cleanup_completed.set,
                    ) as cleanup,
                ):
                    server = _start_captured_server(serve, config)
                    with server, _JobRunnerThreadTracker() as runner_threads:
                        runner_threads.release_barrier(release_root_command)
                        start_status, _headers, start_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/start-run",
                            method="POST",
                            body=json.dumps({"mode": mode, "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                                "utf-8"
                            ),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(start_status, 202, start_body.decode("utf-8", errors="replace"))
                        accepted = json.loads(start_body.decode("utf-8"))
                        self.assertTrue(root_command_entered.wait(timeout=2), "".join(child_popen_stacks))
                        self.assertEqual(1, len(worker_tokens))
                        self.assertIsInstance(worker_tokens[0], jobs._CancellationToken)
                        self.assertFalse(worker_tokens[0].is_set())

                        stop_status, _headers, stop_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/stop-current-run",
                            method="POST",
                            body=b"{}",
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(stop_status, 202, stop_body.decode("utf-8", errors="replace"))
                        self.assertEqual(accepted["run_id"], json.loads(stop_body.decode("utf-8"))["run_id"])
                        self.assertTrue(cleanup_completed.wait(timeout=2))
                        cleanup.assert_called_once_with()
                        self.assertTrue(worker_tokens[0].is_set())

                        release_root_command.set()
                        self.assertTrue(worker_finished.wait(timeout=2))
                        runner_threads.join_tracked()
                        self.assertEqual([], child_popen_commands, "".join(child_popen_stacks))

                        state = read_state(config.output.state_dir)
                        history_status, _headers, history_body = _http_request(server.port, "/api/core/runs/history")
                        self.assertEqual(history_status, 200, history_body.decode("utf-8", errors="replace"))
                        history = json.loads(history_body.decode("utf-8"))
                        self.assertIsNone(state["last_error"])
                        self.assertEqual("stopped", state["last_run_status"])
                        self.assertEqual(accepted["run_id"], history["runs"][0]["run_id"])
                        self.assertEqual("stopped", history["runs"][0]["status"])
                        root_command.assert_called_once()
                        self.assertEqual([], child_popen_commands)
                        root_signal.assert_not_called()
                        self.assertEqual(runner_threads.tracked_count, 1)

        for mode in ("standard", "multi_domain"):
            with self.subTest(mode=mode):
                run_stop_during_root_command(mode)


    def test_strategy_discovery_stop_after_precheck_before_popen_finishes_stopped(self) -> None:
        def run_stop_after_precheck_before_popen(mode: str) -> None:
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                original_runner = web_app.JobRunner
                original_stdout_log_enter = strategy_finder._RotatingTextWriter.__enter__
                stdout_log_opened = threading.Event()
                release_stdout_log = threading.Event()
                worker_finished = threading.Event()
                cleanup_completed = threading.Event()
                worker_tokens: list[object] = []
                claim_observations: list[tuple[object, bool, bool | None]] = []
                child_popen_commands: list[object] = []
                child_popen_stacks: list[str] = []
                original_popen = strategy_finder.subprocess.Popen
                original_claim_child_launch = strategy_finder._claim_child_launch

                def reject_blockcheck_popen(command: object, *args: object, **kwargs: object) -> object:
                    if command == ["/test/blockcheck2.sh"]:
                        child_popen_commands.append(command)
                        child_popen_stacks.append("".join(traceback.format_stack()))
                        raise AssertionError("privileged blockcheck child must not start after stop before Popen")
                    return original_popen(command, *args, **kwargs)

                @contextlib.contextmanager
                def observe_child_launch_claim(stop_event: object) -> object:
                    is_set = getattr(stop_event, "is_set")
                    claim_observations.append((stop_event, bool(is_set()), None))
                    with original_claim_child_launch(stop_event) as claimed:
                        claim_observations.append((stop_event, bool(is_set()), bool(claimed)))
                        yield claimed

                class ObservingJobRunner(original_runner):
                    def _run(self, *args: object, **kwargs: object) -> None:
                        worker_tokens.append(args[4])
                        try:
                            super()._run(*args, **kwargs)
                        finally:
                            worker_finished.set()

                def open_stdout_log_at_barrier(writer: strategy_finder._RotatingTextWriter) -> strategy_finder._RotatingTextWriter:
                    result = original_stdout_log_enter(writer)
                    if writer._path.name.endswith(".stdout.log"):
                        stdout_log_opened.set()
                        if not release_stdout_log.wait(timeout=2):
                            raise AssertionError("test did not release stdout-log barrier")
                    return result

                with (
                    mock.patch.object(web_app, "JobRunner", ObservingJobRunner),
                    mock.patch.object(web_app, "recover_registered_process_runs", return_value=True),
                    mock.patch.object(
                        web_app,
                        "create_post_run_snapshot",
                        return_value={
                            "kind": "snapshot",
                            "status": "success",
                            "completed_at": "2026-08-12T00:00:00Z",
                            "snapshot_id": "post-run-snapshot",
                        },
                    ),
                    mock.patch.object(strategy_finder.shutil, "which", return_value="/test/blockcheck2.sh"),
                    mock.patch.object(strategy_finder, "_count_script_function_attempts", return_value=1),
                    mock.patch.object(strategy_finder, "root_command", side_effect=lambda command, **_kwargs: command) as root_command,
                    mock.patch.object(
                        strategy_finder._RotatingTextWriter,
                        "__enter__",
                        new=open_stdout_log_at_barrier,
                    ),
                    mock.patch.object(strategy_finder, "_claim_child_launch", side_effect=observe_child_launch_claim),
                    mock.patch.object(
                        strategy_finder.subprocess,
                        "Popen",
                        side_effect=reject_blockcheck_popen,
                    ),
                    mock.patch.object(
                        strategy_finder,
                        "signal_registered_process_run",
                        side_effect=AssertionError("root signal must not run before a child is launched"),
                    ) as root_signal,
                    mock.patch.object(
                        web_app,
                        "cleanup_nft_blockcheck_tables",
                        side_effect=cleanup_completed.set,
                    ) as cleanup,
                ):
                    server = _start_captured_server(serve, config)
                    with server, _JobRunnerThreadTracker() as runner_threads:
                        runner_threads.release_barrier(release_stdout_log)
                        start_status, _headers, start_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/start-run",
                            method="POST",
                            body=json.dumps({"mode": mode, "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                                "utf-8"
                            ),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(start_status, 202, start_body.decode("utf-8", errors="replace"))
                        accepted = json.loads(start_body.decode("utf-8"))
                        self.assertTrue(stdout_log_opened.wait(timeout=2), "".join(child_popen_stacks))
                        self.assertEqual(1, len(worker_tokens))
                        self.assertIsInstance(worker_tokens[0], jobs._CancellationToken)
                        self.assertFalse(worker_tokens[0].is_set())

                        stop_status, _headers, stop_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/stop-current-run",
                            method="POST",
                            body=b"{}",
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(stop_status, 202, stop_body.decode("utf-8", errors="replace"))
                        self.assertEqual(accepted["run_id"], json.loads(stop_body.decode("utf-8"))["run_id"])
                        self.assertTrue(cleanup_completed.wait(timeout=2))
                        cleanup.assert_called_once_with()
                        self.assertTrue(worker_tokens[0].is_set())

                        release_stdout_log.set()
                        self.assertTrue(worker_finished.wait(timeout=2))
                        runner_threads.join_tracked()
                        self.assertEqual([], child_popen_commands, "".join(child_popen_stacks))

                        state = read_state(config.output.state_dir)
                        status_status, _headers, status_body = _http_request(server.port, "/api/core/status")
                        history_status, _headers, history_body = _http_request(server.port, "/api/core/runs/history")
                        self.assertEqual(status_status, 200, status_body.decode("utf-8", errors="replace"))
                        self.assertEqual(history_status, 200, history_body.decode("utf-8", errors="replace"))
                        status_payload = json.loads(status_body.decode("utf-8"))
                        history = json.loads(history_body.decode("utf-8"))

                        self.assertIsNone(state["current_run_id"])
                        self.assertIsNone(state["last_error"])
                        self.assertEqual("stopped", state["last_run_status"])
                        self.assertEqual("idle", status_payload["state"])
                        self.assertEqual(accepted["run_id"], history["runs"][0]["run_id"])
                        self.assertEqual("stopped", history["runs"][0]["status"])
                        root_command.assert_called_once()
                        self.assertEqual([], child_popen_commands)
                        self.assertEqual(
                            [(worker_tokens[0], True, None), (worker_tokens[0], True, False)],
                            claim_observations,
                        )
                        root_signal.assert_not_called()
                        self.assertEqual(runner_threads.tracked_count, 1)

        for mode in ("standard", "multi_domain"):
            with self.subTest(mode=mode):
                run_stop_after_precheck_before_popen(mode)


    def test_strategy_discovery_without_stop_reaches_actual_privileged_child(self) -> None:
        def run_without_stop(mode: str) -> None:
            blockcheck_path = str(Path("/test/blockcheck2.sh").resolve())
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                worker_finished = threading.Event()
                child_started = threading.Event()
                child_commands: list[list[str]] = []
                child_kwargs: list[dict[str, object]] = []

                class CompletedChild:
                    def __init__(self) -> None:
                        self.stdout = io.StringIO("")
                        self.returncode: int | None = None

                    def wait(self, timeout: float | None = None) -> int:
                        self.returncode = 0
                        return 0

                child = CompletedChild()
                original_runner = web_app.JobRunner

                class ObservingJobRunner(original_runner):
                    def _run(self, *args: object, **kwargs: object) -> None:
                        try:
                            super()._run(*args, **kwargs)
                        finally:
                            worker_finished.set()

                def launch_child(command: list[str], *args: object, **kwargs: object) -> CompletedChild:
                    child_commands.append(command)
                    child_kwargs.append(dict(kwargs))
                    child_started.set()
                    return child

                with (
                    mock.patch.object(web_app, "JobRunner", ObservingJobRunner),
                    mock.patch.object(web_app, "recover_registered_process_runs", return_value=True),
                    mock.patch.object(
                        web_app,
                        "create_post_run_snapshot",
                        return_value={
                            "kind": "snapshot",
                            "status": "success",
                            "completed_at": "2026-08-12T00:00:00Z",
                            "snapshot_id": "post-run-snapshot",
                        },
                    ),
                    mock.patch.object(strategy_finder.shutil, "which", return_value=blockcheck_path),
                    mock.patch.object(strategy_finder, "_count_script_function_attempts", return_value=1),
                    mock.patch.object(strategy_finder, "root_command", side_effect=lambda command, **_kwargs: command) as root_command,
                    mock.patch.object(strategy_finder.subprocess, "Popen", side_effect=launch_child),
                ):
                    server = _start_captured_server(serve, config)
                    with server, _JobRunnerThreadTracker() as runner_threads:
                        start_status, _headers, start_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/start-run",
                            method="POST",
                            body=json.dumps({"mode": mode, "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                                "utf-8"
                            ),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(start_status, 202, start_body.decode("utf-8", errors="replace"))
                        accepted = json.loads(start_body.decode("utf-8"))
                        self.assertTrue(child_started.wait(timeout=2))
                        self.assertTrue(worker_finished.wait(timeout=2))
                        runner_threads.join_tracked()

                        state = read_state(config.output.state_dir)
                        history_status, _headers, history_body = _http_request(server.port, "/api/core/runs/history")
                        self.assertEqual(history_status, 200, history_body.decode("utf-8", errors="replace"))
                        history = json.loads(history_body.decode("utf-8"))
                        self.assertEqual([[blockcheck_path]], child_commands)
                        self.assertEqual(1, len(child_kwargs))
                        self.assertEqual(os.name != "nt", child_kwargs[0]["start_new_session"])
                        self.assertEqual(0, child.returncode)
                        self.assertIsNone(state["current_run_id"])
                        self.assertIsNone(state["last_error"])
                        self.assertEqual("success", state["last_run_status"])
                        self.assertEqual(accepted["run_id"], history["runs"][0]["run_id"])
                        self.assertEqual("success", history["runs"][0]["status"])
                        root_command.assert_called_once()
                        self.assertEqual(runner_threads.tracked_count, 1)

        for mode in ("standard", "multi_domain"):
            with self.subTest(mode=mode):
                run_without_stop(mode)


    def test_strategy_discovery_stop_after_actual_child_launch_terminates_and_cleans_up(self) -> None:
        def run_stop_after_launch(mode: str) -> None:
            blockcheck_path = str(Path("/test/blockcheck2.sh").resolve())
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                worker_finished = threading.Event()
                child_started = threading.Event()
                child_commands: list[list[str]] = []
                termination_calls: list[tuple[object, str]] = []
                cleanup_calls: list[str] = []
                original_popen = strategy_finder.subprocess.Popen

                class ControlledChild:
                    def __init__(self) -> None:
                        self.stdout = io.StringIO("")
                        self.returncode: int | None = None
                        self.terminated = threading.Event()

                    def wait(self, timeout: float | None = None) -> int:
                        if not self.terminated.is_set():
                            raise subprocess.TimeoutExpired(blockcheck_path, timeout)
                        assert self.returncode is not None
                        return self.returncode

                child = ControlledChild()
                original_runner = web_app.JobRunner

                class ObservingJobRunner(original_runner):
                    def _run(self, *args: object, **kwargs: object) -> None:
                        try:
                            super()._run(*args, **kwargs)
                        finally:
                            worker_finished.set()

                def launch_child(command: list[str], *args: object, **_kwargs: object) -> ControlledChild:
                    if command == [blockcheck_path]:
                        child_commands.append(command)
                        child_started.set()
                        return child
                    return original_popen(command, *args, **_kwargs)

                def terminate_child(process: object, run_id: str | None = None) -> None:
                    self.assertIs(process, child)
                    self.assertIsNotNone(run_id)
                    termination_calls.append((process, str(run_id)))
                    child.returncode = -15
                    child.terminated.set()

                def record_cleanup() -> None:
                    cleanup_calls.append("cleanup")

                with (
                    mock.patch.object(web_app, "JobRunner", ObservingJobRunner),
                    mock.patch.object(web_app, "recover_registered_process_runs", return_value=True),
                    mock.patch.object(
                        web_app,
                        "create_post_run_snapshot",
                        return_value={
                            "kind": "snapshot",
                            "status": "success",
                            "completed_at": "2026-08-12T00:00:00Z",
                            "snapshot_id": "post-run-snapshot",
                        },
                    ),
                    mock.patch.object(strategy_finder.shutil, "which", return_value=blockcheck_path),
                    mock.patch.object(strategy_finder, "_count_script_function_attempts", return_value=1),
                    mock.patch.object(strategy_finder, "root_command", side_effect=lambda command, **_kwargs: command) as root_command,
                    mock.patch.object(strategy_finder.subprocess, "Popen", side_effect=launch_child),
                    mock.patch.object(strategy_finder, "_stop_process_group", side_effect=terminate_child),
                    mock.patch.object(strategy_finder, "_cleanup_nft_blockcheck_tables", side_effect=record_cleanup),
                ):
                    server = _start_captured_server(serve, config)
                    with server, _JobRunnerThreadTracker() as runner_threads:
                        runner_threads.add_release_action(
                            "stop active JobRunner after child-launch test failure",
                            lambda: _stop_current_run_if_started(server.port, child_started, worker_finished),
                        )
                        start_status, _headers, start_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/start-run",
                            method="POST",
                            body=json.dumps({"mode": mode, "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                                "utf-8"
                            ),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(start_status, 202, start_body.decode("utf-8", errors="replace"))
                        accepted = json.loads(start_body.decode("utf-8"))
                        self.assertTrue(child_started.wait(timeout=2))

                        stop_status, _headers, stop_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/stop-current-run",
                            method="POST",
                            body=b"{}",
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(stop_status, 202, stop_body.decode("utf-8", errors="replace"))
                        self.assertEqual(accepted["run_id"], json.loads(stop_body.decode("utf-8"))["run_id"])
                        self.assertTrue(worker_finished.wait(timeout=2))
                        runner_threads.join_tracked()

                        state = read_state(config.output.state_dir)
                        history_status, _headers, history_body = _http_request(server.port, "/api/core/runs/history")
                        self.assertEqual(history_status, 200, history_body.decode("utf-8", errors="replace"))
                        history = json.loads(history_body.decode("utf-8"))
                        self.assertEqual([[blockcheck_path]], child_commands)
                        self.assertTrue(
                            child.terminated.is_set(),
                            f"termination_calls={termination_calls!r}, state={state!r}, history={history!r}",
                        )
                        self.assertEqual(-15, child.returncode)
                        self.assertTrue(child.stdout.closed)
                        self.assertEqual([(child, accepted["run_id"])], termination_calls)
                        self.assertEqual(["cleanup"], cleanup_calls)
                        self.assertIsNone(state["current_run_id"])
                        self.assertIsNone(state["last_error"])
                        self.assertEqual("stopped", state["last_run_status"])
                        self.assertEqual(accepted["run_id"], history["runs"][0]["run_id"])
                        self.assertEqual("stopped", history["runs"][0]["status"])
                        root_command.assert_called_once()
                        self.assertEqual(runner_threads.tracked_count, 1)

        for mode in ("standard", "multi_domain"):
            with self.subTest(mode=mode):
                run_stop_after_launch(mode)


    def test_core_strategy_discovery_start_run_rejects_unknown_protocols(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported protocols"):
            web_app._core_strategy_discovery_job_payload(
                {
                    "mode": "multi_domain",
                    "domains": ["youtube.com"],
                    "protocols": ["tcp", "bad"],
                }
            )


    def test_core_strategy_discovery_start_run_quic_only_disables_tcp_protocols(self) -> None:
        job_name, job_payload = web_app._core_strategy_discovery_job_payload(
            {
                "mode": "multi_domain",
                "domains": ["youtube.com"],
                "protocols": ["quic"],
                "settings": {
                    "curl_max_time_quic": 7,
                    "enable_ipv6": False,
                },
            }
        )

        self.assertEqual("zapret-multi-domain-discovery", job_name)
        self.assertEqual(["youtube.com"], job_payload["domains"])
        self.assertTrue(job_payload["include_quic"])
        self.assertFalse(job_payload["enable_http"])
        self.assertFalse(job_payload["enable_tls12"])
        self.assertFalse(job_payload["enable_tls13"])
        self.assertEqual(7, job_payload["curl_max_time_quic"])
        self.assertFalse(job_payload["enable_ipv6"])


    def test_core_strategy_discovery_start_run_rejects_empty_domain_list(self) -> None:
        with self.assertRaisesRegex(ValueError, "domains are required"):
            web_app._core_strategy_discovery_job_payload(
                {
                    "mode": "multi_domain",
                    "domains": ["", "   "],
                    "protocols": ["tcp"],
                }
            )


    def test_core_strategy_discovery_start_run_rejects_unknown_top_level_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported start-run fields: mode_settings"):
            web_app._core_strategy_discovery_job_payload(
                {
                    "mode": "multi_domain",
                    "domains": ["youtube.com"],
                    "mode_settings": {"common_strategy_min_domains": 2},
                }
            )


    def test_core_strategy_discovery_start_run_rejects_unknown_settings_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported start-run settings: mode_settings"):
            web_app._core_strategy_discovery_job_payload(
                {
                    "mode": "multi_domain",
                    "domains": ["youtube.com"],
                    "settings": {"mode_settings": {"common_strategy_min_domains": 2}},
                }
            )


    def test_core_strategy_candidates_require_filter_and_export_streams_full_facts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            config = AppConfig(output=OutputConfig(state_dir=state_dir))
            upsert_candidates(
                state_dir,
                {
                    "candidates": [
                        {"protocol": "tcp", "args": "--dpi-desync=fake", "domain": "youtube.com"},
                        {"protocol": "quic", "args": "--dpi-desync=fake", "domain": "discord.com"},
                    ]
                },
                {"id": "run-1"},
            )

            with self.assertRaisesRegex(ValueError, "filter is required"):
                web_app.core_api.strategy_candidates_payload(config, {})

            payload = web_app.core_api.strategy_candidates_payload(config, {"domain": ["youtube.com"]})
            exported = [
                json.loads(line.decode("utf-8"))
                for line in web_app.core_api.iter_strategy_candidates_export_lines(config, {})
            ]

            self.assertEqual(len(payload["candidates"]), 1)
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["filters"], {"domains": ["youtube.com"]})
            self.assertEqual(payload["candidates"][0]["seen"][0]["domain"], "youtube.com")
            self.assertEqual(len(exported), 2)
            self.assertEqual({item["protocol"] for item in exported}, {"tcp", "quic"})
            self.assertNotIn("has_more", payload)
            self.assertIn("id", payload["candidates"][0])
            self.assertIn("protocol", payload["candidates"][0])
            self.assertIn("seen", payload["candidates"][0])


    def test_service_status_reports_installation_shape_without_web_unit_hardcode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.dict(os.environ, {"GP_INSTALL_WEB": "off"}, clear=False):
                payload = web_app.service_api.service_status_payload(
                    config,
                    current_version="0.test",
                    runtime_role="core",
                )

            self.assertEqual(payload["state"], "active")
            self.assertEqual(payload["mode"], "core")
            self.assertNotIn("unit", payload)
            self.assertEqual(payload["services"]["core"]["name"], "gp-control-plane-core.service")
            self.assertEqual(payload["services"]["core"]["state"], "active")
            self.assertEqual(payload["services"]["web"]["state"], "disabled")
            self.assertFalse(payload["services"]["web"]["required"])
            self.assertIsInstance(payload["data_state"], dict)
            self.assertIn("v2fly", payload["data_state"])


    def test_service_status_reports_missing_v2fly_as_data_state_not_runtime_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.object(
                web_app.service_api,
                "v2fly_storage_status_payload",
                return_value={"state": "missing", "source_commit": "", "group_count": 0},
            ):
                payload = web_app.service_api.service_status_payload(
                    config,
                    current_version="0.test",
                    runtime_role="core",
                    web_enabled=True,
                )

            self.assertEqual(payload["state"], "active")
            self.assertEqual(payload["data_state"]["state"], "missing")
            self.assertEqual(payload["data_state"]["v2fly"]["state"], "missing")
            self.assertEqual(payload["data_state"]["v2fly"]["group_count"], 0)


    def test_service_install_identity_environment_overrides_restored_settings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            state = read_state(config.output.state_dir)
            state["settings"] = {"installed_ref": "v0.4.0"}
            write_state(config.output.state_dir, state)
            release = {
                "channel": "stable",
                "available_version": "v0.4.1",
                "url": "https://example.invalid/release",
                "published_at": "",
            }
            with (
                mock.patch.dict(
                    os.environ,
                    {"GP_INSTALLED_REF": "candidate:" + "a" * 40, "GP_INSTALLED_COMMIT": "a" * 40},
                    clear=False,
                ),
                mock.patch.object(web_app.service_api, "release_channel_info", return_value=release),
            ):
                status = web_app.service_api.service_status_payload(config, current_version="0.4.1")
                available = web_app.service_api.available_releases_payload(
                    {"installed_ref": "v0.4.0"}, current_version="0.4.1"
                )

            expected_ref = "candidate:" + "a" * 40
            self.assertEqual(expected_ref, status["version"]["installed_ref"])
            self.assertEqual("a" * 40, status["version"]["commit"])
            self.assertEqual(expected_ref, available["current"]["installed_ref"])
            self.assertEqual("a" * 40, available["current"]["commit"])

            with (
                mock.patch.dict(os.environ, {"GP_INSTALLED_REF": "", "GP_INSTALLED_COMMIT": ""}, clear=False),
                mock.patch.object(web_app.service_api, "release_channel_info", return_value=release),
            ):
                fallback_status = web_app.service_api.service_status_payload(config, current_version="0.4.1")
                fallback_available = web_app.service_api.available_releases_payload(
                    {"installed_ref": "v0.4.0"}, current_version="0.4.1"
                )
            self.assertEqual("v0.4.0", fallback_status["version"]["installed_ref"])
            self.assertEqual("", fallback_status["version"]["commit"])
            self.assertEqual("v0.4.0", fallback_available["current"]["installed_ref"])
            self.assertEqual("", fallback_available["current"]["commit"])


    def test_run_preferences_endpoint_saves_last_finder_form(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps(
                {
                    "run_preferences": {
                        "domains": ["youtube.com", "discord.com"],
                        "domain_preset": "custom",
                        "discovery_profile": "custom",
                        "settings_preset": "accelerated",
                        "run_mode": "multi",
                        "curl_parallelism": 19,
                        "enable_http": True,
                        "enable_tls12": True,
                        "enable_tls13": False,
                        "include_quic": True,
                        "enable_ipv6": False,
                        "scan_level": "force",
                        "repeats": 2,
                        "repeat_parallel": True,
                        "skip_dnscheck": False,
                        "skip_ipblock": True,
                        "limit_time_enabled": True,
                        "timeout_hours": 3.5,
                    }
                }
            )
            connection.request("POST", "/api/internal/web-views/run-preferences", body=body, headers=_authenticated_headers(port, {"Content-Type": "application/json"}))
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"run_mode":"multi"', saved)
            self.assertIn('"curl_parallelism":19', saved)
            self.assertIn('"scan_level":"force"', saved)
            self.assertIn('"timeout_hours":3.5', saved)
            self.assertNotIn('"settings_preset"', saved)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/internal/web-views/run-preferences", headers=_authenticated_headers(port))
            response = connection.getresponse()
            status = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"run_preferences"', status)
            self.assertIn('"youtube.com"', status)
            self.assertIn('"discord.com"', status)
            self.assertNotIn('"settings_preset"', status)


    def test_web_preset_domain_endpoints_save_and_page_domains(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"scope": "finder", "name": "mine", "domains": ["youtube.com", "discord.com", "discordcdn.com"]})
            connection.request("POST", "/api/internal/web-views/preset-save", body=body, headers=_authenticated_headers(port, {"Content-Type": "application/json"}))
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn('"mine"', saved)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/internal/web-views/preset-domains?scope=finder&name=mine&limit=2", headers=_authenticated_headers(port))
            response = connection.getresponse()
            page = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn('"total":3', page)
            self.assertIn('"has_more":true', page)


    def test_system_preset_api_allows_empty_save_and_user_only_delete(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"scope": "finder", "name": "required", "kind": "system", "domains": []})
            connection.request("POST", "/api/internal/web-views/preset-save", body=body, headers=_authenticated_headers(port, {"Content-Type": "application/json"}))
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"system"', saved)
            self.assertIn('"required":[]', saved)
            self.assertIn('"enabled_count":0', saved)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"scope": "finder", "name": "mine", "domains": ["youtube.com"]})
            connection.request("POST", "/api/internal/web-views/preset-save", body=body, headers=_authenticated_headers(port, {"Content-Type": "application/json"}))
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 200)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"scope": "finder", "names": ["mine", "required"]})
            connection.request(
                "POST",
                "/api/internal/web-views/preset-delete-user-lists",
                body=body,
                headers=_authenticated_headers(port, {"Content-Type": "application/json"}),
            )
            response = connection.getresponse()
            deleted = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertNotIn('"mine"', deleted)
            self.assertIn('"required":[]', deleted)
            self.assertIn('"kind":"system"', deleted)


    def test_discovery_profiles_endpoint_is_removed(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps(
                {
                    "profiles": {
                        "night-test": {
                            "title": "Night test",
                            "enable_http": True,
                            "enable_tls12": True,
                            "enable_tls13": True,
                            "include_quic": True,
                            "scan_level": "force",
                            "repeats": 99,
                            "curl_parallelism": 99,
                            "limit_time_enabled": True,
                            "timeout_hours": 99,
                        },
                        "standard": {
                            "title": "Changed built-in",
                            "enable_tls12": False,
                        },
                    }
                }
            )
            connection.request("POST", "/api/discovery-profiles", body=body, headers=_authenticated_headers(port, {"Content-Type": "application/json"}))
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()

            self.assertIn(response.status, {404, 405})
            self.assertNotIn('"night-test"', saved)
            self.assertNotIn("Changed built-in", saved)



class _CleanupFailureRecords(AssertionError):
    """Structured leaf cleanup failures that survive nested cleanup scopes."""

    def __init__(self, records: list[tuple[str, BaseException]]) -> None:
        self.records = tuple(records)
        super().__init__(
            "\n".join(
                f"Cleanup failed during {description}: {error!r}"
                for description, error in self.records
            )
        )


def _cleanup_failure_records(error: BaseException, description: str) -> tuple[tuple[str, BaseException], ...]:
    if isinstance(error, _CleanupFailureRecords):
        return error.records
    return ((description, error),)


class _CleanupActions:
    """Run every cleanup action without hiding the test's primary failure."""

    def __init__(self) -> None:
        self._actions: list[tuple[str, Any]] = []

    def add(self, description: str, action: Any) -> None:
        self._actions.append((description, action))

    def run(self, primary_error: BaseException | None = None) -> None:
        cleanup_failures: list[tuple[str, BaseException]] = []
        for description, action in reversed(self._actions):
            try:
                action()
            except BaseException as error:
                cleanup_failures.extend(_cleanup_failure_records(error, description))
        if not cleanup_failures:
            return
        if primary_error is not None:
            for description, error in cleanup_failures:
                primary_error.add_note(f"Cleanup also failed during {description}: {error!r}")
            return
        raise _CleanupFailureRecords(cleanup_failures)


@contextlib.contextmanager
def _cleanup_scope() -> Any:
    cleanup = _CleanupActions()
    primary_error: BaseException | None = None
    try:
        yield cleanup
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup.run(primary_error)


class _CapturedTestServer:
    def __init__(
        self,
        port: int,
        server: Any,
        thread: threading.Thread,
        listener_registry: _CapturedListenerRegistry | None = None,
    ) -> None:
        self.port = port
        self._server = server
        self._thread = thread
        self._listener_registry = listener_registry
        self._close_lock = threading.Lock()
        self._closed = False

    def __enter__(self) -> _CapturedTestServer:
        return self

    def __exit__(self, _exc_type: Any, exc_value: BaseException | None, _traceback: Any) -> None:
        cleanup = _CleanupActions()
        cleanup.add("captured HTTP server close", self.close)
        cleanup.run(exc_value)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

        listeners = self._owned_listeners()

        def join_server_thread() -> None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise AssertionError(f"server thread did not stop on port {self.port}")

        def close_listener_after_handlers(listener: Any, index: int) -> None:
            if not self._listener_started_serving(listener):
                try:
                    socketserver.TCPServer.server_close(listener)
                except BaseException as error:
                    raise _CleanupFailureRecords(
                        _cleanup_failure_records(
                            error,
                            f"captured HTTP raw listener close listener {index}",
                        )
                    ) from error
                self._release_listener_after_close(listener)
                return

            cleanup_failures: list[tuple[str, BaseException]] = []
            try:
                self._wait_for_request_handlers(listener)
            except BaseException as error:
                cleanup_failures.extend(
                    _cleanup_failure_records(
                        error,
                        f"captured HTTP request handler wait listener {index}",
                    )
                )
                # ThreadingMixIn.server_close() joins every registered request
                # handler without a timeout. The bounded wait is the diagnostic
                # boundary, so only close the listening socket on this path.
                try:
                    socketserver.TCPServer.server_close(listener)
                except BaseException as error:
                    cleanup_failures.extend(
                        _cleanup_failure_records(
                            error,
                            f"captured HTTP raw listener close listener {index}",
                        )
                    )
                else:
                    self._release_listener_after_close(listener)
                raise _CleanupFailureRecords(cleanup_failures)
            try:
                listener.server_close()
            except BaseException:
                raise
            self._release_listener_after_close(listener)

        try:
            with _cleanup_scope() as cleanup:
                # Add in creation order so every listener phase runs in LIFO order.
                for index, listener in enumerate(listeners, start=1):
                    cleanup.add(
                        f"captured HTTP listener close listener {index}",
                        lambda listener=listener, index=index: close_listener_after_handlers(listener, index),
                    )
                for index, listener in enumerate(listeners, start=1):
                    if self._listener_started_serving(listener):
                        cleanup.add(
                            f"captured HTTP active connection close listener {index}",
                            listener.close_active_request_connections,
                        )
                cleanup.add("captured HTTP server thread join", join_server_thread)
                for index, listener in enumerate(listeners, start=1):
                    if self._listener_started_serving(listener):
                        cleanup.add(f"captured HTTP server shutdown listener {index}", listener.shutdown)
        finally:
            self._finalize_owned_listeners()

    def close_active_request_connections(self) -> None:
        self._server.close_active_request_connections()

    def _owned_listeners(self) -> tuple[Any, ...]:
        if self._listener_registry is None:
            return (self._server,)
        self._listener_registry.abandon()
        return self._listener_registry.snapshot()

    def _release_listener_after_close(self, listener: Any) -> None:
        if self._listener_registry is not None:
            self._listener_registry.release(listener)

    def _finalize_owned_listeners(self) -> None:
        if self._listener_registry is not None:
            self._listener_registry.finalize()

    @staticmethod
    def _listener_started_serving(listener: Any) -> bool:
        serving_started = getattr(listener, "serve_forever_started", None)
        return serving_started is None or bool(serving_started.is_set())

    def _wait_for_request_handlers(self, listener: Any, timeout: float = 5) -> None:
        if listener.request_handlers_idle.wait(timeout):
            return
        port = self.port if self._listener_registry is None else listener.server_address[1]
        raise AssertionError(
            f"request handlers did not stop on port {port}: "
            f"{listener.active_request_handler_count} still active"
        )


class _CapturedListenerRegistry:
    def __init__(self, startup_abandoned: threading.Event) -> None:
        self._startup_abandoned = startup_abandoned
        self._lock = threading.Lock()
        self._listeners: list[Any] = []

    def register(self, listener: Any) -> bool:
        with self._lock:
            self._listeners.append(listener)
            return not self._startup_abandoned.is_set()

    def abandon(self) -> None:
        with self._lock:
            self._startup_abandoned.set()

    def snapshot(self) -> tuple[Any, ...]:
        with self._lock:
            return tuple(self._listeners)

    def release(self, listener: Any) -> None:
        with self._lock:
            self._listeners.remove(listener)

    def finalize(self) -> None:
        with self._lock:
            self._listeners.clear()

    def close_after_abandonment(self, listener: Any) -> None:
        claim_index = self._claim(listener)
        if claim_index is None:
            return
        try:
            socketserver.TCPServer.server_close(listener)
        except BaseException as error:
            self._requeue(listener, claim_index)
            raise _CleanupFailureRecords(
                _cleanup_failure_records(error, "captured server abandoned listener raw close")
            ) from error

    def close_all(self, phase: str, *, final: bool = False) -> None:
        listeners = self.snapshot()

        try:
            with _cleanup_scope() as cleanup:
                # Register in creation order so _CleanupActions closes in LIFO order.
                for listener in listeners:
                    cleanup.add(
                        f"{phase} listener {listener.server_address!r}",
                        lambda listener=listener: self._close_claimed(listener),
                    )
        finally:
            if final:
                self.finalize()

    def _claim(self, listener: Any) -> int | None:
        with self._lock:
            for index, pending_listener in enumerate(self._listeners):
                if pending_listener is listener:
                    self._listeners.pop(index)
                    return index
        return None

    def _requeue(self, listener: Any, index: int) -> None:
        with self._lock:
            self._listeners.insert(min(index, len(self._listeners)), listener)

    def _close_claimed(self, listener: Any) -> None:
        claim_index = self._claim(listener)
        if claim_index is None:
            return
        try:
            self._close(listener)
        except BaseException:
            self._requeue(listener, claim_index)
            raise

    @staticmethod
    def _close(listener: Any) -> None:
        serving_started = getattr(listener, "serve_forever_started", None)
        if serving_started is None or not serving_started.is_set():
            socketserver.TCPServer.server_close(listener)
            return

        with _cleanup_scope() as cleanup:
            cleanup.add("serving listener close", listener.server_close)
            cleanup.add("serving listener active connection close", listener.close_active_request_connections)
            cleanup.add("serving listener shutdown", listener.shutdown)


class _JobRunnerThreadTracker:
    """Scoped capture of JobRunner worker threads without changing production code."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._release_actions: list[tuple[str, Any]] = []
        self._joined_thread_ids: set[int] = set()
        self._patch: Any | None = None

    def __enter__(self) -> _JobRunnerThreadTracker:
        original_threading = jobs.threading
        tracker = self

        class ThreadingProxy:
            def Thread(self, *args: Any, **kwargs: Any) -> threading.Thread:
                thread = original_threading.Thread(*args, **kwargs)
                if tracker._is_job_runner_worker(kwargs.get("target")):
                    tracker._register(thread)
                return thread

            def __getattr__(self, name: str) -> Any:
                return getattr(original_threading, name)

        self._patch = mock.patch.object(jobs, "threading", ThreadingProxy())
        self._patch.start()
        return self

    def __exit__(self, _exc_type: Any, exc_value: BaseException | None, _traceback: Any) -> None:
        assert self._patch is not None
        cleanup = _CleanupActions()
        cleanup.add("restore JobRunner threading module", self._patch.stop)
        cleanup.add("JobRunner worker thread join", self.join_tracked)
        for description, action in self._release_actions:
            cleanup.add(description, action)
        cleanup.run(exc_value)

    @property
    def threads(self) -> tuple[threading.Thread, ...]:
        with self._lock:
            return tuple(self._threads)

    @property
    def tracked_count(self) -> int:
        with self._lock:
            return len(self._threads)

    def release_barrier(self, barrier: threading.Event, description: str = "release JobRunner test barrier") -> None:
        self._release_actions.append((description, barrier.set))

    def add_release_action(self, description: str, action: Any) -> None:
        self._release_actions.append((description, action))

    def join_tracked(self) -> None:
        while True:
            with self._lock:
                pending = [thread for thread in self._threads if id(thread) not in self._joined_thread_ids]
            if not pending:
                return
            for thread in pending:
                if thread is threading.current_thread():
                    raise AssertionError("JobRunner tracker attempted to join its current thread")
                try:
                    thread.join(timeout=2)
                except BaseException as error:
                    raise AssertionError(f"JobRunner worker thread could not be joined: {thread!r}") from error
                if thread.is_alive():
                    raise AssertionError(f"JobRunner worker thread did not stop: {thread!r}")
                with self._lock:
                    self._joined_thread_ids.add(id(thread))

    @staticmethod
    def _is_job_runner_worker(target: Any) -> bool:
        return (
            getattr(target, "__name__", "") == "_run"
            and isinstance(getattr(target, "__self__", None), jobs.JobRunner)
        )

    def _register(self, thread: threading.Thread) -> None:
        with self._lock:
            self._threads.append(thread)


@contextlib.contextmanager
def _captured_server_temporary_directory() -> Any:
    with tempfile.TemporaryDirectory() as raw:
        with _cleanup_scope() as cleanup:
            def start_server(function: Any, config: AppConfig, **kwargs: Any) -> _CapturedTestServer:
                server = _start_captured_server(function, config, **kwargs)
                cleanup.add("captured HTTP server close", server.close)
                return server

            yield raw, start_server


def _is_expected_socket_teardown_error(error: OSError) -> bool:
    return error.errno in {
        errno.EBADF,
        errno.ECONNABORTED,
        errno.ECONNRESET,
        errno.ENOTCONN,
    } or getattr(error, "winerror", None) in {
        10038,  # WSAENOTSOCK: already closed.
        10053,  # WSAECONNABORTED: connection aborted.
        10054,  # WSAECONNRESET: connection reset.
        10057,  # WSAENOTCONN: no longer connected.
    }


def _start_captured_server(
    function: Any,
    config: AppConfig,
    *,
    _startup_timeout: float = 5,
    _after_constructor_abandon_check: Any | None = None,
    _server_type: type[Any] | None = None,
    **kwargs: Any,
) -> _CapturedTestServer:
    module = sys.modules[function.__module__]
    if function.__name__ in {"serve", "serve_core"} and function.__module__ in {
        "gp_control_plane.http.api_server",
        "gp_control_plane.http.core_server",
    }:
        # A6 moves the Core listener onto the same explicit Cheroot runtime as
        # Web.  This test-only lifecycle owner exercises the real public Core
        # factory instead of preserving a parallel BaseHTTP test listener.
        from gp_control_plane.web.core_runtime import create_core_runtime

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = int(reservation.getsockname()[1])
        # Linux may reuse a port from an earlier fixture with a different auth
        # store. A bearer belongs to that server lifetime, not to the port.
        _BEARER_AUTHORIZATION_BY_PORT.pop(port, None)
        runtime = create_core_runtime(
            config,
            "127.0.0.1",
            port,
            ui_enabled=False,
        )
        thread = threading.Thread(target=runtime.serve_forever, daemon=True)
        thread.start()
        captured = _CapturedTestServer(port, runtime, thread)
        try:
            deadline = time.monotonic() + _startup_timeout
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise AssertionError(f"Cheroot Core listener did not start on port {port}")
                    time.sleep(0.01)
        except BaseException:
            captured.close()
            raise
        return captured

    server_type = _server_type or getattr(module, "ThreadingHTTPServer")
    server_modules = (
        module,
        importlib.import_module("gp_control_plane.http.api_server"),
    )
    startup_abandoned = threading.Event()
    listeners = _CapturedListenerRegistry(startup_abandoned)
    startup_condition = threading.Condition()
    listener_constructed = False
    serving_listener: Any | None = None
    startup_finished = False
    startup_errors: list[BaseException] = []

    class CapturingThreadingHTTPServer(server_type):
        def __init__(self, *args: Any, **server_kwargs: Any) -> None:
            server_address, request_handler_class, *server_args = args
            server_address = (server_address[0], 0)

            class CapturingRequestHandler(request_handler_class):
                def handle_one_request(self) -> None:
                    super().handle_one_request()
                    if getattr(self, "path", "").split("?", 1)[0] == "/api/web/events/stream":
                        self.close_connection = True

            super().__init__(server_address, CapturingRequestHandler, *server_args, **server_kwargs)
            if not listeners.register(self):
                # The registry owns this listener before observing abandonment.
                # A failed early raw close therefore remains available to the
                # post-join drain instead of disappearing from cleanup.
                listeners.close_after_abandonment(self)
                raise RuntimeError("captured server startup was abandoned during listener registration")
            with startup_condition:
                nonlocal listener_constructed
                listener_constructed = True
                startup_condition.notify_all()
            if _after_constructor_abandon_check is not None:
                _after_constructor_abandon_check(self, startup_abandoned)
            self._request_handlers_lock = threading.Lock()
            self._active_request_handler_count = 0
            self._active_request_sockets: set[socket.socket] = set()
            self.request_handlers_idle = threading.Event()
            self.request_handlers_idle.set()
            self.serve_forever_started = threading.Event()

        def serve_forever(self, *args: Any, **server_kwargs: Any) -> None:
            if startup_abandoned.is_set():
                return
            self.serve_forever_started.set()
            with startup_condition:
                nonlocal serving_listener
                serving_listener = self
                startup_condition.notify_all()
            super().serve_forever(*args, **server_kwargs)

        @property
        def active_request_handler_count(self) -> int:
            with self._request_handlers_lock:
                return self._active_request_handler_count

        def process_request(self, request: Any, client_address: Any) -> None:
            with self._request_handlers_lock:
                self._active_request_handler_count += 1
                self._active_request_sockets.add(request)
                self.request_handlers_idle.clear()
            try:
                super().process_request(request, client_address)
            except BaseException:
                # ThreadingMixIn starts the worker in process_request().  If that
                # fails, no worker finally block will release this reservation.
                with self._request_handlers_lock:
                    self._active_request_handler_count -= 1
                    self._active_request_sockets.discard(request)
                    if self._active_request_handler_count == 0:
                        self.request_handlers_idle.set()
                raise

        def process_request_thread(self, request: Any, client_address: Any) -> None:
            try:
                super().process_request_thread(request, client_address)
            finally:
                with self._request_handlers_lock:
                    self._active_request_handler_count -= 1
                    self._active_request_sockets.discard(request)
                    if self._active_request_handler_count == 0:
                        self.request_handlers_idle.set()

        def close_active_request_connections(self) -> None:
            with self._request_handlers_lock:
                active_request_sockets = tuple(self._active_request_sockets)
            cleanup_failures: list[tuple[str, BaseException]] = []

            for index, request in enumerate(active_request_sockets, start=1):
                try:
                    request.shutdown(socket.SHUT_RDWR)
                except OSError as error:
                    if not _is_expected_socket_teardown_error(error):
                        cleanup_failures.extend(
                            _cleanup_failure_records(
                                error,
                                f"captured HTTP active request socket {index} shutdown",
                            )
                        )
                try:
                    request.close()
                except OSError as error:
                    if not _is_expected_socket_teardown_error(error):
                        cleanup_failures.extend(
                            _cleanup_failure_records(
                                error,
                                f"captured HTTP active request socket {index} close",
                            )
                        )
            if cleanup_failures:
                raise _CleanupFailureRecords(cleanup_failures)

    def run() -> None:
        nonlocal startup_finished
        try:
            with contextlib.ExitStack() as patches:
                patched_modules: set[int] = set()
                for server_module in server_modules:
                    if id(server_module) in patched_modules:
                        continue
                    patched_modules.add(id(server_module))
                    patches.enter_context(
                        mock.patch.object(server_module, "ThreadingHTTPServer", CapturingThreadingHTTPServer)
                    )
                function(config, "127.0.0.1", 0, **kwargs)
        except BaseException as error:
            with startup_condition:
                startup_errors.append(error)
                startup_condition.notify_all()
        finally:
            with startup_condition:
                startup_finished = True
                startup_condition.notify_all()

    def abort_startup() -> BaseException | None:
        listeners.abandon()

        def join_startup_thread() -> None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise AssertionError("server startup thread did not stop")

        cleanup = _CleanupActions()
        # LIFO execution produces the required pre-join then post-join sweep.
        cleanup.add(
            "captured server post-join listener close",
            lambda: listeners.close_all("post-join", final=True),
        )
        cleanup.add("captured server startup thread join", join_startup_thread)
        cleanup.add("captured server pre-join listener close", lambda: listeners.close_all("pre-join"))
        try:
            cleanup.run()
        except BaseException as error:
            return error
        return None

    def raise_startup_failure(message: str, cause: BaseException | None = None) -> None:
        cleanup_error = abort_startup()
        if cause is None:
            with startup_condition:
                if startup_errors:
                    cause = startup_errors[0]
        failure = AssertionError(message)
        if cleanup_error is not None:
            for description, error in _cleanup_failure_records(cleanup_error, "captured server startup cleanup"):
                failure.add_note(f"Captured server startup cleanup also failed during {description}: {error!r}")
        if cause is not None:
            raise failure from cause
        raise failure

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    startup_deadline = time.monotonic() + _startup_timeout
    startup_failure: tuple[str, BaseException | None] | None = None
    with startup_condition:
        while not listener_constructed and not startup_errors and not startup_finished:
            remaining = startup_deadline - time.monotonic()
            if remaining <= 0:
                break
            startup_condition.wait(timeout=remaining)
        if startup_errors:
            startup_failure = ("server failed during startup", startup_errors[0])
        elif not listener_constructed:
            startup_failure = ("server did not construct", None)
        while startup_failure is None and serving_listener is None and not startup_errors and not startup_finished:
            remaining = startup_deadline - time.monotonic()
            if remaining <= 0:
                break
            startup_condition.wait(timeout=remaining)
        if startup_failure is None and startup_errors:
            startup_failure = ("server failed during startup", startup_errors[0])
        elif startup_failure is None and serving_listener is None:
            message = "server exited during startup" if startup_finished else "server did not start serving"
            startup_failure = (message, None)

    if startup_failure is not None:
        raise_startup_failure(*startup_failure)

    if serving_listener is None:
        raise_startup_failure("server listener registry was empty")
    port = int(serving_listener.server_address[1])
    return _CapturedTestServer(port, serving_listener, thread, listeners)


def _captured_active_socket_failure_server(sockets: tuple[Any, ...]) -> _CapturedTestServer:
    """Test capture-helper leaf aggregation without the retired HTTP stack."""

    class Listener:
        def __init__(self) -> None:
            self._request_handlers_lock = threading.Lock()
            self._active_request_sockets = set(sockets)
            self.request_handlers_idle = threading.Event()
            self.request_handlers_idle.set()
            self.active_request_handler_count = 0
            self.serve_forever_started = threading.Event()
            self.serve_forever_started.set()

        def shutdown(self) -> None:
            return

        def server_close(self) -> None:
            return

        def close_active_request_connections(self) -> None:
            failures: list[tuple[str, BaseException]] = []
            with self._request_handlers_lock:
                active = tuple(self._active_request_sockets)
            for index, active_socket in enumerate(active, start=1):
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except OSError as error:
                    if not _is_expected_socket_teardown_error(error):
                        failures.extend(_cleanup_failure_records(error, f"captured HTTP active request socket {index} shutdown"))
                try:
                    active_socket.close()
                except OSError as error:
                    if not _is_expected_socket_teardown_error(error):
                        failures.extend(_cleanup_failure_records(error, f"captured HTTP active request socket {index} close"))
            if failures:
                raise _CleanupFailureRecords(failures)

    thread = mock.Mock()
    thread.is_alive.return_value = False
    return _CapturedTestServer(12345, Listener(), thread)


def _close_sse_stream(
    connection: http.client.HTTPConnection | None, response: http.client.HTTPResponse | None
) -> None:
    try:
        if connection is not None and connection.sock is not None:
            linger_format = "HH" if os.name == "nt" else "ii"
            connection.sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_LINGER,
                struct.pack(linger_format, 1, 0),
            )
        if response is not None:
            response.close()
    finally:
        if connection is not None:
            connection.close()


def _stop_current_run_if_started(port: int, worker_started: threading.Event, worker_finished: threading.Event) -> None:
    if not worker_started.is_set() or worker_finished.is_set():
        return
    status, _headers, body = _http_request(
        port,
        "/api/core/strategy-discovery/stop-current-run",
        method="POST",
        body=b"{}",
        headers={"Content-Type": "application/json"},
    )
    if status != 202:
        raise AssertionError(f"test cleanup could not stop active JobRunner: HTTP {status}: {body!r}")



@contextlib.contextmanager
def _reserved_unavailable_loopback_port() -> Any:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        # Holding a bound-but-not-listening socket guarantees connection refusal
        # while preventing another test or process from claiming this port.
        yield int(sock.getsockname()[1])


def _openapi_test_request(
    openapi_contract: dict[str, Any],
    path: str,
    method: str,
    operation: dict[str, Any],
    snapshot_id: str,
) -> tuple[str, bytes | None, dict[str, str]]:
    request_path = path
    if path == "/api/core/backups/download-archive":
        request_path = f"{path}?snapshot_id={snapshot_id}"
    elif path == "/api/core/clean-install-vaults/create":
        # Do not create a device-local vault from the generic OpenAPI route
        # walker: the route contract is exercised through its documented
        # fail-closed invalid-request response here and targeted vault tests.
        return request_path, b'{"unexpected":true}', {"Content-Type": "application/json"}
    elif path == "/api/core/presets/v2fly/category-domains":
        request_path = f"{path}?category=missing"
    elif path == "/api/core/strategy-candidates":
        request_path = f"{path}?domain=youtube.com"
    elif path == "/api/web/presets/domains":
        request_path = f"{path}?scope=finder&name=required&kind=system&limit=2"

    if method != "POST":
        return request_path, None, {}
    content = ((operation.get("requestBody") or {}).get("content") or {})
    if "application/zip" in content:
        return request_path, b"not-a-zip", {"Content-Type": "application/zip"}
    if "multipart/form-data" in content:
        return request_path, b"not-a-zip", {"Content-Type": "application/zip"}
    json_body = content.get("application/json")
    if not isinstance(json_body, dict):
        return request_path, None, {}
    payload = _openapi_first_example_value(openapi_contract, json_body.get("examples") or {})
    if payload is None:
        payload = {}
    return request_path, json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"}


def _openapi_first_example_value(openapi_contract: dict[str, Any], examples: dict[str, Any]) -> Any:
    if not examples:
        return None
    example = next(iter(examples.values()))
    if isinstance(example, dict) and "$ref" in example:
        example = _openapi_resolve_ref(openapi_contract, str(example["$ref"]))
    if isinstance(example, dict) and "value" in example:
        return example["value"]
    return None


def _openapi_resolve_ref(openapi_contract: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported OpenAPI ref: {ref}")
    node: Any = openapi_contract
    for part in ref[2:].split("/"):
        node = node[part]
    return node



def _assert_openapi_response_contract(
    test_case: unittest.TestCase,
    openapi_contract: dict[str, Any],
    operation: dict[str, Any],
    status: int,
    headers: dict[str, str],
    body: bytes,
    *,
    context: object,
) -> None:
    responses = operation.get("responses")
    test_case.assertIsInstance(responses, dict, context)
    response = responses.get(str(status)) or responses.get("default")
    test_case.assertIsNotNone(
        response,
        (*context, f"HTTP {status} has no documented response") if isinstance(context, tuple) else context,
    )
    response = _openapi_resolve_document_node(openapi_contract, response)
    content = response.get("content") or {}
    if not content:
        test_case.assertEqual(body, b"", context)
        return

    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    test_case.assertIn(content_type, content, context)
    if content_type != "application/json":
        return

    media = content[content_type]
    schema = media.get("schema")
    test_case.assertIsInstance(schema, dict, context)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        test_case.fail(f"{context}: documented JSON response is not valid JSON: {error}")
    _assert_openapi_schema(test_case, openapi_contract, schema, payload, path="$")


def _openapi_resolve_document_node(openapi_contract: dict[str, Any], node: Any) -> Any:
    while isinstance(node, dict) and "$ref" in node:
        resolved = _openapi_resolve_ref(openapi_contract, str(node["$ref"]))
        if len(node) == 1:
            node = resolved
        else:
            node = {**resolved, **{key: value for key, value in node.items() if key != "$ref"}}
    return node


def _assert_openapi_schema(
    test_case: unittest.TestCase,
    openapi_contract: dict[str, Any],
    schema: Any,
    value: Any,
    *,
    path: str,
) -> None:
    schema = _openapi_resolve_document_node(openapi_contract, schema)
    if schema is True:
        return
    test_case.assertIsInstance(schema, dict, f"{path}: invalid OpenAPI schema")
    if schema is False:
        test_case.fail(f"{path}: value is forbidden by the documented schema")

    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword not in schema:
            continue
        choices = schema[keyword]
        test_case.assertIsInstance(choices, list, f"{path}: {keyword} must be an array")
        if keyword == "allOf":
            for index, choice in enumerate(choices):
                _assert_openapi_schema(test_case, openapi_contract, choice, value, path=f"{path}.allOf[{index}]")
            continue
        matches = 0
        failures: list[str] = []
        for index, choice in enumerate(choices):
            try:
                _assert_openapi_schema(test_case, openapi_contract, choice, value, path=f"{path}.{keyword}[{index}]")
            except AssertionError as error:
                failures.append(str(error))
            else:
                matches += 1
        if keyword == "anyOf":
            test_case.assertGreater(matches, 0, f"{path}: no anyOf schema matched: {failures}")
        else:
            test_case.assertEqual(matches, 1, f"{path}: expected exactly one oneOf match, got {matches}: {failures}")

    if "const" in schema:
        test_case.assertEqual(value, schema["const"], f"{path}: const mismatch")
    if "enum" in schema:
        test_case.assertIn(value, schema["enum"], f"{path}: value is not in enum")

    schema_type = schema.get("type")
    if schema_type is not None:
        expected_types = schema_type if isinstance(schema_type, list) else [schema_type]
        test_case.assertTrue(
            any(_openapi_value_matches_type(value, expected_type) for expected_type in expected_types),
            f"{path}: expected {expected_types}, got {type(value).__name__}",
        )

    if isinstance(value, str):
        if "minLength" in schema:
            test_case.assertGreaterEqual(len(value), schema["minLength"], f"{path}: shorter than minLength")
        if "maxLength" in schema:
            test_case.assertLessEqual(len(value), schema["maxLength"], f"{path}: longer than maxLength")
        if "pattern" in schema:
            test_case.assertIsNotNone(re.search(schema["pattern"], value), f"{path}: pattern mismatch")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema:
            test_case.assertGreaterEqual(value, schema["minimum"], f"{path}: below minimum")
        if "maximum" in schema:
            test_case.assertLessEqual(value, schema["maximum"], f"{path}: above maximum")
        if "exclusiveMinimum" in schema:
            test_case.assertGreater(value, schema["exclusiveMinimum"], f"{path}: below exclusiveMinimum")
        if "exclusiveMaximum" in schema:
            test_case.assertLess(value, schema["exclusiveMaximum"], f"{path}: above exclusiveMaximum")

    if isinstance(value, list):
        if "minItems" in schema:
            test_case.assertGreaterEqual(len(value), schema["minItems"], f"{path}: fewer than minItems")
        if "maxItems" in schema:
            test_case.assertLessEqual(len(value), schema["maxItems"], f"{path}: more than maxItems")
        if schema.get("uniqueItems"):
            test_case.assertEqual(len(value), len({json.dumps(item, sort_keys=True) for item in value}), f"{path}: items are not unique")
        if "items" in schema:
            for index, item in enumerate(value):
                _assert_openapi_schema(test_case, openapi_contract, schema["items"], item, path=f"{path}[{index}]")

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        for name in required:
            test_case.assertIn(name, value, f"{path}: missing required property {name!r}")
        for name, item in value.items():
            if name in properties:
                _assert_openapi_schema(test_case, openapi_contract, properties[name], item, path=f"{path}.{name}")
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                test_case.fail(f"{path}: unexpected property {name!r}")
            if isinstance(additional, dict):
                _assert_openapi_schema(test_case, openapi_contract, additional, item, path=f"{path}.{name}")


def _openapi_value_matches_type(value: Any, expected_type: str) -> bool:
    return {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
    }.get(expected_type, False)


def _http_request(
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5,
    authenticated: bool = True,
) -> tuple[int, dict[str, str], bytes]:
    request_headers = dict(headers or {})
    if authenticated and _requires_bearer_token(path):
        request_headers = _authenticated_headers(port, request_headers)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        response_body = response.read()
        return response.status, response_headers, response_body
    finally:
        connection.close()

def _http_sse_first_event(
    port: int,
    path: str,
    *,
    timeout: float = 5,
) -> tuple[int, dict[str, str], str, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(
            "GET",
            path,
            headers={"Accept": "text/event-stream", "Authorization": _bearer_authorization(port, timeout=timeout)},
        )
        response = connection.getresponse()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        first_line = response.readline().decode("utf-8").strip()
        second_line = response.readline().decode("utf-8").strip()
        return response.status, response_headers, first_line, second_line
    finally:
        connection.close()


def _requires_bearer_token(path: str) -> bool:
    return path.split("?", 1)[0] not in {"/", "/swagger", "/swagger/", "/openapi.json", "/api/health", "/api/auth/login"}


_BEARER_AUTHORIZATION_BY_PORT: dict[int, str] = {}


def _authenticated_headers(port: int, headers: dict[str, str] | None = None) -> dict[str, str]:
    request_headers = dict(headers or {})
    if "Authorization" not in request_headers:
        request_headers["Authorization"] = _bearer_authorization(port)
    return request_headers


def _bearer_authorization_for_state(state_dir: Path) -> str:
    from gp_control_plane.auth import login

    token = login(state_dir, {"username": "admin", "password": "admin"})["access_token"]
    return f"Bearer {token}"


def _bearer_authorization(port: int, *, timeout: float = 5) -> str:
    if token := _BEARER_AUTHORIZATION_BY_PORT.get(port):
        return token
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(
            "POST",
            "/api/auth/login",
            body=b'{"username":"admin","password":"admin"}',
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    if response.status != 200:
        raise AssertionError(f"test login failed with HTTP {response.status}: {body.decode('utf-8', errors='replace')}")
    authorization = f"Bearer {json.loads(body)['access_token']}"
    _BEARER_AUTHORIZATION_BY_PORT[port] = authorization
    return authorization


if __name__ == "__main__":
    unittest.main()
