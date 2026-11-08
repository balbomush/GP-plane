from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .state import (
    JOB_RUNNER_LOCK_FILE_NAME,
    append_jsonl,
    is_stale_process_payload,
    now_iso,
    read_job_lock_payload,
    read_state,
    update_state,
)
from .storage import append_run, compact_run_payload, connect


FINAL_JOB_STATUSES = {"success", "failed", "timeout", "stopped"}
FINALIZATION_ERROR_MESSAGE_MAX_LENGTH = 512


class ManagedRuntimeQuarantinedError(RuntimeError):
    """A root-owned runtime could not be proven stopped and must stay blocked."""

    def __init__(self, message: str, *, nft_table: str | None = None):
        super().__init__(message)
        self.nft_table = nft_table


@dataclass(frozen=True)
class Run:
    run_id: str
    name: str
    status: str
    created_at: str


class _CancellationToken(threading.Event):
    """Event-compatible cancellation token with an atomic child-launch claim."""

    def __init__(self) -> None:
        super().__init__()
        self._mutex = threading.Lock()

    def set(self) -> None:
        with self._mutex:
            super().set()

    def clear(self) -> None:
        with self._mutex:
            super().clear()

    @contextmanager
    def claim_child_launch(self) -> Iterator[bool]:
        """Atomically check cancellation and reserve a child launch until exit."""
        with self._mutex:
            yield not self.is_set()


class JobRunner:
    def __init__(self, state_dir: Path, on_idle: Callable[[], Any] | None = None):
        self.state_dir = state_dir
        self._on_idle = on_idle
        self._lock = threading.Lock()
        self._active_run_id: str | None = None
        self._active_run_name: str | None = None
        self._active_cancel: _CancellationToken | None = None
        self._active_cancel_hook: Callable[[], Any] | None = None
        self._active_state_lock: _StateDirJobLock | None = None
        self._active_quarantined = False

    def start(
        self,
        name: str,
        func: Callable[[threading.Event, str], Any],
        cancel_hook: Callable[[], Any] | None = None,
    ) -> Run:
        run_id: str | None = None
        try:
            with self._lock:
                if str(read_state(self.state_dir).get("current_run_status") or "") == "quarantined":
                    raise RuntimeError("managed runtime recovery is required before starting a new run")
                if self._active_run_id:
                    raise RuntimeError(f"run already running: {self._active_run_id}")
                run_id = uuid.uuid4().hex
                cancel_event = _CancellationToken()
                state_lock = _StateDirJobLock.acquire(self.state_dir, run_id, name)
                self._active_run_id = run_id
                self._active_run_name = name
                self._active_cancel = cancel_event
                self._active_cancel_hook = cancel_hook
                self._active_state_lock = state_lock
                self._active_quarantined = False
                created_at = now_iso()
                run = Run(run_id=run_id, name=name, status="queued", created_at=created_at)
                self._record(run_id, name, "queued", created_at)
                self._persist_queued_run(run)
                self._set_current_run_locked(run_id, name, "queued")
                thread = threading.Thread(
                    target=self._run,
                    args=(run_id, name, created_at, func, cancel_event),
                    daemon=True,
                )
                thread.start()
        except Exception:
            if run_id is not None:
                self._clear_active_run(run_id, release_state_lock=True)
            raise
        return run

    def cancel_active(self) -> dict[str, str]:
        with self._lock:
            if not self._active_run_id or not self._active_run_name:
                raise RuntimeError("no active run")
            run_id = self._active_run_id
            name = self._active_run_name
            if self._active_quarantined:
                return {"run_id": run_id, "name": name, "status": "quarantined"}
            if not self._active_cancel:
                raise RuntimeError("no active run")
            cancel_hook = None
            cancellation_started = not self._active_cancel.is_set()
            if cancellation_started:
                self._active_cancel.set()
                cancel_hook = self._active_cancel_hook
                # Keep the state transition ordered with respect to worker
                # finalisation.  Otherwise a worker can mark this same run as
                # "saving" and a late Stop write can incorrectly restore
                # "stopping" while the post-run snapshot is already running.
                self._record(run_id, name, "stopping", now_iso())
                self._set_current_run_locked(run_id, name, "stopping")
        if cancel_hook:
            threading.Thread(target=self._run_cancel_hook, args=(cancel_hook,), daemon=True).start()
        return {"run_id": run_id, "name": name, "status": "stopping"}

    @staticmethod
    def _run_cancel_hook(cancel_hook: Callable[[], Any]) -> None:
        try:
            cancel_hook()
        except Exception:
            return

    def _run(
        self,
        run_id: str,
        name: str,
        started_at: str,
        func: Callable[[threading.Event, str], Any],
        cancel_event: _CancellationToken,
    ) -> None:
        self._mark_run_started(run_id, name, cancel_event)
        last_error: str | None = None
        last_run_status = "failed"
        quarantined = False
        try:
            result = func(cancel_event, run_id)
            status = _status_from_result(result)
            self._record(run_id, name, status, now_iso(), result=result)
            last_error = None
            last_run_status = status
        except ManagedRuntimeQuarantinedError as exc:
            completed_at = now_iso()
            last_error = str(exc)
            quarantined = True
            self._record(run_id, name, "failed", completed_at, error=last_error, event="runtime_quarantine")
            self._persist_failed_run(run_id, name, started_at, completed_at, last_error)
        except Exception as exc:  # noqa: BLE001
            completed_at = now_iso()
            last_error = str(exc)
            self._record(run_id, name, "failed", completed_at, error=last_error)
            self._persist_failed_run(run_id, name, started_at, completed_at, last_error)
        finally:
            if quarantined:
                self._quarantine_active_run(run_id, name, last_run_status, last_error)
                return
            last_snapshot: dict[str, Any] | None = None
            try:
                try:
                    self._mark_run_finalizing(run_id, name, last_run_status, last_error)
                except Exception:
                    pass
                if self._on_idle:
                    last_snapshot = self._run_post_run_finalizer()
                    self._record(
                        run_id,
                        name,
                        "saving",
                        now_iso(),
                        event="post_run_snapshot",
                        outcome=last_snapshot,
                        terminal_run_status=last_run_status,
                        terminal_last_error=last_error,
                    )
            finally:
                self._finish_active_run(run_id, last_run_status, last_error, last_snapshot)

    def _quarantine_active_run(
        self,
        run_id: str,
        name: str,
        last_run_status: str,
        last_error: str | None,
    ) -> None:
        with self._lock:
            if self._active_run_id != run_id:
                return
            self._active_cancel = None
            self._active_cancel_hook = None
            self._active_quarantined = True

            def mark_quarantined(state: dict[str, Any]) -> dict[str, Any]:
                state["last_error"] = last_error
                state["last_run_status"] = last_run_status
                state["current_run_id"] = run_id
                state["current_run_name"] = name
                state["current_run_status"] = "quarantined"
                return state

            update_state(self.state_dir, mark_quarantined)

    def _run_post_run_finalizer(self) -> dict[str, Any]:
        try:
            result = self._on_idle() if self._on_idle else None
        except Exception as exc:  # noqa: BLE001
            return _finalization_callback_failure(exc)
        if not isinstance(result, dict):
            return _finalization_callback_failure("snapshot finalizer returned no outcome")
        outcome = dict(result)
        if outcome.get("kind") != "snapshot" or outcome.get("status") not in {"success", "failed"}:
            return _finalization_callback_failure("snapshot finalizer returned an invalid outcome")
        if not isinstance(outcome.get("completed_at"), str) or not outcome["completed_at"]:
            return _finalization_callback_failure("snapshot finalizer outcome has no completion time")
        if outcome["status"] == "success" and not str(outcome.get("snapshot_id") or "").strip():
            return _finalization_callback_failure("successful snapshot outcome has no snapshot id")
        if outcome["status"] == "failed":
            if not isinstance(outcome.get("error_code"), str) or not outcome["error_code"]:
                return _finalization_callback_failure("failed snapshot outcome has no error code")
            if not isinstance(outcome.get("error_message"), str) or not outcome["error_message"]:
                return _finalization_callback_failure("failed snapshot outcome has no error message")
        return outcome

    def _mark_run_finalizing(
        self,
        run_id: str,
        name: str,
        last_run_status: str,
        last_error: str | None,
    ) -> None:
        with self._lock:
            if self._active_run_id != run_id:
                return
            self._active_cancel = None
            self._active_cancel_hook = None

            def mark_finalizing(state: dict[str, Any]) -> dict[str, Any]:
                state["last_error"] = last_error
                state["last_run_status"] = last_run_status
                state["current_run_id"] = run_id
                state["current_run_name"] = name
                state["current_run_status"] = "saving"
                return state

            update_state(self.state_dir, mark_finalizing)

    def _finish_active_run(
        self,
        run_id: str,
        last_run_status: str,
        last_error: str | None,
        last_snapshot: dict[str, Any] | None,
    ) -> None:
        state_lock = None
        try:
            with self._lock:
                if self._active_run_id != run_id:
                    return
                state_lock = self._active_state_lock
                try:
                    def mark_finished(state: dict[str, Any]) -> dict[str, Any]:
                        state["last_error"] = last_error
                        state["last_run_status"] = last_run_status
                        if last_snapshot is not None:
                            state["last_snapshot"] = last_snapshot
                        state["current_run_id"] = None
                        state["current_run_name"] = None
                        state["current_run_status"] = None
                        return state

                    update_state(self.state_dir, mark_finished)
                finally:
                    self._active_run_id = None
                    self._active_run_name = None
                    self._active_cancel = None
                    self._active_cancel_hook = None
                    self._active_state_lock = None
                    self._active_quarantined = False
        finally:
            if state_lock:
                state_lock.release()

    def _clear_active_run(self, run_id: str, *, release_state_lock: bool) -> None:
        state_lock = None
        with self._lock:
            if self._active_run_id != run_id:
                return
            if release_state_lock:
                state_lock = self._active_state_lock
            self._active_run_id = None
            self._active_run_name = None
            self._active_cancel = None
            self._active_cancel_hook = None
            self._active_state_lock = None
            self._active_quarantined = False
        if state_lock:
            state_lock.release()

    def _record(self, run_id: str, name: str, status: str, timestamp: str, **extra: Any) -> None:
        payload = {
            "run_id": run_id,
            "name": name,
            "status": status,
            "timestamp": timestamp,
        }
        for key, value in extra.items():
            if key == "result" and isinstance(value, dict):
                payload[key] = compact_run_payload(value)
            else:
                payload[key] = value
        append_jsonl(self.state_dir / "jobs.jsonl", payload)

    def _persist_queued_run(self, run: Run) -> None:
        append_run(
            self.state_dir,
            {
                "id": run.run_id,
                "kind": run.name,
                "status": run.status,
                "timestamp": run.created_at,
                "started_at": run.created_at,
            },
        )

    def _persist_failed_run(
        self,
        run_id: str,
        name: str,
        started_at: str,
        completed_at: str,
        error: str,
    ) -> None:
        previous = _latest_run_payload_by_id(self.state_dir, run_id)
        log_metadata = {
            key: previous[key]
            for key in (
                "stdout_log",
                "stderr_log",
                "progress_log",
                "metrics_log",
                "summary_fallback_log",
                "debug_stdout_log",
                "stdout_log_mode",
                "debug_stdout",
            )
            if key in previous
        }
        append_run(
            self.state_dir,
            {
                "id": run_id,
                "kind": name,
                "status": "failed",
                "timestamp": started_at,
                "started_at": started_at,
                "completed_at": completed_at,
                "error": error,
                **log_metadata,
            },
        )

    def _set_current_run(self, run_id: str, name: str, status: str) -> None:
        with self._lock:
            self._set_current_run_locked(run_id, name, status)

    def _mark_run_started(self, run_id: str, name: str, cancel_event: _CancellationToken) -> None:
        """Record ``running`` only when cancellation has not already won startup.

        ``cancel_active()`` and the worker share this lock.  The winner either
        transitions queued work to ``stopping`` before the worker can emit a
        misleading running event, or records ``running`` first and lets Stop
        subsequently replace it with ``stopping``.
        """
        with self._lock:
            if self._active_run_id != run_id or cancel_event.is_set():
                return
            self._record(run_id, name, "running", now_iso())
            self._set_current_run_locked(run_id, name, "running")

    def _set_current_run_if_active(self, run_id: str, name: str, status: str) -> None:
        with self._lock:
            if self._active_run_id != run_id:
                return
            self._set_current_run_locked(run_id, name, status)

    def _set_current_run_locked(self, run_id: str, name: str, status: str) -> None:
        def mark_running(state: dict[str, Any]) -> dict[str, Any]:
            state["current_run_id"] = run_id
            state["current_run_name"] = name
            state["current_run_status"] = status
            return state

        update_state(self.state_dir, mark_running)


class _StateDirJobLock:
    _LOCK_FILE_NAME = JOB_RUNNER_LOCK_FILE_NAME

    def __init__(self, path: Path):
        self._path = path
        self._released = False

    @classmethod
    def acquire(cls, state_dir: Path, run_id: str, run_name: str) -> "_StateDirJobLock":
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / cls._LOCK_FILE_NAME
        payload = {
            "pid": os.getpid(),
            "run_id": run_id,
            "run_name": run_name,
            "created_at": now_iso(),
        }
        for _attempt in range(2):
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                existing = read_job_lock_payload(state_dir)
                if is_stale_process_payload(existing):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise RuntimeError(cls._busy_message(existing)) from exc
                    continue
                raise RuntimeError(cls._busy_message(existing))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True)
            return cls(path)
        raise RuntimeError(cls._busy_message(read_job_lock_payload(state_dir)))

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._path.unlink()
        except FileNotFoundError:
            return

    @staticmethod
    def _busy_message(payload: dict[str, Any]) -> str:
        run_id = str(payload.get("run_id") or "").strip()
        pid = payload.get("pid")
        details = f": {run_id}" if run_id else ""
        if isinstance(pid, int) and pid > 0:
            details = f"{details} pid={pid}"
        return f"run already running in state_dir{details}"


def _status_from_result(result: Any) -> str:
    if isinstance(result, dict):
        status = str(result.get("status") or "").strip().lower()
        if status in FINAL_JOB_STATUSES:
            return status
    return "success"


def _finalization_callback_failure(error: BaseException | str) -> dict[str, str]:
    message = str(error).strip() or (
        type(error).__name__ if isinstance(error, BaseException) else "snapshot finalizer failed"
    )
    return {
        "kind": "snapshot",
        "status": "failed",
        "completed_at": now_iso(),
        "error_code": "finalization_callback_failed",
        "error_message": " ".join(message.split())[:FINALIZATION_ERROR_MESSAGE_MAX_LENGTH],
    }


def _latest_run_payload_by_id(state_dir: Path, run_id: str) -> dict[str, Any]:
    """Return the newest persisted payload for one run without paging global history."""
    with connect(state_dir) as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE id = ? ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    if row is None:
        return {}
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        return {}
    return compact_run_payload(payload) if isinstance(payload, dict) else {}
