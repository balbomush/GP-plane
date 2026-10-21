from __future__ import annotations
import http.client,io,json,socket,tempfile,threading,time,unittest
from pathlib import Path
from unittest import mock
from wsgiref.util import setup_testing_defaults
from gp_control_plane.auth import AuthenticationError
from gp_control_plane.config import AppConfig,OutputConfig
from gp_control_plane.http import core_app,docs
from gp_control_plane.http.cheroot_connection import WebStreamLifecycle
from gp_control_plane.http.legacy_http import unsupported_method
from gp_control_plane.http.stream_admission import ClosingIterator,StreamAdmission,stream_kind

class CoreProductCorrectionTests(unittest.TestCase):
    def test_headless_public_sse_stays_404_after_auth_when_internal_slots_full(self):
        with tempfile.TemporaryDirectory() as raw:
            services = core_app.CoreServices(AppConfig(output=OutputConfig(state_dir=Path(raw))), mock.Mock())
            permits = [services.stream_admission.acquire("sse") for _ in range(2)]
            def auth(_state, token):
                if token != "Bearer valid":
                    raise AuthenticationError("invalid")
            app = core_app.create_core_app(services, auth)
            for path, token, expected in (("/api/web/events/stream", "Bearer valid", 404), ("/api/web/events/stream", "", 401), ("/api/internal/web-events/stream", "Bearer valid", 503)):
                environ = {}
                setup_testing_defaults(environ)
                environ.update(PATH_INFO=path, HTTP_AUTHORIZATION=token)
                statuses = []
                def start(status, headers, exc_info=None):
                    statuses.append(status)
                    return lambda _: None
                result = app(environ, start)
                try:
                    list(result)
                    self.assertEqual(int(statuses[0].split()[0]), expected)
                finally:
                    if hasattr(result, "close"):
                        result.close()
            for permit in permits:
                permit.release()


    def test_legacy_exact_body_headers_and_method_escaping(self):
        response = unsupported_method("PUT")
        self.assertEqual(len(response.body), 356)
        self.assertEqual(response.headers["Content-Type"], "text/html;charset=utf-8")
        self.assertEqual(response.headers["Content-Length"], "356")
        self.assertNotIn("Allow", response.headers)
        for method in ("PATCH", "DELETE", "X<&>"):
            result = unsupported_method(method)
            self.assertEqual(result.status_code, 501)
            self.assertEqual(int(result.headers["Content-Length"]), len(result.body))
        self.assertIn(b"X&lt;&amp;&gt;", unsupported_method("X<&>").body)


    def test_source_contract_and_core_filter_ignore_working_directory(self):
        contract = (Path(__file__).resolve().parents[1] / "openapi.json").read_bytes()
        self.assertEqual(docs.openapi_json_bytes(), contract)
        core = json.loads(docs.openapi_json_bytes(core_only=True))
        self.assertFalse(any(path.startswith("/api/web/") for path in core["paths"]))
        with mock.patch.object(docs, "__file__", "/tmp/site-packages/gp_control_plane/web/docs.py"), mock.patch.object(docs, "files") as resource:
            resource.return_value.joinpath.return_value.is_file.return_value = False
            with self.assertRaises(FileNotFoundError):
                docs.openapi_json_bytes()


    def test_admission_classes_unstarted_empty_exception_and_double_close(self):
        for method in ("HEAD", "POST"):
            self.assertIsNone(stream_kind(method, "/api/core/backups/download-archive"))
        self.assertEqual(stream_kind("GET", "/api/core/strategy-candidates/export"), "long")
        admission = StreamAdmission()
        first, second = admission.acquire("sse"), admission.acquire("sse")
        self.assertIsNone(admission.acquire("sse"))
        unstarted = ClosingIterator(iter([b"unused"]), first.release)
        unstarted.close()
        unstarted.close()
        empty = ClosingIterator(iter(()), second.release)
        self.assertEqual(list(empty), [])
        acquired = [admission.acquire("sse"), admission.acquire("sse")]
        self.assertTrue(all(acquired))
        self.assertIsNone(admission.acquire("sse"))
        for permit in acquired:
            permit.release()
        def fail():
            raise RuntimeError("visible stream failure")
            yield b""
        failed = ClosingIterator(fail(), admission.acquire("long").release)
        with self.assertRaisesRegex(RuntimeError, "visible stream failure"):
            next(failed)
        self.assertIsNotNone(admission.acquire("long"))
        self.assertIsNotNone(admission.acquire("long"))


    def test_core_unstarted_sse_close_releases_lifecycle_and_both_slots(self):
        with tempfile.TemporaryDirectory() as raw:
            services = core_app.CoreServices(AppConfig(output=OutputConfig(state_dir=Path(raw))), mock.Mock(), ui_enabled=False, stream_lifecycle=WebStreamLifecycle())
            core_app.request.bind({"REQUEST_METHOD": "GET"})
            core_app.response.bind()
            streams = [core_app._dispatch(services, lambda *_: None, "/api/internal/web-events/stream", "GET", "", {}, {}) for _ in range(2)]
            self.assertEqual(services.stream_lifecycle.active_count(), 2)
            for stream in streams:
                stream.close()
                stream.close()
            self.assertEqual(services.stream_lifecycle.active_count(), 0)
            acquired = [services.stream_admission.acquire("sse") for _ in range(2)]
            self.assertTrue(all(acquired))
            self.assertIsNone(services.stream_admission.acquire("sse"))
            for permit in acquired:
                permit.release()


    def test_early_sse_start_response_and_write_failure_close_before_handoff(self):
        def environ():
            value = {}
            setup_testing_defaults(value)
            value.update(PATH_INFO="/api/internal/web-events/stream", HTTP_AUTHORIZATION="Bearer valid", REMOTE_ADDR="127.0.0.1", REMOTE_PORT="43210")
            return value

        for failure_at in ("start", "write"):
            with self.subTest(failure_at=failure_at), tempfile.TemporaryDirectory() as raw:
                lifecycle = WebStreamLifecycle()
                services = core_app.CoreServices(AppConfig(output=OutputConfig(state_dir=Path(raw))), mock.Mock(), ui_enabled=False, stream_lifecycle=lifecycle)
                core = core_app.create_core_app(services, lambda *_: None)
                failure = BrokenPipeError("header handoff failed")
                def fail_start(*_):
                    if failure_at == "start":
                        raise failure
                    def write(_):
                        raise failure
                    return write
                for app, owner in ((core, lifecycle),):
                    for _ in range(2):
                        with self.assertRaises(BrokenPipeError) as caught:
                            app(environ(), fail_start)
                        self.assertIs(caught.exception, failure)
                        owner.close_peer(("127.0.0.1", 43210))
                        self.assertEqual(owner.active_count(), 0)
                    statuses = []
                    def start(status, headers, exc_info=None):
                        statuses.append(status)
                        return lambda _: None
                    stream = app(environ(), start)
                    self.assertTrue(statuses[0].startswith("200"), statuses)
                    stream.close()
                    self.assertEqual(owner.active_count(), 0)
                permits = [services.stream_admission.acquire("sse") for _ in range(2)]
                self.assertTrue(all(permits))
                for permit in permits:
                    permit.release()
