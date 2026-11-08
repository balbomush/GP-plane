"""Non-queued heavy operation admission for the Core application."""

from contextlib import contextmanager
from threading import Lock


class OperationBusy(RuntimeError):
    def __init__(self, operation: str):
        super().__init__(operation)
        self.operation = operation


class OperationAdmission:
    def __init__(self):
        self._lock = Lock()
        self._active: str | None = None

    @property
    def active(self) -> str | None:
        with self._lock:
            return self._active

    @contextmanager
    def claim(self, operation: str):
        with self._lock:
            if self._active is not None:
                raise OperationBusy(self._active)
            self._active = operation
        try:
            yield
        finally:
            with self._lock:
                self._active = None

    @contextmanager
    def user_write(self):
        # Hold only across a short write transaction. A restore admission
        # cannot race between this check and the write's commit.
        with self._lock:
            if self._active == "backup_restore":
                raise OperationBusy(self._active)
            yield
