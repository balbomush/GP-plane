"""BGT-001 Playwright bridge used by the fourteen browser product scenarios."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Any


_REPOSITORY = Path(__file__).resolve().parents[2]
_WORKSPACE = _REPOSITORY.parents[1]
_LEDGER = Path(os.environ["GP_BROWSER_ARTIFACTS"]) if os.environ.get("GP_BROWSER_ARTIFACTS") else _WORKSPACE / "release-ledger" / "attempts" / "v0.4.1"
_LOCK_PATH = Path(__file__).with_name("toolchain.lock.json")


def _load_toolchain_lock() -> dict[str, Any]:
    try:
        lock = json.loads(_LOCK_PATH.read_text(encoding="utf-8"))
        if lock["schema"] != 1:
            raise ValueError(f"unsupported schema {lock['schema']!r}")
        for key in ("runtimeId", "node", "playwright", "browser"):
            if not lock.get(key):
                raise ValueError(f"missing {key}")
        return lock
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise AssertionError(f"BGT-001 infrastructure failure: invalid toolchain lock: {error}") from error


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_toolchain(runtime: Path, lock: dict[str, Any]) -> tuple[Path, Path]:
    node = runtime / "node" / "node.exe"
    package = lock["playwright"]["package"]
    module = runtime / "playwright-project" / "node_modules" / package / "package.json"
    expected_node = lock["node"]["nodeExeSha256"]
    expected_playwright = lock["playwright"]["version"]
    revision = str(lock["browser"]["revision"])
    if not node.is_file():
        raise AssertionError(f"BGT-001 infrastructure failure: pinned Node is unavailable: {node}")
    actual_node = _file_sha256(node)
    if actual_node != expected_node:
        raise AssertionError(
            f"BGT-001 infrastructure failure: Node SHA-256 mismatch: expected {expected_node}, actual {actual_node}"
        )
    try:
        actual_playwright = json.loads(module.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise AssertionError(f"BGT-001 infrastructure failure: Playwright package is unavailable: {error}") from error
    if actual_playwright != expected_playwright:
        raise AssertionError(
            f"BGT-001 infrastructure failure: Playwright version mismatch: expected {expected_playwright}, actual {actual_playwright}"
        )
    chromium = runtime / "browsers" / f"chromium-{revision}"
    if not chromium.is_dir():
        raise AssertionError(
            f"BGT-001 infrastructure failure: Chromium revision mismatch: expected {revision}, missing {chromium}"
        )
    return node, module


class PlaywrightPage:
    """One persistent Chromium profile per unittest case, always reaped."""

    def __init__(self) -> None:
        self._lock = _load_toolchain_lock()
        self._runtime = _WORKSPACE / "runtime" / "bgt-001" / self._lock["runtimeId"]
        node, _module = _validate_toolchain(self._runtime, self._lock)
        run_id = uuid.uuid4().hex
        self._artifact_dir = _LEDGER / f"bgt-001-{run_id}"
        self._profile_dir = self._runtime / "profiles" / run_id
        self._closed = False
        environment = os.environ | {
            "BGT001_RUNTIME": str(self._runtime),
            "BGT001_NODE_MODULES": str(self._runtime / "playwright-project" / "node_modules"),
            "BGT001_PLAYWRIGHT_PACKAGE": self._lock["playwright"]["package"],
            "BGT001_ARTIFACTS": str(self._artifact_dir),
            "BGT001_PROFILE": str(self._profile_dir),
            "PLAYWRIGHT_BROWSERS_PATH": str(self._runtime / "browsers"),
        }
        self._process = subprocess.Popen(
            [str(node), str(Path(__file__).with_name("playwright_bridge.mjs"))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        self._next_id = 0

    def __enter__(self) -> "PlaywrightPage":
        try:
            self._call("open")
        except BaseException as error:
            self._attach_cleanup_note(error, self._shutdown())
            raise
        return self

    def __exit__(self, _type: object, value: BaseException | None, _traceback: object) -> None:
        cleanup_errors = self._shutdown()
        if value is not None:
            self._attach_cleanup_note(value, cleanup_errors)
            return
        if cleanup_errors:
            raise AssertionError("BGT-001 browser cleanup failed: " + "; ".join(cleanup_errors))
        if self._artifact_dir.exists():
            shutil.rmtree(self._artifact_dir)

    def navigate(self, url: str) -> None:
        self._call("goto", url=url)

    def set_viewport(self, width: int, height: int = 900) -> None:
        self._call("viewport", width=width, height=height)

    def press_key(self, key: str, modifiers: int = 0) -> None:
        self._call("key", key=(f"Shift+{key}" if modifiers & 8 else key))

    def move_pointer_to(self, x: float, y: float) -> None:
        self._call("pointer", x=x, y=y)

    def pointer_down(self, x: float, y: float) -> None:
        self.move_pointer_to(x, y)
        self._call("pointerDown")

    def pointer_up(self, x: float, y: float) -> None:
        self.move_pointer_to(x, y)
        self._call("pointerUp")

    def click(self, selector: str, timeout: float = 10) -> None:
        self._call("click", selector=selector, timeoutMs=int(timeout * 1000))

    def double_click(self, selector: str, timeout: float = 10) -> None:
        self._call("doubleClick", selector=selector, timeoutMs=int(timeout * 1000))

    def fill(self, selector: str, value: str, timeout: float = 10) -> None:
        self._call("fill", selector=selector, value=value, timeoutMs=int(timeout * 1000))

    def evaluate(self, expression: str) -> Any:
        return self._call("evaluate", expression=expression)

    def wait_for(self, expression: str, description: str, timeout: float = 10, diagnostics: str | None = None) -> None:
        try:
            self._call("wait", expression=expression, timeoutMs=int(timeout * 1000))
        except AssertionError as error:
            detail = self.evaluate(diagnostics) if diagnostics else None
            raise AssertionError(f"browser condition did not become true: {description}; diagnostics: {detail!r}") from error

    def _shutdown(self) -> list[str]:
        if self._closed:
            return []
        self._closed = True
        errors: list[str] = []
        try:
            if self._process.poll() is None:
                self._call("close")
        except BaseException as error:
            errors.append(f"bridge close: {error}")
        finally:
            if self._process.stdin:
                try:
                    self._process.stdin.close()
                except BaseException as error:
                    errors.append(f"bridge stdin close: {error}")
            try:
                self._process.wait(timeout=15)
            except BaseException as error:
                errors.append(f"bridge wait: {error}")
                try:
                    self._process.kill()
                except BaseException as kill_error:
                    errors.append(f"bridge kill: {kill_error}")
                try:
                    self._process.wait(timeout=15)
                except BaseException as wait_error:
                    errors.append(f"bridge wait after kill: {wait_error}")
            try:
                stderr = self._process.stderr.read() if self._process.stderr else ""
            except BaseException as error:
                stderr = ""
                errors.append(f"bridge stderr read: {error}")
            if self._process.stdout:
                try:
                    self._process.stdout.close()
                except BaseException as error:
                    errors.append(f"bridge stdout close: {error}")
            if self._process.stderr:
                try:
                    self._process.stderr.close()
                except BaseException as error:
                    errors.append(f"bridge stderr close: {error}")
            if self._process.returncode:
                errors.append(f"bridge exit {self._process.returncode}: {stderr}")
            try:
                shutil.rmtree(self._profile_dir)
            except FileNotFoundError:
                pass
            except OSError as error:
                errors.append(f"profile cleanup: {error}")
        return errors

    @staticmethod
    def _attach_cleanup_note(error: BaseException, cleanup_errors: list[str]) -> None:
        if cleanup_errors and hasattr(error, "add_note"):
            error.add_note("BGT-001 cleanup: " + "; ".join(cleanup_errors))

    def _call(self, method: str, **params: Any) -> Any:
        if not self._process.stdin or not self._process.stdout:
            raise AssertionError("BGT-001 browser bridge is closed")
        self._next_id += 1
        self._process.stdin.write(json.dumps({"id": self._next_id, "method": method, "params": params}) + "\n")
        self._process.stdin.flush()
        line = self._process.stdout.readline()
        if not line:
            stderr = self._process.stderr.read() if self._process.stderr else ""
            raise AssertionError(f"BGT-001 browser bridge stopped unexpectedly: {stderr}")
        response = json.loads(line)
        if not response.get("ok"):
            raise AssertionError(f"BGT-001 Playwright failure: {response.get('error')}")
        return response.get("result")
