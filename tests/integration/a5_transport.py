"""A5 artifact integration. Core controller + independently installed Web.

Original assertions retained; test-only stdin control measures the Web child.
No product process loads both distributions or reads a neighboring source tree.
"""
from __future__ import annotations
import ctypes,gc,http.client,json,os,socket,struct,subprocess,sys,tempfile,threading,time,unittest
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from gp_control_plane.backups import snapshot_archive_path,snapshots_dir
from gp_control_plane.config import AppConfig,OutputConfig
from gp_control_plane.resource_budget import BACKUP_UPLOAD_MAX_BYTES
from tests.support.process_guard import OwnedProcess

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

@dataclass(frozen=True)
class _SsePeer:
    socket: socket.socket
    address: tuple[str, int]

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

    try:
        linger_format = "HH" if os.name == "nt" else "ii"
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack(linger_format, 1, 0))
    finally:
        connection.close()

@contextmanager
def _running_current_core(config: AppConfig) -> Any:
    """Start the actual A6 Core factory on its common Cheroot listener."""

    from gp_control_plane.web.core_runtime import create_core_runtime

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])
    runtime = create_core_runtime(config, "127.0.0.1", port, ui_enabled=False)
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

def _wait_for_web_stream_release(control: Any) -> dict[str, int]:
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

class _RemoteWebControl:
    def __init__(self, core_port):
        python=Path(os.environ['GP_TEST_WEB_PYTHON'])
        controller=Path(os.environ['GP_TEST_WEB_METRICS'])
        assert python.is_absolute() and python.is_file() and controller.is_absolute() and controller.is_file()
        self.log=tempfile.TemporaryFile();self.stopped=False;self.final_metrics=None
        env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME'}}
        self.owner=OwnedProcess([str(python),'-I','-W','error',str(controller),str(core_port)],cwd=tempfile.gettempdir(),env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log)
        self.process=self.owner.process
        try:
            row=self._read(10);assert row['kind']=='ready',row
            self.port=row['port']
        except BaseException:
            self.close();raise
    def _read(self,timeout):
        # A bounded helper thread prevents an absent metrics reply hanging CI.
        rows=[]
        thread=threading.Thread(target=lambda:rows.append(self.process.stdout.readline()),daemon=True);thread.start();thread.join(timeout)
        if thread.is_alive():
            self.owner.close();thread.join(2);raise AssertionError('Web metrics reply deadline')
        if not rows or not rows[0]:
            self.log.seek(0);raise AssertionError('Web controller exited: '+self.log.read().decode('utf-8','replace'))
        return json.loads(rows[0])
    def metrics(self,*,collect=False):
        assert not self.stopped
        self.process.stdin.write((('settled-metrics' if collect else 'metrics')+'\n').encode());self.process.stdin.flush()
        row=self._read(2);assert row['kind']=='metrics',row;return row['metrics']
    def settled_metrics(self):return self.metrics(collect=True)
    def stop_and_metrics(self):
        if self.stopped:return self.final_metrics
        self.process.stdin.write(b'stop\n');self.process.stdin.flush()
        row=self._read(5);assert row['kind']=='stopped',row
        assert self.process.wait(5)==0
        self.stopped=True;self.final_metrics=row['metrics'];return self.final_metrics
    def stop(self):self.stop_and_metrics()
    def join(self,timeout):self.process.wait(timeout)
    def is_alive(self):return self.process.poll() is None
    def close(self):
        try:
            if not self.stopped and self.process.poll() is None:self.stop_and_metrics()
        finally:
            self.owner.close()
            for stream in (self.process.stdin,self.process.stdout):
                if stream is not None:stream.close()
            self.log.close()

@contextmanager
def _running_web_process(core_port):
    control=_RemoteWebControl(core_port)
    try:yield control.port,control
    finally:control.close()

def _start_web(core_port):
    control=_RemoteWebControl(core_port)
    # runtime.stop owns graceful termination; join reaps the actual child.
    class Runtime:
        def metrics(self):return control.metrics()
        def stop(self):
            try:control.stop_and_metrics()
            finally:control.close()
    return control.port,Runtime(),control

class A5ArtifactIntegrationTests(unittest.TestCase):
    def test_real_web_backup_download_and_upload_preserve_archive_bytes(self) -> None:
            """The new Web streams a real Core archive and forwards it unchanged."""

            with tempfile.TemporaryDirectory() as raw:
                config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
                with _running_current_core(config) as core_port:
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

    @unittest.skipUnless(os.name == "nt", "Windows HANDLE accounting is platform-specific")
    def test_windows_twenty_real_sse_disconnects_have_nonmonotonic_handle_series(self) -> None:
            """Measure twenty actual SSE aborts in the separate Web process."""

            with tempfile.TemporaryDirectory() as raw, _running_current_core(
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
                        "tests.integration.a5_transport.A5ArtifactIntegrationTests.test_real_core_two_sse_64_mib_download_remains_responsive",
                    ],
                    cwd=Path(__file__).resolve().parents[2],
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
            with tempfile.TemporaryDirectory() as raw, _running_current_core(AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))) as core_port:
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
                        metrics["during"] = {key: value + runtime.metrics().get(key, 0) for key, value in _process_metrics().items()}
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
