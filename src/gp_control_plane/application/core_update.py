"""Persist one update report across Core restarts and launch the fixed installer."""

import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from threading import RLock, Thread
from uuid import UUID

from packaging.version import Version

from .. import __version__, core_supply
from ..backups import _write_private_json_atomic
from ..zapret2 import DEFAULT_ROOT_HELPER
from .operation_admission import OperationBusy


_LOG = logging.getLogger(__name__)
_SCOPE = "gp-control-plane-core-update.scope"
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:(a|b|rc)(0|[1-9][0-9]*))?(?:\.post(0|[1-9][0-9]*))?")


class UpdateError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def update_identity(value):
    try:
        canonical = str(UUID(value))
    except (AttributeError, ValueError, TypeError):
        return None
    return canonical if value.lower() == canonical else None


def version_key(version):
    match = _VERSION.fullmatch(version) if len(version) <= 64 else None
    if match is None:
        return None
    return Version(version)


def _probe_scope():
    if not sys.platform.startswith("linux"):
        return False
    try:
        check = subprocess.run(
            ["/usr/bin/systemctl", "show", "--property=LoadState", "--property=ActiveState", _SCOPE],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    properties = dict(line.split("=", 1) for line in check.stdout.splitlines() if "=" in line)
    load_state, active_state = properties.get("LoadState"), properties.get("ActiveState")
    if load_state == "loaded" and active_state in {"active", "activating", "deactivating", "reloading"}:
        return True
    if load_state in {"loaded", "not-found"} and active_state in {"inactive", "failed"}:
        return False
    return None


def _launch(update_id, version):
    if not sys.platform.startswith("linux"):
        raise OSError("Core update requires Linux")
    helper = Path(DEFAULT_ROOT_HELPER)
    if not helper.is_file():
        raise OSError("Installed Core helper is unavailable")
    command = [str(helper), "core-update", update_id, version]
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            raise OSError("sudo is unavailable")
        command[:0] = [sudo, "-n", "--"]
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)


class CoreUpdate:
    def __init__(self, state_dir, admission, *, prepare=core_supply.for_update, launch=_launch, probe=_probe_scope):
        self.path = Path(state_dir) / "strategy-finder" / "core-update.json"
        self.state_dir = state_dir
        self.admission = admission
        self.prepare = prepare
        self.launch = launch
        self.probe = probe
        self.guard = RLock()
        self._worker = None
        self._launching = False
        admission.set_external(self.external_operation)

    def _saved(self):
        try:
            report = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if (not isinstance(report, dict) or not update_identity(report.get("update_id"))
                or report.get("state") not in {"running", "failed", "succeeded", "unknown"}
                or not isinstance(report.get("target_version"), str) or "error" not in report):
            raise ValueError("Invalid Core update report")
        return report

    def external_operation(self):
        # There is no installer to recover before the first durable request.
        if not self.path.exists():
            return None
        return "core_update" if self.probe() is not False else None

    def _view(self, report):
        if report is None or report["state"] != "running":
            return report
        if self._launching or self.probe() is True:
            return report
        return {**report, "state": "unknown",
                "error": {"code": "update_outcome_unknown", "message": "Результат установки не подтверждён"}}

    def read(self, update_id):
        with self.guard:
            report = self._saved()
            if report is None or report["update_id"] != update_id:
                raise UpdateError(404, "not_found", "Отчёт обновления не найден")
            return self._view(report)

    def _known(self, update_id, version, tag):
        report = self._saved()
        if report is None or report["update_id"] != update_id:
            return None
        if report["target_version"] != version or report.get("tag") != tag:
            raise UpdateError(409, "state_conflict", "Для этого обновления сохранена другая версия или стадия")
        return self._view(report)

    def start(self, update_id, version, tag):
        core_supply._pair(version, tag)
        with self.guard:
            known = self._known(update_id, version, tag)
            if known is not None:
                return 200, known
            active = self.admission.active
            if active:
                raise OperationBusy(active)
        target = version_key(version)
        if target is None:
            raise UpdateError(404, "not_found", "Точная поставка Core не найдена")
        current = version_key(__version__)
        if current is None:
            raise UpdateError(503, "update_unavailable", "Не удалось определить установленную версию")
        if target < current:
            raise UpdateError(409, "downgrade_forbidden", "Понижение версии Core запрещено")
        try:
            supply = self.prepare(self.state_dir, version, tag)
        except (OSError, ValueError):
            with self.guard:
                known = self._known(update_id, version, tag)
                if known is not None:
                    return 200, known
            raise
        if supply is None:
            with self.guard:
                known = self._known(update_id, version, tag)
                if known is not None:
                    return 200, known
            raise UpdateError(404, "not_found", "Точная поставка Core не найдена")
        with self.guard:
            known = self._known(update_id, version, tag)
            if known is not None:
                return 200, known
            slot = self.admission.claim("core_update")
            slot.__enter__()
            report = {"update_id": update_id, "target_version": version, "tag": tag,
                      "supply": supply, "state": "running", "error": None}
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _write_private_json_atomic(self.path, report)
                self._launching = True
                try:
                    process = self.launch(update_id, version)
                except OSError as error:
                    failed = {**report, "state": "failed",
                              "error": {"code": "installer_unavailable", "message": "Установщик не запущен"}}
                    _write_private_json_atomic(self.path, failed)
                    raise UpdateError(503, "installer_unavailable", "Установщик не запущен") from error
            except BaseException:
                self._launching = False
                slot.__exit__(None, None, None)
                raise
            self._worker = Thread(target=self._watch, args=(process, slot, report),
                                  name="api2-core-update", daemon=True)
            self._worker.start()
            return 202, report

    def _watch(self, process, slot, accepted):
        try:
            code = process.wait()
            if type(code) is int and code > 0:
                with self.guard:
                    if self.probe() is False and self._saved() == accepted:
                        failed = {**accepted, "state": "failed",
                                  "error": {"code": "installation_failed", "message": "Установщик завершился ошибкой"}}
                        _write_private_json_atomic(self.path, failed)
        except (OSError, ValueError):
            _LOG.exception("Could not reconcile Core installer exit")
        finally:
            with self.guard:
                self._launching = False
                slot.__exit__(None, None, None)
