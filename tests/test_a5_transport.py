from __future__ import annotations

import errno
import http.client
import io
import json
import multiprocessing
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
import ctypes
import gc
from collections.abc import Callable
from contextlib import contextmanager, redirect_stderr
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock
from ctypes import wintypes


import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.backups import snapshot_archive_path, snapshots_dir
from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.resource_budget import BACKUP_UPLOAD_MAX_BYTES, JSON_REQUEST_MAX_BYTES
from gp_control_plane_web.cheroot_connection import (
    GPHTTPConnection,
    WebStreamLifecycle,
    _is_peer_disconnect,
    close_writer_then_base,
)
from gp_control_plane_web.core_client import CoreClient, CoreResponse
from gp_control_plane_web.http_body import (
    ProxyRequestBodyMalformed,
    ProxyRequestBodyTooLarge,
    read_request_body,
)
from gp_control_plane_web.http_runtime import WebHttpRuntime
from gp_control_plane_web.web_app import _CoreResponseStream, create_web_app
from cheroot.server import HTTPConnection, MakeFile


class _WindowsProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class A5HttpBodyTests(unittest.TestCase):
    def test_json_limit_upload_limit_and_explicit_framing_errors(self) -> None:
        accepted_json = b"j" * JSON_REQUEST_MAX_BYTES
        self.assertEqual(
            read_request_body(_environ(accepted_json), "/api/core/run-settings/save"),
            accepted_json,
        )
        with self.assertRaisesRegex(ProxyRequestBodyTooLarge, "request body is too large"):
            read_request_body(_environ(accepted_json + b"x"), "/api/core/run-settings/save")

        # This is the real public 64 MiB backup limit, not a substituted tiny
        # candidate fixture.  Backup bytes stay opaque and Core-owned.
        upload = b"u" * BACKUP_UPLOAD_MAX_BYTES
        self.assertEqual(read_request_body(_environ(upload), "/api/core/backups/upload"), upload)
        with self.assertRaises(ProxyRequestBodyTooLarge):
            read_request_body(_environ(upload + b"x"), "/api/core/backups/upload")

        with self.assertRaisesRegex(ProxyRequestBodyMalformed, "invalid content length"):
            read_request_body({"CONTENT_LENGTH": "nope", "wsgi.input": io.BytesIO()}, "/api/core/run-settings/save")
        with self.assertRaisesRegex(ProxyRequestBodyMalformed, "transfer encoding"):
            read_request_body(
                {"CONTENT_LENGTH": "4", "HTTP_TRANSFER_ENCODING": "chunked", "wsgi.input": io.BytesIO(b"body")},
                "/api/core/run-settings/save",
            )
        with self.assertRaisesRegex(ProxyRequestBodyMalformed, "truncated"):
            read_request_body({"CONTENT_LENGTH": "4", "wsgi.input": io.BytesIO(b"two")}, "/api/core/run-settings/save")

    def test_rejected_known_length_body_is_drained_in_bounded_chunks(self) -> None:
        class RecordingStream(io.BytesIO):
            def __init__(self, data: bytes) -> None:
                super().__init__(data)
                self.requested_sizes: list[int] = []

            def read(self, size: int = -1) -> bytes:
                self.requested_sizes.append(size)
                return super().read(size)

        rejected = b"x" * (3 * 64 * 1024 + 7)
        stream = RecordingStream(rejected)
        with mock.patch("gp_control_plane_web.http_body.JSON_REQUEST_MAX_BYTES", 4), self.assertRaises(
            ProxyRequestBodyTooLarge
        ):
            read_request_body(
                {"CONTENT_LENGTH": str(len(rejected)), "wsgi.input": stream},
                "/api/core/run-settings/save",
            )
        self.assertEqual(stream.tell(), len(rejected))
        self.assertTrue(stream.requested_sizes)
        self.assertLessEqual(max(stream.requested_sizes), 64 * 1024)


class A5CloseAdapterTests(unittest.TestCase):
    def test_writer_precedes_base_and_only_concrete_peer_abort_is_nonfatal(self) -> None:
        events: list[str] = []

        class Writer:
            closed = False

            def close(self) -> None:
                events.append("writer")

        close_writer_then_base(Writer(), lambda: events.append("base"))
        self.assertEqual(events, ["writer", "base"])

        class PeerAbortWriter:
            closed = False

            def close(self) -> None:
                events.append("peer")
                raise ConnectionResetError(errno.ECONNRESET, "peer already gone")

        close_writer_then_base(PeerAbortWriter(), lambda: events.append("base-after-peer"))
        self.assertEqual(events[-2:], ["peer", "base-after-peer"])

        class UnexpectedWriter:
            closed = False

            def close(self) -> None:
                events.append("unexpected")
                raise OSError(errno.EBADF, "must remain visible")

        with self.assertRaises(OSError) as raised:
            close_writer_then_base(UnexpectedWriter(), lambda: events.append("base-after-ebadf"))
        self.assertEqual(raised.exception.errno, errno.EBADF)
        self.assertFalse(_is_peer_disconnect(raised.exception))
        self.assertFalse(_is_peer_disconnect(OSError(errno.EIO, "unsuitable writer failure")))
        self.assertEqual(events[-2:], ["unexpected", "base-after-ebadf"])

    def test_confirmed_abort_defers_reader_close_to_the_blocked_worker(self) -> None:
        """A5-FR: listener stop must not lock against a live Core read."""

        test_case = self
        read_started = threading.Event()
        allow_read_return = threading.Event()
        close_threads: list[int | None] = []

        class BlockingResponse:
            def read1(self, _size: int) -> bytes:
                read_started.set()
                test_case.assertTrue(allow_read_return.wait(timeout=2))
                return b""

            def read(self, size: int) -> bytes:
                return self.read1(size)

            def close(self) -> None:
                close_threads.append(threading.get_ident())

        class BlockingSocket:
            def shutdown(self, _how: int) -> None:
                allow_read_return.set()

            def close(self) -> None:
                allow_read_return.set()

        class BlockingConnection:
            def __init__(self) -> None:
                self.sock = BlockingSocket()
                self.close_threads: list[int | None] = []

            def close(self) -> None:
                self.close_threads.append(threading.get_ident())

        response = BlockingResponse()
        connection = BlockingConnection()
        response_socket = connection.sock
        # http.client clears connection.sock for close-delimited responses
        # after getresponse(), although the reader remains on this transport.
        connection.sock = None
        upstream = CoreResponse(  # type: ignore[arg-type]
            connection=connection,
            response=response,
            response_socket=response_socket,
        )
        lifecycle = WebStreamLifecycle()
        # Cheroot can omit REMOTE_PORT on a supported environment.  An
        # unpaired stream remains listener-owned, so runtime.stop's close_all
        # still wakes the actual blocked Core reader without a peer key.
        stream = _CoreResponseStream(upstream, lifecycle, None)
        self.assertEqual(lifecycle.active_count(), 1)
        observed: list[bytes] = []
        worker = threading.Thread(target=lambda: observed.append(stream.read()), daemon=True)
        worker.start()
        self.assertTrue(read_started.wait(timeout=2))

        stop_thread = threading.get_ident()
        started = time.monotonic()
        lifecycle.close_all()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(lifecycle.active_count(), 0)
        self.assertTrue(upstream.abort_pending)
        self.assertEqual(close_threads, [])
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(observed, [b""])
        self.assertFalse(upstream.abort_pending)
        self.assertEqual(close_threads, [worker.ident])
        self.assertEqual(connection.close_threads, [worker.ident])
        self.assertNotEqual(close_threads[0], stop_thread)

    def test_confirmed_abort_maps_only_its_chunked_incomplete_read_to_terminal_eof(self) -> None:
        """Linux HTTPResponse can wake a listener-owned RST as IncompleteRead."""

        test_case = self
        started = threading.Event()
        release = threading.Event()
        closed_by: list[int | None] = []

        class ChunkedReader:
            def read1(self, _size: int) -> bytes:
                started.set()
                test_case.assertTrue(release.wait(timeout=2))
                raise http.client.IncompleteRead(b"")

            def read(self, size: int) -> bytes:
                return self.read1(size)

            def close(self) -> None:
                closed_by.append(threading.get_ident())

        class TransportSocket:
            def shutdown(self, _how: int) -> None:
                release.set()

            def close(self) -> None:
                release.set()

        class Connection:
            def __init__(self) -> None:
                self.sock = TransportSocket()

            def close(self) -> None:
                closed_by.append(threading.get_ident())

        connection = Connection()
        transport_socket = connection.sock
        connection.sock = None
        upstream = CoreResponse(  # type: ignore[arg-type]
            connection=connection,
            response=ChunkedReader(),
            response_socket=transport_socket,
        )
        stream = _CoreResponseStream(upstream, WebStreamLifecycle(), ("127.0.0.1", 12345))
        observed: list[bytes] = []
        worker = threading.Thread(target=lambda: observed.append(stream.read()), daemon=True)
        worker.start()
        self.assertTrue(started.wait(timeout=2))

        stream.abort()
        self.assertTrue(upstream.abort_pending)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(observed, [b""])
        self.assertFalse(upstream.abort_pending)
        self.assertEqual(len(closed_by), 2)

        class UnexpectedChunkedReader:
            def read1(self, _size: int) -> bytes:
                raise http.client.IncompleteRead(b"partial", 9)

            def read(self, size: int) -> bytes:
                return self.read1(size)

            def close(self) -> None:
                pass

        unexpected = CoreResponse(  # type: ignore[arg-type]
            connection=Connection(),
            response=UnexpectedChunkedReader(),
        )
        with self.assertRaises(http.client.IncompleteRead):
            _CoreResponseStream(unexpected, None, None).read()

    def test_completed_owned_abort_still_maps_its_late_incomplete_read_to_eof(self) -> None:
        """The exhausted-socket abort race has no pending resource by read exit."""

        test_case = self
        started = threading.Event()
        release = threading.Event()

        class Reader:
            def read1(self, _size: int) -> bytes:
                started.set()
                test_case.assertTrue(release.wait(timeout=2))
                raise http.client.IncompleteRead(b"")

            def read(self, size: int) -> bytes:
                return self.read1(size)

            def close(self) -> None:
                pass

        class ExhaustedSocket:
            def shutdown(self, _how: int) -> None:
                release.set()
                raise OSError(10038, "already closed listener socket")

            def close(self) -> None:
                release.set()

        class Connection:
            def __init__(self) -> None:
                self.sock = ExhaustedSocket()

            def close(self) -> None:
                pass

        connection = Connection()
        transport_socket = connection.sock
        connection.sock = None
        upstream = CoreResponse(  # type: ignore[arg-type]
            connection=connection,
            response=Reader(),
            response_socket=transport_socket,
        )
        stream = _CoreResponseStream(upstream, WebStreamLifecycle(), ("127.0.0.1", 54321))
        observed: list[bytes] = []
        worker = threading.Thread(target=lambda: observed.append(stream.read()), daemon=True)
        worker.start()
        self.assertTrue(started.wait(timeout=2))

        stream.abort()
        self.assertTrue(stream._closed)
        self.assertFalse(upstream.abort_pending)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(observed, [b""])

    def test_shutdown_releases_two_streams_with_the_same_public_peer(self) -> None:
        """A listener shutdown owns all streams, not a lossy peer lookup.

        Reverse-proxy/public peer data is permitted to be identical (and the
        companion test above covers a missing peer).  Both live Core readers
        must therefore have their own lifecycle entries until shutdown takes
        its finite snapshot.  The real Cheroot abort test below then proves a
        subsequent health request still succeeds through the listener.
        """

        test_case = self
        lifecycle = WebStreamLifecycle()
        peer = ("198.51.100.44", 443)
        streams: list[_CoreResponseStream] = []
        upstreams: list[CoreResponse] = []
        readers_started: list[threading.Event] = []
        reader_released: list[threading.Event] = []
        close_threads: list[list[int | None]] = []

        def make_stream() -> tuple[_CoreResponseStream, CoreResponse, threading.Event, threading.Event, list[int | None]]:
            started = threading.Event()
            released = threading.Event()
            closed_by: list[int | None] = []

            class BlockingResponse:
                def read1(self, _size: int) -> bytes:
                    started.set()
                    test_case.assertTrue(released.wait(timeout=2))
                    return b""

                def read(self, size: int) -> bytes:
                    return self.read1(size)

                def close(self) -> None:
                    closed_by.append(threading.get_ident())

            class BlockingSocket:
                def shutdown(self, _how: int) -> None:
                    released.set()

                def close(self) -> None:
                    released.set()

            class BlockingConnection:
                def __init__(self) -> None:
                    self.sock = BlockingSocket()
                    self.closed_by: list[int | None] = []

                def close(self) -> None:
                    self.closed_by.append(threading.get_ident())

            connection = BlockingConnection()
            response_socket = connection.sock
            connection.sock = None
            upstream = CoreResponse(  # type: ignore[arg-type]
                connection=connection,
                response=BlockingResponse(),
                response_socket=response_socket,
            )
            return _CoreResponseStream(upstream, lifecycle, peer), upstream, started, released, closed_by

        for _index in range(2):
            stream, upstream, started, released, closed_by = make_stream()
            streams.append(stream)
            upstreams.append(upstream)
            readers_started.append(started)
            reader_released.append(released)
            close_threads.append(closed_by)

        self.assertEqual(lifecycle.active_count(), 2)
        observed: list[bytes] = []
        workers = [
            threading.Thread(target=lambda stream=stream: observed.append(stream.read()), daemon=True)
            for stream in streams
        ]
        for worker in workers:
            worker.start()
        for started in readers_started:
            self.assertTrue(started.wait(timeout=2))

        lifecycle.close_all()
        self.assertEqual(lifecycle.active_count(), 0)
        self.assertTrue(all(upstream.abort_pending for upstream in upstreams))
        self.assertTrue(all(released.is_set() for released in reader_released))
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

        self.assertEqual(observed, [b"", b""])
        self.assertTrue(all(not upstream.abort_pending for upstream in upstreams))
        self.assertTrue(all(len(closed_by) == 1 for closed_by in close_threads))

    def test_abort_finishes_only_an_already_closed_transport_socket(self) -> None:
        """WSAENOTSOCK is an exhausted owned stream, unlike arbitrary I/O."""

        def make_response(error_number: int) -> tuple[CoreResponse, list[str]]:
            closed: list[str] = []

            class Response:
                def close(self) -> None:
                    closed.append("response")

            class Connection:
                sock = None

                def close(self) -> None:
                    closed.append("connection")

            class TransportSocket:
                def shutdown(self, _how: int) -> None:
                    raise OSError(error_number, "controlled transport state")

                def close(self) -> None:
                    closed.append("socket")

            return (
                CoreResponse(  # type: ignore[arg-type]
                    connection=Connection(),
                    response=Response(),
                    response_socket=TransportSocket(),
                ),
                closed,
            )

        exhausted, exhausted_closed = make_response(10038)
        exhausted.abort()
        self.assertFalse(exhausted.abort_pending)
        self.assertEqual(exhausted_closed, ["response", "connection"])

        unexpected, unexpected_closed = make_response(errno.EBADF)
        with self.assertRaises(OSError) as raised:
            unexpected.abort()
        self.assertEqual(raised.exception.errno, errno.EBADF)
        self.assertFalse(unexpected.abort_pending)
        self.assertEqual(unexpected_closed, ["response", "connection"])


class A5WebRuntimeTests(unittest.TestCase):
    def test_factory_has_no_listener_and_real_cheroot_preserves_head_and_501_contract(self) -> None:
        app = create_web_app(_NeverCalledCore())
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = int(reservation.getsockname()[1])
            # Construction succeeds while the port is still reserved: factory
            # and runtime constructors do not bind/listen or start a thread.
            runtime = WebHttpRuntime(app, "127.0.0.1", port)

        thread = threading.Thread(target=runtime.serve_forever, daemon=True)
        thread.start()
        try:
            _wait_for_port(port)
            root = _request(port, "GET", "/")
            head = _request(port, "HEAD", "/")
            put = _request(port, "PUT", "/api/health")
            self.assertEqual(root[0], 200)
            self.assertEqual(head[0], 200)
            self.assertEqual(root[1]["content-type"], "text/html; charset=utf-8")
            self.assertEqual(head[1]["content-type"], "text/html; charset=utf-8")
            self.assertEqual(int(root[1]["content-length"]), len(root[2]))
            self.assertEqual(int(head[1]["content-length"]), len(root[2]))
            self.assertEqual(head[2], b"")
            self.assertEqual(put[0], 501)
            self.assertNotIn("allow", put[1])
            self.assertEqual(len(put[2]), 356)
            self.assertEqual(put[1]["content-length"], "356")
            self.assertEqual(put[1]["content-type"], "text/html;charset=utf-8")
        finally:
            runtime.stop()
            runtime.stop()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_real_http_framing_rejects_invalid_cl_cl_te_and_truncation(self) -> None:
        """Exercise framing at the Cheroot socket boundary, not just WSGI."""

        with tempfile.TemporaryDirectory() as raw, _running_old_core(
            AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
        ) as core_port:
            web_port, runtime, web_thread = _start_web(core_port)
            try:
                cases = {
                    "invalid-content-length": b"POST /api/auth/login HTTP/1.1\r\nHost: localhost\r\nContent-Length: nope\r\n\r\n{}",
                    "content-length-transfer-encoding": (
                        b"POST /api/auth/login HTTP/1.1\r\nHost: localhost\r\n"
                        b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
                    ),
                    "truncated": b"POST /api/auth/login HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4\r\n\r\ntwo",
                }
                for label, request_bytes in cases.items():
                    with self.subTest(label=label):
                        status, _headers, body = _raw_http_response(web_port, request_bytes)
                        self.assertEqual(status, 400, body)
            finally:
                runtime.stop()
                web_thread.join(timeout=5)
                self.assertFalse(web_thread.is_alive())

    def test_real_web_backup_download_and_upload_preserve_archive_bytes(self) -> None:
        """The new Web streams a real Core archive and forwards it unchanged."""

        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _running_old_core(config) as core_port:
                web_port, runtime, web_thread = _start_web(core_port)
                try:
                    login = _request(
                        web_port,
                        "POST",
                        "/api/auth/login",
                        body=b'{"username":"admin","password":"admin"}',
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(login[0], 200, login[2])
                    bearer = {"Authorization": f"Bearer {json.loads(login[2])['access_token']}"}
                    created = _request(
                        web_port,
                        "POST",
                        "/api/core/backups/create",
                        body=b"{}",
                        headers={**bearer, "Content-Type": "application/json"},
                    )
                    self.assertEqual(created[0], 201, created[2])
                    snapshot_id = str(json.loads(created[2])["snapshot_id"])
                    downloaded = _request(
                        web_port,
                        "GET",
                        f"/api/core/backups/download-archive?snapshot_id={snapshot_id}",
                        headers=bearer,
                    )
                    self.assertEqual(downloaded[0], 200, downloaded[2])
                    self.assertEqual(
                        _sha256_bytes(downloaded[2]),
                        _file_sha256(snapshot_archive_path(config.output.state_dir, snapshot_id)),
                    )
                    uploaded = _request(
                        web_port,
                        "POST",
                        "/api/core/backups/upload",
                        body=downloaded[2],
                        headers={**bearer, "Content-Type": "application/zip"},
                    )
                    self.assertEqual(uploaded[0], 201, uploaded[2])
                finally:
                    runtime.stop()
                    web_thread.join(timeout=5)
                    self.assertFalse(web_thread.is_alive())

    def test_public_connection_class_abort_uses_adapter_order_and_stock_control(self) -> None:
        """Exercise actual Cheroot construction/close, not a helper callback."""

        # Cheroot owns Windows transport-manager handles per listener.  The
        # actual lifecycle contract is measured in its own Web process below;
        # run this two-listener ConnectionClass probe in a child too, so its
        # deliberate adapter/stock comparison cannot delay the independent
        # 64 MiB Core pressure assertion in the parent test interpreter.
        # This is isolation, not a mocked or skipped transport check: the
        # child reruns this exact method through real loopback TCP/Cheroot.
        if os.environ.get("GP_A5_CHEROOT_ABORT_CHILD") != "1":
            environment = dict(os.environ)
            environment["GP_A5_CHEROOT_ABORT_CHILD"] = "1"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "-v",
                    "tests.test_a5_transport.A5WebRuntimeTests.test_public_connection_class_abort_uses_adapter_order_and_stock_control",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                completed.returncode,
                0,
                "isolated real Cheroot abort probe failed:\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
            )
            print("A5_CHEROOT_ABORT_CHILD=" + json.dumps({"exit": completed.returncode, "stdout": completed.stdout.strip()}))
            return

        adapted_stderr = io.StringIO()
        with warnings.catch_warnings(record=True) as adapted_warnings, redirect_stderr(adapted_stderr):
            warnings.simplefilter("always")
            with _running_controlled_stream_core() as core_port:
                _ObservedGPHTTPConnection.reset()
                web_port, runtime, web_thread = _start_web(core_port, _ObservedGPHTTPConnection)
                try:
                    # Startup probing is a different accepted socket.
                    _ObservedGPHTTPConnection.reset()
                    bearer = _login_bearer(web_port)
                    peer, frame = _open_first_sse_frame(web_port, bearer)
                    self.assertIn(b"event:", frame)
                    _abort_sse_client(peer.socket)
                    observed = _ObservedGPHTTPConnection.wait_for_closed_peer(peer.address)
                    self.assertIsNotNone(observed)
                    assert observed is not None
                    self.assertEqual(observed.connection._gp_peer, peer.address)
                    events = observed.events
                    self.assertLess(events.index("writer-close"), events.index("reader-close"), events)
                    self.assertLess(events.index("reader-close"), events.index("socket-shutdown"), events)
                    self.assertLess(events.index("socket-shutdown"), events.index("socket-close"), events)
                    self.assertEqual(_request(web_port, "GET", "/api/health")[0], 200)

                    # This is a second invocation of the actual accepted
                    # GPHTTPConnection instance.  Its adapter must not touch
                    # writer/reader/socket after the first close completed.
                    before_repeat = list(events)
                    observed.connection.close()
                    self.assertEqual(events, before_repeat)
                    self.assertEqual(observed.close_attempts, 2)
                finally:
                    runtime.stop()
                    web_thread.join(timeout=5)
                    self.assertFalse(web_thread.is_alive())
            gc.collect()

        resource_warnings = [warning for warning in adapted_warnings if issubclass(warning.category, ResourceWarning)]
        self.assertEqual(resource_warnings, [])
        self.assertEqual(adapted_stderr.getvalue(), "")
        _ObservedGPHTTPConnection.reset()

        # Comparable stock Cheroot control: the same accepted-socket wrapper
        # observes base reader/socket cleanup, but no writer close.  It proves
        # that the positive ordering comes from GPHTTPConnection rather than
        # a fake helper or a global Cheroot patch.
        with _running_controlled_stream_core() as core_port:
            _ObservedStockHTTPConnection.reset()
            web_port, runtime, web_thread = _start_web(core_port, _ObservedStockHTTPConnection)
            try:
                _ObservedStockHTTPConnection.reset()
                bearer = _login_bearer(web_port)
                peer, frame = _open_first_sse_frame(web_port, bearer)
                self.assertIn(b"event:", frame)
                _abort_sse_client(peer.socket)
                observed = _ObservedStockHTTPConnection.wait_for_closed_peer(peer.address)
                self.assertIsNotNone(observed)
                assert observed is not None
                self.assertNotIn("writer-close", observed.events)
                self.assertLess(observed.events.index("reader-close"), observed.events.index("socket-shutdown"))
                self.assertEqual(_request(web_port, "GET", "/api/health")[0], 200)
            finally:
                runtime.stop()
                web_thread.join(timeout=5)
                self.assertFalse(web_thread.is_alive())
        # The observed accepted-socket wrapper deliberately retains close
        # ordering references for assertions.  Release that test-only graph
        # before the independent 64 MiB pressure scenario in this process.
        _ObservedStockHTTPConnection.reset()
        gc.collect()

    @unittest.skipUnless(os.name == "nt", "Windows handle proof; Linux FD proof remains a later Linux/Pi gate")
    def test_windows_twenty_real_sse_disconnects_have_nonmonotonic_handle_series(self) -> None:
        """Measure twenty actual SSE aborts in the separate Web process."""

        with tempfile.TemporaryDirectory() as raw, _running_old_core(
            AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
        ) as core_port, _running_web_process(core_port) as (web_port, control):
            bearer = _login_bearer(web_port)
            # CPython/Bottle form cyclic short-lived request wrappers.  The
            # raw series records that observation; the separately labelled
            # settled series uses an explicit test-only collection barrier.
            # A reachable resource leak survives that boundary and therefore
            # fails the finite warm-up-derived ceiling.
            baseline = control.settled_metrics()
            warmup = [self._abort_and_measure_sse(web_port, bearer, control) for _index in range(15)]
            settled_warmup_handles = [int(sample["settled_after_close"]["windows_handles"]) for sample in warmup]
            settled_tail = settled_warmup_handles[-3:]
            self.assertEqual(
                len(set(settled_tail)),
                1,
                {"settled_warmup_handles": settled_warmup_handles, "settled_tail": settled_tail},
            )
            # This is a finite, observed listener-process high-water mark,
            # not an arbitrary Windows-handle threshold.  A handle still
            # reachable after a new client close survives collection and
            # makes one of the following 20 samples exceed this ceiling.
            accepted_ceiling = settled_tail[0]

            started = time.monotonic()
            samples = [self._abort_and_measure_sse(web_port, bearer, control) for _index in range(20)]
            disconnect_seconds = time.monotonic() - started
            peers = [str(sample["peer"]) for sample in samples]
            raw_handles = [int(sample["raw_after_close"]["windows_handles"]) for sample in samples]
            settled_handles = [int(sample["settled_after_close"]["windows_handles"]) for sample in samples]
            self.assertEqual(len(set(peers)), 20)
            self.assertTrue(
                all(
                    sample["raw_after_close"]["active_web_streams"] == 0
                    and sample["settled_after_close"]["active_web_streams"] == 0
                    for sample in samples
                ),
                samples,
            )
            self.assertLessEqual(
                max(settled_handles),
                accepted_ceiling,
                {
                    "measurement_process": "separate Web listener only",
                    "baseline": baseline,
                    "warmup": warmup,
                    "settled_warmup_handles": settled_warmup_handles,
                    "settled_warmup_tail": settled_tail,
                    "accepted_handle_ceiling": accepted_ceiling,
                    "after_each_disconnect": samples,
                },
            )
            self.assertLessEqual(settled_handles[-1], accepted_ceiling)
            self.assertFalse(all(before < after for before, after in zip(settled_handles, settled_handles[1:])), settled_handles)
            self.assertLess(disconnect_seconds, 5.0)

            cleanup_started = time.monotonic()
            after_stop = control.stop_and_metrics()
            cleanup_seconds = time.monotonic() - cleanup_started
            self.assertLessEqual(after_stop["windows_handles"], accepted_ceiling)
            self.assertEqual(after_stop["active_web_streams"], 0)
            self.assertLess(cleanup_seconds, 5.0)
            result = {
                "measurement_process": "separate Web listener only",
                "baseline": baseline,
                "warmup": warmup,
                "settled_warmup_handles": settled_warmup_handles,
                "settled_warmup_tail": settled_tail,
                "accepted_handle_ceiling": accepted_ceiling,
                "after_each_disconnect": samples,
                "raw_after_each_disconnect_windows_handles": raw_handles,
                "settled_after_each_disconnect_windows_handles": settled_handles,
                "after_listener_stop": after_stop,
                "disconnect_seconds": disconnect_seconds,
                "listener_cleanup_seconds": cleanup_seconds,
            }
            print("A5_WINDOWS_SSE_LIFECYCLE=" + json.dumps(result, sort_keys=True))

    def _abort_and_measure_sse(
        self,
        web_port: int,
        bearer: dict[str, str],
        control: Any,
    ) -> dict[str, object]:
        peer, frame = _open_first_sse_frame(web_port, bearer)
        self.assertIn(b"event:", frame)
        _abort_sse_client(peer.socket)
        raw_metrics = _wait_for_web_stream_release(control)
        return {
            "peer": list(peer.address),
            "raw_after_close": raw_metrics,
            "settled_after_close": control.settled_metrics(),
        }

    def test_wsgi_relay_removes_hop_by_hop_headers(self) -> None:
        upstream = _FakeUpstream(
            headers=(
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", "2"),
                ("Connection", "keep-alive, x-remove"),
                ("Keep-Alive", "timeout=5"),
                ("X-Remove", "not-public"),
            ),
            body=b"{}",
        )
        core = _FakeCore(upstream)
        app = create_web_app(core)
        headers: list[tuple[str, str]] = []
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/api/core/status",
            "QUERY_STRING": "",
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.url_scheme": "http",
            "wsgi.version": (1, 0),
            "wsgi.input": io.BytesIO(),
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": True,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
        }

        def start_response(_status: str, response_headers: list[tuple[str, str]], _exc: object | None = None) -> Callable[[bytes], None]:
            headers.extend(response_headers)
            return lambda _data: None

        self.assertEqual(b"".join(app(environ, start_response)), b"{}")
        names = {name.lower() for name, _value in headers}
        self.assertNotIn("connection", names)
        self.assertNotIn("keep-alive", names)
        self.assertNotIn("x-remove", names)
        self.assertTrue(upstream.closed)

    def test_real_core_two_sse_64_mib_download_remains_responsive(self) -> None:
        """Exercise the real current Web→Core client path under pressure."""

        # This scenario owns both a Core and a Web listener and deliberately
        # keeps two old-Core SSE handlers active through the download.  Run it
        # in a fresh interpreter so it is independent from the preceding
        # ConnectionClass listener probe's process-local Cheroot state.  The
        # child executes the unchanged real Core/Web/socket assertion; no
        # implementation or response is faked by this isolation boundary.
        if os.environ.get("GP_A5_PRESSURE_CHILD") != "1":
            environment = dict(os.environ)
            environment["GP_A5_PRESSURE_CHILD"] = "1"
            # The isolated interpreter must prove its own warning policy; it
            # cannot inherit this property accidentally from the parent test
            # invocation or runner shell.
            environment["PYTHONWARNINGS"] = "error"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-W",
                    "error",
                    "-m",
                    "unittest",
                    "-v",
                    "tests.test_a5_transport.A5WebRuntimeTests.test_real_core_two_sse_64_mib_download_remains_responsive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(
                completed.returncode,
                0,
                "isolated real 2-SSE/64-MiB pressure scenario failed:\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
            )
            print("A5_PRESSURE_CHILD=" + json.dumps({"exit": completed.returncode, "stdout": completed.stdout.strip()}))
            return

        self.assertEqual(os.environ.get("PYTHONWARNINGS"), "error")
        self.assertIn("error", sys.warnoptions)
        print(
            "A5_PRESSURE_CHILD_WARNING_POLICY="
            + json.dumps(
                {"PYTHONWARNINGS": os.environ.get("PYTHONWARNINGS"), "sys_warnoptions": list(sys.warnoptions)},
                sort_keys=True,
            )
        )
        metrics: dict[str, dict[str, int]] = {"before": _process_metrics()}
        with tempfile.TemporaryDirectory() as raw, _running_old_core(AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))) as core_port:
            web_port, runtime, web_thread = _start_web(core_port)
            sse: list[tuple[http.client.HTTPConnection, http.client.HTTPResponse]] = []
            try:
                login = _request(
                    web_port,
                    "POST",
                    "/api/auth/login",
                    body=b'{"username":"admin","password":"admin"}',
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(login[0], 200, login[2])
                bearer = {"Authorization": f"Bearer {json.loads(login[2])['access_token']}"}
                created = _request(
                    web_port,
                    "POST",
                    "/api/core/backups/create",
                    body=b"{}",
                    headers={**bearer, "Content-Type": "application/json"},
                )
                self.assertEqual(created[0], 201, created[2])
                snapshot_id = str(json.loads(created[2])["snapshot_id"])
                fixture = snapshots_dir(Path(raw) / "state") / snapshot_id / "pressure.bin"
                fixture.write_bytes(os.urandom(BACKUP_UPLOAD_MAX_BYTES))
                fixture_hash = _file_sha256(fixture)
                # Archive generation is a Core-owned synchronous ZIP build.
                # Complete that real product operation before this transport
                # scenario starts: its two-second assertion measures Web/Core
                # admission and response headers under two live SSE streams,
                # not the CPU time for first-time compression of 64 MiB.
                prepared_archive = snapshot_archive_path(Path(raw) / "state", snapshot_id)
                self.assertTrue(prepared_archive.is_file())

                download: dict[str, object] = {}
                first_body_chunk = threading.Event()
                allow_full_download = threading.Event()
                download_failed = threading.Event()

                def fetch_archive() -> None:
                    connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=30)
                    try:
                        download["stage"] = "requesting"
                        connection.request("GET", f"/api/core/backups/download-archive?snapshot_id={snapshot_id}", headers=bearer)
                        download["stage"] = "awaiting_response"
                        reply = connection.getresponse()
                        download["stage"] = "reading"
                        download["status"] = reply.status
                        digest = __import__("hashlib").sha256()
                        size = 0
                        first_chunk = reply.read(64 * 1024)
                        if not first_chunk:
                            raise AssertionError("64 MiB download emitted no first body chunk")
                        digest.update(first_chunk)
                        size += len(first_chunk)
                        # Hold a real downloaded body open.  This means all
                        # later SSE/status/fixture-Stop assertions run while
                        # the Web->Core transfer worker is still live, rather
                        # than after the kernel happened to buffer the archive.
                        download.update({"first_chunk_bytes": len(first_chunk), "stage": "holding_first_chunk"})
                        first_body_chunk.set()
                        if not allow_full_download.wait(timeout=15):
                            raise AssertionError("test did not release held 64 MiB download")
                        download["stage"] = "reading_remainder"
                        while chunk := reply.read(256 * 1024):
                            digest.update(chunk)
                            size += len(chunk)
                        download.update({"bytes": size, "sha256": digest.hexdigest()})
                    except BaseException as exc:  # record worker failure for the owning assertion
                        download["error"] = f"{type(exc).__name__}: {exc}"
                        download_failed.set()
                    finally:
                        connection.close()

                worker = threading.Thread(target=fetch_archive, daemon=True)
                worker.start()
                try:
                    self.assertTrue(
                        first_body_chunk.wait(timeout=2),
                        f"64 MiB first body chunk did not arrive; stage={download.get('stage')!r} error={download.get('error')!r}",
                    )
                    self.assertFalse(download_failed.is_set(), download.get("error"))
                    self.assertGreater(int(download.get("first_chunk_bytes") or 0), 0)
                    self.assertEqual(download.get("stage"), "holding_first_chunk")
                    self.assertTrue(worker.is_alive(), "download worker ended before concurrent checks")
                    self.assertFalse(allow_full_download.is_set())

                    # The full frames are deliberately opened after a real
                    # download body byte has arrived and while its reader is
                    # held.  Both SSE streams therefore prove the R1-16 frame
                    # budget under the same concurrent transfer, not merely
                    # before it started.
                    for _index in range(2):
                        connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=5)
                        connection.request("GET", "/api/web/events/stream", headers=bearer)
                        reply = connection.getresponse()
                        self.assertEqual(reply.status, 200)
                        started = time.monotonic()
                        frame = reply.read(512)
                        # The real status event can exceed the first transport
                        # chunk.  Complete this one frame with bounded reads;
                        # it must still arrive within the unchanged R1-16
                        # two-second budget while the archive is held open.
                        while b"\n\n" not in frame:
                            self.assertLess(len(frame), 64 * 1024, "SSE frame exceeded bounded test reader")
                            frame += reply.read(512)
                        self.assertIn(b"event:", frame)
                        self.assertIn(b"\n\n", frame, "SSE must deliver a complete frame")
                        frame_seconds = time.monotonic() - started
                        self.assertLess(frame_seconds, 2.0)
                        self.assertTrue(worker.is_alive(), "download finished while SSE frame was delivered")
                        self.assertEqual(download.get("stage"), "holding_first_chunk")
                        sse.append((connection, reply))
                        download.setdefault("sse_frame_seconds", []).append(frame_seconds)

                    self.assertEqual(len(sse), 2)
                    metrics["during"] = _process_metrics()
                    concurrent: dict[str, object] = {
                        "first_chunk_bytes": download["first_chunk_bytes"],
                        "sse_open": len(sse),
                        "worker_live_before_commands": worker.is_alive(),
                        "reader_state_before_commands": download["stage"],
                        "reader_release_before_commands": allow_full_download.is_set(),
                        "sse_frame_seconds": list(download["sse_frame_seconds"]),
                    }
                    for method, path, body in (
                        ("GET", "/api/core/status", None),
                        ("POST", "/api/core/strategy-discovery/stop-current-run", b'{"dry_run":true}'),
                    ):
                        self.assertTrue(worker.is_alive(), f"download ended before {path}")
                        self.assertEqual(download.get("stage"), "holding_first_chunk")
                        started = time.monotonic()
                        result = _request(
                            web_port,
                            method,
                            path,
                            body=body,
                            headers={**bearer, **({"Content-Type": "application/json"} if body else {})},
                        )
                        elapsed = time.monotonic() - started
                        self.assertLess(elapsed, 2.0, path)
                        self.assertIn(result[0], {200, 202}, result[2])
                        payload = json.loads(result[2])
                        if path.endswith("stop-current-run"):
                            self.assertEqual(payload.get("status"), "dry_run")
                            self.assertTrue(payload.get("accepted"))
                        concurrent[path] = {"status": result[0], "seconds": elapsed}
                        self.assertTrue(worker.is_alive(), f"download ended during {path}")
                        self.assertEqual(download.get("stage"), "holding_first_chunk")
                        self.assertFalse(allow_full_download.is_set())
                    download["concurrent"] = concurrent
                finally:
                    allow_full_download.set()
                    worker.join(timeout=30)
                self.assertFalse(worker.is_alive())
                self.assertFalse(download_failed.is_set(), download.get("error"))
                self.assertEqual(download.get("status"), 200)
                self.assertGreaterEqual(int(download.get("bytes") or 0), BACKUP_UPLOAD_MAX_BYTES)
                self.assertNotEqual(download.get("sha256"), fixture_hash, "zip transport must not pretend fixture bytes are archive bytes")
                self.assertEqual(download.get("concurrent", {}).get("sse_open"), 2)
                print("A5_PRESSURE_TRANSFER=" + json.dumps(download, sort_keys=True))
            finally:
                for connection, reply in sse:
                    reply.close()
                    connection.close()
                runtime.stop()
                web_thread.join(timeout=5)
                self.assertFalse(web_thread.is_alive())

        # RSS and threads remain bounded for the mixed 2-SSE/download shape.
        # Windows handle lifecycle is measured separately through real client
        # disconnects; a process-handle count is not a Linux FD substitute.
        self.assertLess(metrics["during"]["rss_bytes"], 300 * 1024 * 1024)
        self.assertLess(metrics["during"]["threads"], 50)
        print("A5_PRESSURE_METRICS=" + json.dumps(metrics, sort_keys=True))


class _NeverCalledCore:
    def open(self, *_args: Any, **_kwargs: Any) -> CoreResponse:
        raise AssertionError("static/method contract must not call Core")


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read1(self, _size: int) -> bytes:
        body, self._body = self._body, b""
        return body

    def read(self) -> bytes:
        return self.read1(-1)


class _FakeUpstream:
    def __init__(self, *, headers: tuple[tuple[str, str], ...], body: bytes) -> None:
        self.status = 200
        self.reason = "OK"
        self._headers = headers
        self.response = _FakeResponse(body)
        self.closed = False
        # The relay test doubles the finite Core response protocol.  It never
        # represents a confirmed listener abort, but keeps the same explicit
        # state surface as CoreResponse so EOF relay stays type-accurate.
        self.abort_pending = False

    def finish_abort(self) -> None:
        raise AssertionError("finite fake upstream cannot have an abort pending")

    def headers(self) -> tuple[tuple[str, str], ...]:
        return self._headers

    def close(self) -> None:
        self.closed = True


class _FakeCore:
    def __init__(self, upstream: _FakeUpstream) -> None:
        self._upstream = upstream

    def open(self, *_args: Any, **_kwargs: Any) -> _FakeUpstream:
        return self._upstream


@dataclass
class _ConnectionObservation:
    peer: tuple[str, int]
    events: list[str] = field(default_factory=list)
    close_attempts: int = 0
    connection: Any | None = None


@dataclass(frozen=True)
class _SsePeer:
    socket: socket.socket
    address: tuple[str, int]


class _ObservedFile:
    """Delegating Cheroot file wrapper used only by the public class seam."""

    def __init__(self, wrapped: Any, events: list[str], role: str) -> None:
        self._wrapped = wrapped
        self._events = events
        self._role = role

    @property
    def closed(self) -> bool:
        return bool(getattr(self._wrapped, "closed", False))

    def close(self) -> Any:
        self._events.append(f"{self._role}-close")
        return self._wrapped.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


class _ObservedSocket:
    """Delegating accepted-socket wrapper that records actual base cleanup."""

    def __init__(self, wrapped: socket.socket, events: list[str]) -> None:
        self._wrapped = wrapped
        self._events = events

    def shutdown(self, how: int) -> Any:
        self._events.append("socket-shutdown")
        return self._wrapped.shutdown(how)

    def close(self) -> Any:
        self._events.append("socket-close")
        return self._wrapped.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


class _ObservedConnectionMixin:
    """Shared test-only constructor instrumentation via ConnectionClass."""

    observations: list[_ConnectionObservation] = []
    _observations_lock = threading.Lock()

    @classmethod
    def reset(cls) -> None:
        with cls._observations_lock:
            cls.observations = []

    @classmethod
    def wait_for_closed_peer(cls, peer: tuple[str, int], timeout: float = 5.0) -> _ConnectionObservation | None:
        deadline = time.monotonic() + timeout
        while True:
            with cls._observations_lock:
                for observation in cls.observations:
                    if observation.peer == peer and "socket-close" in observation.events:
                        return observation
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    def _install_observation(self, server: Any, sock: socket.socket, base_init: Callable[..., None]) -> None:
        peer = sock.getpeername()
        if not isinstance(peer, tuple) or len(peer) < 2:
            raise AssertionError(f"unexpected accepted peer: {peer!r}")
        observation = _ConnectionObservation((str(peer[0]), int(peer[1])))
        observed_socket = _ObservedSocket(sock, observation.events)

        def observed_makefile(wrapped_socket: Any, mode: str = "r", bufsize: int = io.DEFAULT_BUFFER_SIZE) -> _ObservedFile:
            wrapped = MakeFile(wrapped_socket, mode, bufsize)
            return _ObservedFile(wrapped, observation.events, "reader" if "r" in mode else "writer")

        base_init(server, observed_socket, makefile=observed_makefile)
        observation.connection = self
        with type(self)._observations_lock:
            type(self).observations.append(observation)
        self._a5_observation = observation


class _ObservedGPHTTPConnection(_ObservedConnectionMixin, GPHTTPConnection):
    """Actual GP adapter plus accepted-socket order recorder."""

    def __init__(self, server: Any, sock: socket.socket, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self._install_observation(server, sock, super().__init__)

    def close(self) -> None:
        self._a5_observation.close_attempts += 1
        super().close()


class _ObservedStockHTTPConnection(_ObservedConnectionMixin, HTTPConnection):
    """Same recorder over stock Cheroot, deliberately without GP close logic."""

    def __init__(self, server: Any, sock: socket.socket, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self._install_observation(server, sock, super().__init__)


def _environ(body: bytes) -> dict[str, object]:
    return {"CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + 5
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise AssertionError(f"Cheroot did not bind test port {port}")
            time.sleep(0.01)


def _raw_http_response(port: int, request_bytes: bytes) -> tuple[int, dict[str, str], bytes]:
    """Send one intentionally malformed request and capture the wire response."""

    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.settimeout(5)
        connection.sendall(request_bytes)
        connection.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise AssertionError(f"response has no complete headers: {raw!r}")
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    headers = {
        key.decode("ascii").lower(): value.strip().decode("iso-8859-1")
        for line in lines[1:]
        if b":" in line
        for key, value in (line.split(b":", 1),)
    }
    return status, headers, body


def _login_bearer(port: int) -> dict[str, str]:
    login = _request(
        port,
        "POST",
        "/api/auth/login",
        body=b'{"username":"admin","password":"admin"}',
        headers={"Content-Type": "application/json"},
    )
    if login[0] != 200:
        raise AssertionError(f"SSE login failed: {login[0]} {login[2]!r}")
    return {"Authorization": f"Bearer {json.loads(login[2])['access_token']}"}


def _open_first_sse_frame(port: int, bearer: dict[str, str]) -> tuple[_SsePeer, bytes]:
    """Open a genuine stream and return only after its first complete frame."""

    connection = socket.create_connection(("127.0.0.1", port), timeout=5)
    connection.settimeout(5)
    peer = _SsePeer(connection, (str(connection.getsockname()[0]), int(connection.getsockname()[1])))
    try:
        request = (
            "GET /api/web/events/stream HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            f"Authorization: {bearer['Authorization']}\r\n"
            "Connection: keep-alive\r\n\r\n"
        ).encode("ascii")
        connection.sendall(request)
        received = b""
        while b"\r\n\r\n" not in received:
            received += connection.recv(4096)
        head, _separator, frame_data = received.partition(b"\r\n\r\n")
        if not head.startswith(b"HTTP/1.1 200"):
            raise AssertionError(f"SSE did not start: {head!r}")
        while b"\n\n" not in frame_data:
            chunk = connection.recv(4096)
            if not chunk:
                raise AssertionError("SSE closed before its first complete frame")
            frame_data += chunk
        frame, _separator, _rest = frame_data.partition(b"\n\n")
        return peer, frame + b"\n\n"
    except BaseException:
        connection.close()
        raise


def _abort_sse_client(connection: socket.socket) -> None:
    """Issue an actual loopback TCP reset after receiving one full SSE frame."""

    connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
    connection.close()


@contextmanager
def _running_old_core(config: AppConfig) -> Any:
    """Start the actual A6 Core factory on its common Cheroot listener."""

    from gp_control_plane.web.core_runtime import create_core_runtime

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])
    runtime = create_core_runtime(config, "127.0.0.1", port)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.01)
    else:
        runtime.stop()
        thread.join(timeout=5)
        raise AssertionError("Core Cheroot listener did not become ready")
    try:
        yield port
    finally:
        runtime.stop()
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("Core Cheroot listener did not stop")


@contextmanager
def _running_controlled_stream_core() -> Any:
    """Run a short-lived HTTP Core fixture for the transport-only abort proof.

    The production Core SSE loop intentionally has a 15 second heartbeat.
    Closing that listener cannot join a handler blocked in the old loop until
    its next write, which made this test leave a daemon handler across the
    following pressure scenario.  This fixture stays on the real HTTP/
    CoreClient boundary while giving its owned stream handler a fast *test
    teardown* signal.  Product heartbeat semantics are not exercised or
    changed here; the existing old-Core and installed topology tests retain
    that coverage.
    """

    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802 - HTTP handler contract
            if self.path.split("?", 1)[0] == "/api/auth/login":
                self._json(200, b'{"access_token":"transport-probe"}')
                return
            self._json(404, b'{"error":"not_found"}')

        def do_GET(self) -> None:  # noqa: N802 - HTTP handler contract
            path = self.path.split("?", 1)[0]
            if path == "/api/health":
                self._json(200, b'{"status":"ok"}')
                return
            if path == "/api/internal/auth/verify-bearer":
                if self.headers.get("Authorization") == "Bearer transport-probe":
                    self._json(200, b'{"authorized":true}')
                else:
                    self._json(401, b'{"error":"authentication_required"}')
                return
            if path != "/api/internal/web-events/stream":
                self._json(404, b'{"error":"not_found"}')
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            frame = b'event: probe\ndata: {"ok":true}\n\n'
            try:
                while not release.is_set():
                    self.wfile.write(frame)
                    self.wfile.flush()
                    release.wait(0.02)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                # This owned fixture sees the client abort that the test sends.
                return

        def _json(self, status: int, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_port)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("controlled stream Core did not stop")


def _start_web(
    core_port: int,
    connection_class: type[HTTPConnection] = GPHTTPConnection,
) -> tuple[int, WebHttpRuntime, threading.Thread]:
    from gp_control_plane_web.core_client import CoreClient

    port = _free_port()
    stream_lifecycle = WebStreamLifecycle()
    runtime = WebHttpRuntime(
        create_web_app(
            CoreClient(f"http://127.0.0.1:{core_port}"),
            stream_lifecycle=stream_lifecycle,
        ),
        "127.0.0.1",
        port,
        connection_class=connection_class,
        stream_lifecycle=stream_lifecycle,
    )
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)
    return port, runtime, thread


@dataclass
class _WebProcessControl:
    """One owned Web child used to measure the listener, not test Core."""

    process: Any
    channel: Any
    stopped: bool = False
    final_metrics: dict[str, int] | None = None

    def metrics(self, *, collect: bool = False) -> dict[str, int]:
        if self.stopped:
            raise AssertionError("cannot sample a stopped Web listener process")
        self.channel.send("settled-metrics" if collect else "metrics")
        if not self.channel.poll(2.0):
            raise AssertionError("Web listener process did not return metrics")
        reply = self.channel.recv()
        if not isinstance(reply, dict) or reply.get("kind") != "metrics":
            raise AssertionError(f"unexpected Web metrics reply: {reply!r}")
        return dict(reply["metrics"])

    def settled_metrics(self) -> dict[str, int]:
        return self.metrics(collect=True)

    def stop_and_metrics(self) -> dict[str, int]:
        if self.stopped:
            assert self.final_metrics is not None
            return self.final_metrics
        self.channel.send("stop")
        if not self.channel.poll(5.0):
            raise AssertionError("Web listener process did not stop")
        reply = self.channel.recv()
        if not isinstance(reply, dict) or reply.get("kind") != "stopped":
            raise AssertionError(f"unexpected Web stop reply: {reply!r}")
        self.process.join(timeout=5.0)
        if self.process.is_alive():
            raise AssertionError("owned Web listener process survived stop")
        self.stopped = True
        self.final_metrics = dict(reply["metrics"])
        return self.final_metrics

@contextmanager
def _running_web_process(core_port: int):
    """Run the real Web listener separately so Windows metrics are meaningful."""

    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_web_listener_process, args=(core_port, child), daemon=True)
    process.start()
    control = _WebProcessControl(process, parent)
    try:
        if not parent.poll(5.0):
            raise AssertionError("Web listener process did not publish a port")
        ready = parent.recv()
        if not isinstance(ready, dict) or ready.get("kind") != "ready":
            raise AssertionError(f"unexpected Web listener startup reply: {ready!r}")
        yield int(ready["port"]), control
    finally:
        if not control.stopped:
            control.stop_and_metrics()
        parent.close()
        child.close()


def _web_listener_process(core_port: int, channel: Any) -> None:
    """Child entrypoint: GPHTTPConnection production path, no test double."""

    port = _free_port()
    lifecycle = WebStreamLifecycle()
    runtime = WebHttpRuntime(
        create_web_app(CoreClient(f"http://127.0.0.1:{core_port}"), stream_lifecycle=lifecycle),
        "127.0.0.1",
        port,
        stream_lifecycle=lifecycle,
    )
    server_thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    server_thread.start()
    try:
        _wait_for_port(port)
        channel.send({"kind": "ready", "port": port})
        while True:
            command = channel.recv()
            if command in {"metrics", "settled-metrics"}:
                if command == "settled-metrics":
                    gc.collect()
                channel.send(
                    {
                        "kind": "metrics",
                        "metrics": {
                            **_process_metrics(),
                            "active_web_streams": lifecycle.active_count(),
                        },
                    }
                )
            elif command == "stop":
                runtime.stop()
                server_thread.join(timeout=5.0)
                if server_thread.is_alive():
                    raise AssertionError("Web listener thread survived stop")
                gc.collect()
                channel.send(
                    {
                        "kind": "stopped",
                        "metrics": {**_process_metrics(), "active_web_streams": lifecycle.active_count()},
                    }
                )
                return
            else:
                raise AssertionError(f"unexpected Web listener command: {command!r}")
    finally:
        runtime.stop()


def _wait_for_web_stream_release(control: _WebProcessControl) -> dict[str, int]:
    deadline = time.monotonic() + 2.0
    while True:
        metrics = control.metrics()
        if metrics["active_web_streams"] == 0:
            return metrics
        if time.monotonic() >= deadline:
            raise AssertionError(f"Web upstream stream remained after TCP abort: {metrics!r}")
        time.sleep(0.01)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _file_sha256(path: Path) -> str:
    digest = __import__("hashlib").sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return __import__("hashlib").sha256(value).hexdigest()


def _process_metrics() -> dict[str, int]:
    """Capture current Windows process resources without adding a dependency."""

    if os.name != "nt":
        import resource

        return {
            "rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024,
            "threads": threading.active_count(),
            # A Windows HANDLE is not a Linux descriptor.  The R1 Linux FD
            # measurement is deliberately deferred to its Linux/Pi gate.
            "linux_fd_count": len(tuple(Path("/proc/self/fd").iterdir())),
        }

    # Loading a WinDLL on every sample itself increments process handles.
    # Cache the two stable process APIs so this observer cannot manufacture
    # the leak it is intended to detect.
    libraries = getattr(_process_metrics, "_windows_libraries", None)
    if libraries is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_WindowsProcessMemoryCounters),
            wintypes.DWORD,
        )
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        libraries = (kernel32, psapi, _WindowsProcessMemoryCounters)
        setattr(_process_metrics, "_windows_libraries", libraries)
    kernel32, psapi, counter_type = libraries
    counters = counter_type()
    counters.cb = ctypes.sizeof(counters)
    handle_count = wintypes.DWORD()
    process = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel32.GetProcessHandleCount(process, ctypes.byref(handle_count)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "rss_bytes": int(counters.WorkingSetSize),
        "threads": threading.active_count(),
        "windows_handles": int(handle_count.value),
    }


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        reply = connection.getresponse()
        return reply.status, {key.lower(): value for key, value in reply.getheaders()}, reply.read()
    finally:
        connection.close()
