"""Own the active execution and its single persisted API 2 run."""

from collections import deque
from dataclasses import dataclass, field
import json
import logging
from os import fstat
from pathlib import Path
import re
from threading import Event, RLock, Thread
from uuid import uuid4

from ..jobs import ManagedRuntimeQuarantinedError, _CancellationToken, _StateDirJobLock
from ..repositories import catalog, search_history
from ..state import now_iso, update_state
from ..storage import connect_api2, save_strategy_check
from .operation_admission import OperationBusy


_LOG = logging.getLogger(__name__)
_GP = "blockcheck2-gp"
_KINDS = {"blockcheck2": "zapret-standard-discovery", _GP: "zapret-multi-domain-discovery"}


class SearchError(ValueError):
    def __init__(self, status, code, message, reason=None):
        super().__init__(message)
        self.status, self.code, self.reason = status, code, reason


def _checkpoint(row):
    value = json.loads(row["checkpoint_json"] or "null")
    if (not isinstance(value, dict) or set(value) != {"revision", "trail"}
            or not isinstance(value["revision"], str) or not value["revision"]
            or not isinstance(value["trail"], list)):
        return None
    for item in value["trail"]:
        if (not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str)
                or not re.fullmatch("[a-f0-9]{64}", item[0]) or type(item[1]) is not bool):
            return None
    return value


def resume_available(row, milestone):
    if row["engine_code"] != _GP or row["status"] not in ("stopped", "failed") or row["milestone_id"] != milestone:
        return False
    try:
        from ..http.api_v2.search_input import validate_start

        domains = json.loads(row["domains_json"] or "null")
        parameters = json.loads(row["parameters_json"] or "null")
        effective, errors = validate_start(_GP, {"domains": domains, "parameters": parameters})
        return not errors and effective["domains"] == domains and effective["parameters"] == parameters and (
            _checkpoint(row) is not None and re.fullmatch("[a-f0-9]{32}", row["id"]) is not None
        )
    except (TypeError, ValueError):
        return False


class RunLog:
    def __init__(self, run_id):
        self.run_id = run_id
        self.generation = uuid4().hex
        self.entries = deque(maxlen=256)
        self.count = 0
        self.lock = RLock()

    def append(self, line, level):
        with self.lock:
            self.count += 1
            self.entries.append({
                "position": f"{self.run_id}:{self.generation}:{self.count}",
                "time": now_iso(), "level": level, "message": line.rstrip("\r\n")[:4096],
            })

    @staticmethod
    def cursor(run_id, after):
        owner, _, rest = after.partition(":")
        generation, _, raw = rest.partition(":")
        if owner != run_id or not generation or not raw.isascii() or not raw.isdecimal() or len(raw) > 20:
            raise SearchError(400, "invalid_parameters", "Неверная позиция журнала", "after")
        return generation, int(raw)

    def read(self, after, limit):
        with self.lock:
            floor = max(0, self.count - len(self.entries))
            index = max(floor, self.count - limit)
            truncated = floor > 0
            if after is not None:
                generation, offset = self.cursor(self.run_id, after)
                if generation == self.generation and floor <= offset <= self.count:
                    index, truncated = offset, False
                else:
                    index, truncated = floor, True
            entries = list(self.entries)[index - floor:index - floor + limit]
            return {"entries": entries, "next_after": entries[-1]["position"] if entries else after,
                    "truncated": truncated}


def _saved_log(path, run_id, cursor, limit):
    tail = deque(maxlen=limit)
    following = []
    matched = False
    lost = False
    if path.parent.is_symlink():
        return None
    for chunk in (path.with_suffix(".log.1"), path):
        if chunk.is_symlink():
            continue
        try:
            stream = chunk.open("rb")
        except FileNotFoundError:
            continue
        with stream:
            # Rotation preserves the file identity while a new file gets a new one.
            metadata = fstat(stream.fileno())
            generation = f"saved-{metadata.st_dev:x}-{metadata.st_ino:x}"
            for line in iter(lambda: stream.readline(65536), b""):
                if not line.endswith(b"\n"):
                    if len(line) == 65536:
                        lost = True
                        while (remainder := stream.readline(65536)) and not remainder.endswith(b"\n"):
                            pass
                    continue
                if line.startswith(b"# log rotated,"):
                    lost = True
                    continue
                if line.startswith(b"GP-API2 "):
                    continue
                position = stream.tell()
                entry = {"position": f"{run_id}:{generation}:{position}", "time": None,
                         "level": "info", "message": line.decode("utf-8", "replace").rstrip("\r\n")[:4096]}
                lost = lost or len(tail) == limit
                tail.append(entry)
                if matched:
                    following.append(entry)
                    if len(following) == limit:
                        break
                elif cursor == (generation, position):
                    matched = True
        if len(following) == limit:
            break
    if not tail:
        return None
    entries = following if matched else list(tail)
    return {"entries": entries, "next_after": entries[-1]["position"] if entries else tail[-1]["position"],
            "truncated": not matched and (cursor is not None or lost)}


@dataclass
class Execution:
    state_dir: Path
    run_id: str
    code: str
    parameters: dict
    domains: list[str]
    checkpoint: dict | None
    summary: dict
    journal: RunLog
    guard: RLock
    publish: object
    cancel: Event = field(default_factory=_CancellationToken)
    done: Event = field(default_factory=Event)
    cleanup_confirmed: bool = False
    nft_table: str | None = None

    def remember_nft_table(self, table):
        update_state(self.state_dir, lambda state: {**state, "current_run_nft_table": table})
        self.nft_table = table

    def log(self, line, level="info"):
        self.journal.append(line, level)

    def check(self, identity, protocol, args, files, outcomes):
        checkpoint = self.checkpoint
        if checkpoint is not None:
            checkpoint = {"revision": checkpoint["revision"],
                          "trail": [*checkpoint["trail"], [identity, all(outcomes.values())]]}
        save_strategy_check(
            self.state_dir, run_id=self.run_id, protocol=protocol, execution_format="nfqws2",
            args=args, files=files, outcomes=outcomes, checkpoint=checkpoint,
        )
        with self.guard:
            self.checkpoint = checkpoint
            progress = self.summary["progress"]
            progress["checked"] = (progress["checked"] or 0) + 1
            progress["current_args"] = args
            self.publish()


class SearchController:
    def __init__(self, state_dir, admission, driver):
        self.state_dir = state_dir
        self.admission = admission
        self.driver = driver
        self.lock = RLock()
        self.active = None
        self.logs = {}
        self.subscribers = {}

    def _prepare(self, code, parameters):
        try:
            return self.driver.prepare(code, parameters)
        except (OSError, RuntimeError, ValueError) as error:
            raise SearchError(503, "engine_not_ready", "Движок не готов к запуску") from error

    def _resume_input(self, conn, run_id):
        row = search_history.read(conn, run_id)
        if row is None:
            raise SearchError(404, "not_found", "Поиск не найден")
        if row["engine_code"] != _GP:
            raise SearchError(409, "state_conflict", "Этот движок не поддерживает продолжение", "wrong_engine")
        current = catalog.milestone(conn, None)
        if row["milestone_id"] != current:
            raise SearchError(409, "milestone_conflict", "Поиск относится к другой вехе")
        if row["status"] not in ("stopped", "failed"):
            raise SearchError(409, "state_conflict", "Этот поиск нельзя продолжить", "not_resumable")
        if not resume_available(row, current):
            raise SearchError(409, "state_conflict", "Позиция продолжения недоступна", "checkpoint_unavailable")
        return row

    def start(self, code, spec, *, resume_id=None):
        prior = None
        if resume_id is not None:
            with connect_api2(self.state_dir) as conn:
                prior = self._resume_input(conn, resume_id)
            spec = {"parameters": json.loads(prior["parameters_json"]), "domains": json.loads(prior["domains_json"]),
                    "domain_list_id": prior["preset_id"]}
        if resume_id is None and spec["domain_list_id"] is not None:
            with connect_api2(self.state_dir) as conn:
                self._matching_preset(conn, spec)
        active = self.admission.active
        if active is not None:
            raise OperationBusy(active)
        revision = self._prepare(code, spec["parameters"])
        slot = self.admission.claim("search")
        slot.__enter__()
        job = None
        ownership = None
        try:
            with self.lock:
                run_id = resume_id or uuid4().hex
                try:
                    ownership = _StateDirJobLock.acquire(self.state_dir, run_id, _KINDS[code])
                except RuntimeError as error:
                    raise OperationBusy("search") from error
                with connect_api2(self.state_dir) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    milestone = catalog.milestone(conn, None)
                    checkpoint = {"revision": revision, "trail": []} if code == _GP else None
                    started_at = None
                    engine = dict(conn.execute("SELECT id, code, name FROM engines WHERE code=?", (code,)).fetchone())
                    if prior is not None:
                        actual = self._resume_input(conn, run_id)
                        checkpoint = _checkpoint(actual)
                        if tuple(actual) != tuple(prior) or checkpoint["revision"] != revision:
                            raise SearchError(409, "state_conflict", "Позиция продолжения изменилась",
                                              "checkpoint_unavailable")
                        started_at = actual["started_at"]
                        conn.execute("UPDATE runs SET status='starting', error_json=NULL, finished_at=NULL WHERE id=?",
                                     (run_id,))
                    else:
                        preset = self._matching_preset(conn, spec)
                        conn.execute(
                            """INSERT INTO runs(id, milestone_id, engine_id, status, parameters_json,
                                               domains_json, preset_id, checkpoint_json)
                               VALUES(?, ?, ?, 'starting', ?, ?, ?, ?)""",
                            (run_id, milestone, engine["id"], json.dumps(spec["parameters"]),
                             json.dumps(spec["domains"]), preset, json.dumps(checkpoint) if checkpoint else None),
                        )
                summary = {"id": run_id, "engine": engine, "milestone_id": milestone,
                           "status": "starting", "error": None, "started_at": started_at,
                           "progress": {"checked": len(checkpoint["trail"]) if checkpoint else None,
                                        "total": None, "current_args": None}}
                if len(self.logs) >= 16 and run_id not in self.logs:
                    self.logs.pop(next(iter(self.logs)))
                journal = self.logs.setdefault(run_id, RunLog(run_id))
                job = Execution(self.state_dir, run_id, code, spec["parameters"], spec["domains"], checkpoint,
                                summary, journal, self.lock, self._publish)
                self.active = job
                worker = Thread(target=self._run, args=(job, slot, ownership), name="api2-search", daemon=True)
                try:
                    worker.start()
                except RuntimeError:
                    error = {"code": "engine_failed", "message": "Не удалось запустить поиск"}
                    with connect_api2(self.state_dir) as conn:
                        conn.execute("UPDATE runs SET status='failed', error_json=?, finished_at=? WHERE id=?",
                                     (json.dumps(error), now_iso(), run_id))
                    raise
                self._publish()
                return {"id": run_id, "status": summary["status"]}
        except BaseException:
            if job is not None and self.active is job:
                self.active = None
            if ownership is not None:
                ownership.release()
            slot.__exit__(None, None, None)
            raise

    @staticmethod
    def _matching_preset(conn, spec):
        preset = spec["domain_list_id"]
        if preset is None:
            return None
        if conn.execute("SELECT 1 FROM domain_presets WHERE id=?", (preset,)).fetchone() is None:
            raise SearchError(404, "not_found", "Список доменов не найден")
        names = {row[0] for row in conn.execute(
            "SELECT d.name FROM preset_domains p JOIN domains d ON d.id=p.domain_id WHERE p.preset_id=?", (preset,)
        )}
        return preset if names == set(spec["domains"]) else None

    def _run(self, job, slot, ownership):
        final = "failed"
        error = None
        quarantined = False
        nft_table = None
        try:
            with self.lock:
                if job.cancel.is_set():
                    final = "stopped"
                else:
                    started = job.summary["started_at"] or now_iso()
                    with connect_api2(self.state_dir) as conn:
                        conn.execute("UPDATE runs SET status='running', started_at=? WHERE id=?", (started, job.run_id))
                    job.summary.update(status="running", started_at=started)
                    self._publish()
                    update_state(self.state_dir, lambda state: {
                        **state, "current_run_id": job.run_id, "current_run_name": _KINDS[job.code],
                        "current_run_status": "running", "current_run_nft_table": None,
                    })
            if final != "stopped":
                final = self.driver.run(job)
                if final not in search_history.TERMINAL:
                    raise RuntimeError("Invalid terminal engine result")
                if final == "failed":
                    error = {"code": "engine_failed", "message": "Поиск завершился ошибкой"}
        except Exception as exc:
            final = "failed"
            quarantined = isinstance(exc, ManagedRuntimeQuarantinedError)
            if quarantined:
                nft_table = exc.nft_table or job.nft_table
            error = {"code": "cleanup_unverified" if quarantined else "engine_failed",
                     "message": "Очистка ресурсов не подтверждена" if quarantined else "Поиск завершился ошибкой"}
            _LOG.error("Search %s failed: %s", job.run_id, type(exc).__name__)
        finally:
            with self.lock:
                try:
                    if quarantined:
                        update_state(self.state_dir, lambda state: {
                            **state, "current_run_status": "quarantined", "current_run_nft_table": nft_table,
                        })
                        with connect_api2(self.state_dir) as conn:
                            conn.execute("UPDATE runs SET status='stopping', error_json=? WHERE id=?",
                                         (json.dumps(error), job.run_id))
                        job.summary.update(status="stopping", error=error)
                    else:
                        update_state(self.state_dir, lambda state: {
                            **state, "current_run_id": None, "current_run_name": None, "current_run_status": None,
                            "current_run_nft_table": None,
                        })
                        ownership.release()
                        finished = now_iso()
                        with connect_api2(self.state_dir) as conn:
                            conn.execute("UPDATE runs SET status=?, error_json=?, finished_at=? WHERE id=?",
                                         (final, json.dumps(error) if error else None, finished, job.run_id))
                        job.summary.update(status=final, error=error, finished_at=finished)
                        job.cleanup_confirmed = True
                except Exception:
                    _LOG.exception("Failed to finalize search %s", job.run_id)
                finally:
                    if job.cleanup_confirmed:
                        self.active = None
                        slot.__exit__(None, None, None)
                    else:
                        job.summary["status"] = "stopping"
                        job.summary.pop("finished_at", None)
                    self._publish()
                    job.done.set()

    def current(self):
        with self.lock:
            return {"active": self.active is not None, "run": self._summary(self.active)}

    @staticmethod
    def _summary(job):
        return None if job is None else {**job.summary, "progress": dict(job.summary["progress"])}

    def stop(self):
        with self.lock:
            job = self.active
            if job is None:
                return {"run": None}
            if not job.done.is_set() and not job.cancel.is_set():
                with connect_api2(self.state_dir) as conn:
                    conn.execute("UPDATE runs SET status='stopping' WHERE id=?", (job.run_id,))
                job.summary["status"] = "stopping"
                job.cancel.set()
                self._publish()
        if not job.done.wait(25) or not job.cleanup_confirmed:
            raise SearchError(503, "cleanup_unverified", "Окончание поиска не подтверждено")
        return {"run": self._summary(job)}

    def subscribe(self):
        with self.lock:
            key = uuid4().hex
            self.subscribers[key] = deque([self.current()], maxlen=32)
            return key

    def unsubscribe(self, key):
        with self.lock:
            self.subscribers.pop(key, None)

    def _publish(self):
        snapshot = self.current()
        for key, pending in self.subscribers.items():
            if pending is not None:
                if len(pending) == pending.maxlen:
                    self.subscribers[key] = None
                elif not pending or pending[-1] != snapshot:
                    pending.append(snapshot)

    def drain(self, key):
        with self.lock:
            pending = self.subscribers.get(key)
            if pending is None:
                return [], True
            snapshots = list(pending)
            pending.clear()
            return snapshots, False

    def log_page(self, run_id, after, limit):
        with connect_api2(self.state_dir) as conn:
            if search_history.read(conn, run_id) is None:
                raise SearchError(404, "not_found", "Поиск не найден")
        with self.lock:
            journal = self.logs.get(run_id)
        cursor = RunLog.cursor(run_id, after) if after is not None else None
        live = journal.read(after, limit) if journal is not None else None
        if live is not None and not live["truncated"]:
            return live
        if re.fullmatch("[a-f0-9]{32}", run_id):
            path = self.state_dir / "strategy-finder" / "diagnostics" / f"{run_id}.log"
            saved = _saved_log(path, run_id, cursor, limit)
            if saved is not None:
                return saved
        return live or {"entries": [], "next_after": None, "truncated": True}
