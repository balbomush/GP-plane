"""Retained R1 wire/admission cases through independent installed Web."""
from __future__ import annotations
import contextlib,http.client,json,os,socket,subprocess,tempfile,threading,time,unittest
from pathlib import Path
from unittest import mock
from gp_control_plane.auth import AuthenticationError
from gp_control_plane.config import AppConfig,OutputConfig
from gp_control_plane.http import core_app
from gp_control_plane.http.cheroot_connection import WebStreamLifecycle
from gp_control_plane.http.http_runtime import WebHttpRuntime
from gp_control_plane.http.legacy_http import unsupported_method
from tests.test_core_transport import _free_port,_request,_wait_for_listener
from tests.integration.a5_transport import _RemoteWebControl

@contextlib.contextmanager
def listeners():
    with tempfile.TemporaryDirectory() as raw:
        state={'storage_error':False,'revoked':False}
        def auth(_state,token):
            if state['storage_error']:raise PermissionError('fixture unavailable')
            if token!='Bearer valid' or state['revoked']:raise AuthenticationError('fixture invalid token')
        config=AppConfig(output=OutputConfig(state_dir=Path(raw)))
        lifecycle=WebStreamLifecycle()
        services=core_app.CoreServices(config,mock.Mock(),ui_enabled=False,stream_lifecycle=lifecycle)
        port=_free_port()
        runtime=WebHttpRuntime(core_app.create_core_app(services,auth),'127.0.0.1',port,stream_lifecycle=lifecycle)
        thread=threading.Thread(target=runtime.serve_forever,daemon=True)
        web=None
        with mock.patch.object(core_app,'_core_get',return_value=core_app.HTTPResponse(body=b'{}',headers={'Content-Type':'application/json','Content-Length':'2'})),mock.patch('gp_control_plane.http.api_server._event_payloads',return_value={'fixture':{'ok':True}}):
            try:
                thread.start();_wait_for_listener(port)
                web=_RemoteWebControl(port)
                yield port,web.port,services,state,runtime,web
            finally:
                state['revoked']=True
                try:
                    if web is not None:web.close()
                finally:
                    runtime.stop();thread.join(5)
                    assert not thread.is_alive(),'Core fixture listener survived'

class R1ArtifactIntegrationTests(unittest.TestCase):
    def test_two_real_sse_readers_shutdown_without_token_rotation(self):
            with listeners() as (_, port, services, state, core, web):
                connections, replies = [], []
                try:
                    for _ in range(2):
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                        connections.append(connection)
                        connection.request("GET", "/api/web/events/stream", headers={"Authorization": "Bearer valid"})
                        reply = connection.getresponse()
                        replies.append(reply)
                        self.assertIn(b"event:", reply.readline())
                    for reply in replies:
                        reply.close()
                    for connection in connections:
                        connection.close()
                    stopped = threading.Thread(target=web.stop, daemon=True)
                    started = time.monotonic()
                    stopped.start()
                    stopped.join(3)
                    clean = not stopped.is_alive()
                    if not clean:
                        # Test teardown only, never used to make the assertion pass.
                        state["revoked"] = True
                        stopped.join(5)
                    self.assertTrue(clean, "Web shutdown did not interrupt both Core readers")
                    self.assertLess(time.monotonic() - started, 3)
                    self.assertEqual(web.final_metrics["active_web_streams"], 0)
                finally:
                    state["revoked"] = True
                    for reply in replies:
                        reply.close()
                    for connection in connections:
                        connection.close()

    def test_real_long_download_export_share_limit_and_release_on_completion(self):
            release = threading.Event()
            def download(*_):
                def chunks():
                    yield b"a" * 4096
                    if not release.wait(4):
                        raise RuntimeError("fixture stream not released")
                    yield b"z"
                return core_app._stream_response(chunks(), "application/zip", headers={"Content-Length": "4097"})
            with mock.patch.object(core_app, "_download_backup", side_effect=download), listeners() as (core_port, web_port, services, state, core, web):
                connections, replies = [], []
                try:
                    for _ in range(2):
                        connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=2)
                        connections.append(connection)
                        connection.request("GET", "/api/core/backups/download-archive?snapshot_id=fixture", headers={"Authorization": "Bearer valid"})
                        reply = connection.getresponse()
                        replies.append(reply)
                        self.assertEqual(reply.status, 200)
                        self.assertEqual(reply.read(4096), b"a" * 4096)
                    for port in (core_port, web_port):
                        started = time.monotonic()
                        for path in ("/api/core/backups/download-archive", "/api/core/strategy-candidates/export"):
                            status, _, body = _request(port, "GET", path, headers={"Authorization": "Bearer valid"})
                            self.assertEqual(status, 503)
                            self.assertEqual(json.loads(body)["error"]["code"], "stream_capacity_exhausted")
                            self.assertEqual(_request(port, "GET", path)[0], 401)
                        self.assertEqual(_request(port, "GET", "/api/core/status", headers={"Authorization": "Bearer valid"})[0], 200)
                        self.assertLess(time.monotonic() - started, 2)
                    release.set()
                    for reply in replies:
                        self.assertEqual(reply.read(), b"z")
                    # A fresh complete long response proves both listeners return permits.
                    result = _request(web_port, "GET", "/api/core/backups/download-archive?snapshot_id=fixture", headers={"Authorization": "Bearer valid"})
                    self.assertEqual(result[0], 200)
                    self.assertEqual(result[2], b"a" * 4096 + b"z")
                finally:
                    release.set()
                    for reply in replies:
                        reply.close()
                    for connection in connections:
                        connection.close()

    def test_real_sse_saturation_auth_precedence_short_requests_and_rotation(self):
            with listeners() as (core_port, web_port, services, state, core, web):
                for port in (core_port, web_port):
                    for method, path in (("PUT", "/api/health"), ("PATCH", "/"), ("DELETE", "/unknown")):
                        status, headers, body = _request(port, method, path)
                        self.assertEqual(status, 501)
                        self.assertEqual(body, unsupported_method(method).body)
                        self.assertEqual(headers["content-type"], "text/html;charset=utf-8")
                        self.assertIn("Cheroot", headers["server"])
                        self.assertNotIn("allow", headers)
                for counts in ((core._server.requests.min, core._server.requests.max, core._server.requests._queue.maxsize), tuple(web.metrics()[key] for key in ("pool_min", "pool_max", "pool_queue"))):
                    self.assertEqual(counts, (8, 8, 8))
                connections = []
                replies = []
                try:
                    for _ in range(2):
                        connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=2)
                        connections.append(connection)
                        connection.request("GET", "/api/web/events/stream", headers={"Authorization": "Bearer valid"})
                        reply = connection.getresponse()
                        replies.append(reply)
                        self.assertEqual(reply.status, 200)
                        self.assertIn(b"event:", reply.readline())
                    for port in (core_port, web_port):
                        started = time.monotonic()
                        status, headers, body = _request(port, "GET", "/api/internal/web-events/stream" if port==core_port else "/api/web/events/stream", headers={"Authorization": "Bearer valid"})
                        self.assertEqual(status, 503, body)
                        self.assertEqual(json.loads(body)["error"]["code"], "stream_capacity_exhausted")
                        self.assertNotIn("allow", headers)
                        self.assertNotIn("text/event-stream", headers["content-type"])
                        self.assertLess(time.monotonic() - started, 2)
                        self.assertEqual(_request(port, "GET", "/api/internal/web-events/stream" if port==core_port else "/api/web/events/stream")[0], 401)
                        self.assertEqual(_request(port, "GET", "/api/core/status", headers={"Authorization": "Bearer valid"})[0], 200)
                    state["storage_error"] = True
                    status, _, body = _request(web_port, "GET", "/api/web/events/stream", headers={"Authorization": "Bearer valid"})
                    self.assertEqual(status, 503)
                    self.assertEqual(json.loads(body)["error"]["code"], "storage_unavailable")
                    state["storage_error"] = False
                    state["revoked"] = True
                    for reply in replies:
                        reply.read()
                    state["revoked"] = False
                    for _ in range(2):
                        permit = services.stream_admission.acquire("sse")
                        self.assertIsNotNone(permit)
                        permit.release()
                    connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=2)
                    connections.append(connection)
                    connection.request("GET", "/api/web/events/stream", headers={"Authorization": "Bearer valid"})
                    reply = connection.getresponse()
                    replies.append(reply)
                    self.assertEqual(reply.status, 200)
                    self.assertIn(b"event:", reply.readline())
                finally:
                    state["revoked"] = True
                    for reply in replies:
                        reply.close()
                    for connection in connections:
                        connection.close()

    def test_segmented_body_auth_rejection_is_complete_and_connection_is_single_use(self):
        with listeners() as (port,*_),mock.patch.object(core_app,'_post') as action:
            env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME'}}
            env['GP_TEST_CORE_URL']='http://127.0.0.1:'+str(port)
            result=subprocess.run([env['GP_TEST_WEB_PYTHON'],'-I','-W','error',env['GP_TEST_WEB_SEGMENTED']],env=env,cwd=tempfile.gettempdir(),capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            action.assert_not_called()
