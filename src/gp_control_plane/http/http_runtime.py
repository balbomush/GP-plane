"""Explicit Cheroot listener owner for the Web process."""

from __future__ import annotations

from collections.abc import Callable
import threading
from typing import Any

from cheroot.server import HTTPConnection
from cheroot.wsgi import Server

from .cheroot_connection import GPHTTPConnection, WebStreamLifecycle


class WebHttpRuntime:
    """Own one listener lifecycle without creating it at Web-app factory time."""

    def __init__(
        self,
        application: Callable[..., Any],
        host: str,
        port: int,
        *,
        connection_class: type[HTTPConnection] = GPHTTPConnection,
        stream_lifecycle: WebStreamLifecycle | None = None,
    ) -> None:
        """Construct an unbound listener with its documented connection hook.

        The optional class is deliberately narrow: production uses
        :class:`GPHTTPConnection`; controlled transport tests can observe the
        same documented Cheroot seam without patching a server method or its
        manager/threadpool lifecycle.
        """

        if not isinstance(connection_class, type) or not issubclass(connection_class, HTTPConnection):
            raise TypeError("connection_class must inherit cheroot.server.HTTPConnection")
        # Keep four workers available when both admitted stream classes fill.
        # request_queue_size is the kernel backlog, not the accepted queue.
        self._server = Server(
            (host, port), application, numthreads=8, max=8,
            request_queue_size=8, accepted_queue_size=8,
            accepted_queue_timeout=1, timeout=10, shutdown_timeout=5,
        )
        # Cheroot documents ConnectionClass as the per-listener extension seam.
        self._server.ConnectionClass = connection_class
        # GP-owned finite upstream-stream owner; this is not a Cheroot
        # lifecycle API.  The connection adapter reads it only for the socket
        # currently being closed through the documented ConnectionClass hook.
        self._stream_lifecycle = stream_lifecycle
        if stream_lifecycle is not None:
            self._server.gp_stream_lifecycle = stream_lifecycle
        # Publicly exposed only as a listener-shaped shutdown contract for
        # existing callers.  The application factory itself owns no threads.
        self.request_handlers_idle = threading.Event()
        self.request_handlers_idle.set()
        self.active_request_handler_count = 0
        self._lifecycle_lock = threading.Lock()
        self._start_entered = False
        self._start_finished = threading.Event()
        self._stop_requested = False

    def serve_forever(self) -> None:
        """Bind and serve in the caller-owned thread/process until stopped."""

        with self._lifecycle_lock:
            if self._stop_requested:
                self._start_finished.set()
                return
            self._start_entered = True
        try:
            self._server.start()
        finally:
            self._start_finished.set()
            # Cheroot.start() deliberately leaves interrupt handling to its
            # caller. Reap its non-daemon workers before unwinding the CLI.
            # Publish completion first so stop() cannot wait on this thread
            # when startup failed before the listener became ready.
            self.stop()

    def stop(self) -> None:
        """Idempotently stop the owned listener and Cheroot worker resources."""

        with self._lifecycle_lock:
            self._stop_requested = True
            started = self._start_entered
        if self._stream_lifecycle is not None:
            # Let a downstream-aborted Core reader leave its own finally
            # before Cheroot begins closing the same socket.  The small,
            # finite handoff prevents the close-order race without touching a
            # private manager or worker lifecycle.
            terminal = self._stream_lifecycle.close_all()
            self._stream_lifecycle.wait_for_terminal(terminal, timeout=0.25)
        if not started:
            return
        # Cheroot binds just before it publishes ``ready``.  A caller can
        # legitimately request stop in that small interval; wait for either
        # its public ready state or startup completion, then stop exactly once.
        while not self._server.ready and not self._start_finished.is_set():
            self._start_finished.wait(0.001)
        if self._server.ready:
            self._server.stop()

    # These narrow aliases let callers with the former listener-shaped facade
    # own shutdown explicitly without exposing Cheroot internals.
    def shutdown(self) -> None:
        self.stop()

    def server_close(self) -> None:
        self.stop()

    def close_active_request_connections(self) -> None:
        """Cheroot.stop() owns connection shutdown; no private manager access."""

        return
