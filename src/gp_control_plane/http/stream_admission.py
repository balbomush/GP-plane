"""Per-application nonblocking admission for worker-occupying GET streams."""

from collections.abc import Callable, Iterable, Iterator
import threading

from ..application.web_view_contracts import INTERNAL_WEB_EVENTS_STREAM_PATH


def stream_kind(method: str, path: str) -> str | None:
    if method != "GET":
        return None
    if path in {"/api/web/events/stream", INTERNAL_WEB_EVENTS_STREAM_PATH}:
        return "sse"
    if path in {"/api/core/backups/download-archive", "/api/core/strategy-candidates/export"}:
        return "long"
    return None


class StreamAdmission:
    def __init__(self) -> None:
        self._slots = {"sse": threading.BoundedSemaphore(2), "long": threading.BoundedSemaphore(2)}

    def acquire(self, kind: str) -> "Permit | None":
        slot = self._slots[kind]
        return Permit(slot) if slot.acquire(blocking=False) else None


class Permit:
    def __init__(self, slot: threading.BoundedSemaphore) -> None:
        self._slot = slot
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if not self._released:
                self._released = True
                self._slot.release()


class ClosingIterator:
    """Close even an unstarted iterable; never depend on generator finally."""

    def __init__(self, source: Iterable[bytes], release: Callable[[], None]) -> None:
        self._source = iter(source)
        self._release = release
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        try:
            return next(self._source)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._source, "close", None)
            if close is not None:
                close()
        finally:
            self._release()
