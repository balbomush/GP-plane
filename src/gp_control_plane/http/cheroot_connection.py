"""Qualified minimal close-order adapter for the production Web listener."""

from __future__ import annotations

import errno
import os
import threading
from collections.abc import Callable
from typing import Any

import cheroot
from cheroot.server import HTTPConnection


PINNED_CHEROOT_VERSION = "11.1.2"
_PEER_DISCONNECT_ERRNOS = frozenset({errno.EPIPE, errno.ECONNRESET})
_WINDOWS_PEER_DISCONNECT_ERRNOS = frozenset({10053, 10054})


class WebStreamLifecycle:
    """Listener-owned release handles for finite upstream Web streams.

    This is not A1's experimental connection-identity registry: it has no
    token, telemetry, expected-disconnect classification or cross-listener
    state.  It only lets the one Web listener release the Core response owned
    by the same client socket when Cheroot closes that socket.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._streams: dict[int, Callable[[], None]] = {}
        self._terminal: dict[int, tuple[Callable[[], None], threading.Event]] = {}
        self._peer_streams: dict[tuple[str, int], set[int]] = {}
        self._next_stream_id = 1

    def register(self, peer: tuple[str, int] | None, close: Callable[[], None]) -> int:
        with self._lock:
            stream_id = self._next_stream_id
            self._next_stream_id += 1
            self._streams[stream_id] = close
            self._terminal[stream_id] = (close, threading.Event())
            if peer is not None:
                self._peer_streams.setdefault(peer, set()).add(stream_id)
            return stream_id

    def release(self, stream_id: int, peer: tuple[str, int] | None, close: Callable[[], None]) -> None:
        with self._lock:
            terminal = self._terminal.get(stream_id)
            if terminal is None or terminal[0] is not close:
                return
            self._streams.pop(stream_id, None)
            self._terminal.pop(stream_id, None)
            terminal[1].set()
            if peer is not None:
                peer_streams = self._peer_streams.get(peer)
                if peer_streams is not None:
                    peer_streams.discard(stream_id)
                    if not peer_streams:
                        self._peer_streams.pop(peer, None)

    def close_peer(self, peer: tuple[str, int]) -> None:
        with self._lock:
            stream_ids = tuple(self._peer_streams.pop(peer, ()))
            closes = tuple(self._streams.pop(stream_id) for stream_id in stream_ids if stream_id in self._streams)
        for close in closes:
            close()

    def close_all(self) -> tuple[threading.Event, ...]:
        """Abort a snapshot and return reader-terminal signals for shutdown."""
        with self._lock:
            closes = tuple(self._streams.values())
            terminal_events = tuple(event for _close, event in self._terminal.values())
            self._streams.clear()
            self._peer_streams.clear()
        for close in closes:
            close()
        return terminal_events

    def wait_for_terminal(self, events: tuple[threading.Event, ...], timeout: float) -> None:
        """Give aborted readers one bounded chance to execute their finally."""

        per_reader = max(0.0, float(timeout)) / max(len(events), 1)
        for event in events:
            # A finite per-listener handoff, not a worker/threadpool control.
            event.wait(per_reader)
        with self._lock:
            for stream_id, (_close, event) in tuple(self._terminal.items()):
                if event.is_set() or event in events:
                    self._terminal.pop(stream_id, None)

    def active_count(self) -> int:
        """Return only this listener's finite upstream streams for diagnostics."""

        with self._lock:
            return len(self._streams)


def require_pinned_cheroot() -> None:
    """Fail closed if the qualified Cheroot implementation changes."""

    observed = getattr(cheroot, "__version__", None)
    if observed != PINNED_CHEROOT_VERSION:
        raise RuntimeError(
            "GP Cheroot close adapter requires Cheroot "
            f"{PINNED_CHEROOT_VERSION}, observed {observed!r}"
        )


class GPHTTPConnection(HTTPConnection):
    """Close Cheroot's buffered writer before its standard reader/socket path.

    This is deliberately limited to the public ``ConnectionClass`` extension
    point.  It retains every base cleanup error and does not classify or hide
    peer disconnects; the A1 identity/telemetry controls remain experimental.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        require_pinned_cheroot()
        super().__init__(*args, **kwargs)
        self._gp_close_started = False
        peer = self.socket.getpeername()
        self._gp_peer = (str(peer[0]), int(peer[1])) if isinstance(peer, tuple) and len(peer) >= 2 else None

    def close(self) -> None:
        if self._gp_close_started:
            return
        self._gp_close_started = True
        lifecycle_error: BaseException | None = None
        lifecycle = getattr(self.server, "gp_stream_lifecycle", None)
        close_peer = getattr(lifecycle, "close_peer", None)
        if callable(close_peer) and self._gp_peer is not None:
            try:
                close_peer(self._gp_peer)
            except BaseException as error:
                lifecycle_error = error
        try:
            close_writer_then_base(getattr(self, "wfile", None), super().close)
        except BaseException as error:
            if lifecycle_error is not None:
                raise lifecycle_error from error
            raise
        if lifecycle_error is not None:
            raise lifecycle_error


def close_writer_then_base(writer: Any, base_close: Any) -> None:
    """Apply the qualified close order while retaining base cleanup errors."""

    writer_error: BaseException | None = None
    if writer is not None and not bool(getattr(writer, "closed", False)):
        try:
            writer.close()
        except BaseException as error:
            writer_error = error

    try:
        base_close()
    except BaseException as base_error:
        if writer_error is not None and not _is_peer_disconnect(writer_error):
            raise writer_error from base_error
        raise
    if writer_error is not None and not _is_peer_disconnect(writer_error):
        raise writer_error


def _is_peer_disconnect(error: BaseException) -> bool:
    """Allow only a concrete peer reset after standard cleanup completed.

    This is not an ``OSError``/``EBADF`` allowance: the adapter still exposes
    every other writer or base-close failure.  A client that has already
    closed its TCP side cannot receive the buffered response, so its narrow
    reset must not terminate Cheroot's listener after the base cleanup path.
    """

    error_number = getattr(error, "errno", None)
    if isinstance(error, (BrokenPipeError, ConnectionResetError)) and error_number in _PEER_DISCONNECT_ERRNOS:
        return True
    return (
        os.name == "nt"
        and isinstance(error, (ConnectionAbortedError, ConnectionResetError))
        and error_number in _WINDOWS_PEER_DISCONNECT_ERRNOS
    )
