"""User backup methods with admission before upload and cleanup before release."""

from collections.abc import Iterable
from contextlib import ExitStack, contextmanager
from threading import Lock
from pathlib import Path
import re
import shutil
import sqlite3
from tempfile import mkdtemp

from bottle import Bottle, HTTPResponse, request

from ... import user_backup
from ...application.operation_admission import OperationAdmission
from ...resource_budget import BACKUP_STREAM_CHUNK_BYTES, BACKUP_UPLOAD_MAX_BYTES
from ..stream_admission import ClosingIterator
from .access import authorized
from .protocol import ApiError, json_response, no_body
from .query import Query


_ROOT = "/api/v2/backup"


def _file_body_size(limit):
    media_type = request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if media_type != "application/octet-stream":
        raise ApiError(415, "unsupported_media_type", "Нужен Content-Type application/octet-stream")
    raw = request.environ.get("CONTENT_LENGTH", "")
    if request.environ.get("HTTP_TRANSFER_ENCODING") or not re.fullmatch(r"[0-9]{1,19}", raw):
        raise ApiError(400, "invalid_request", "Нужен корректный размер архива")
    size = int(raw)
    if size > limit:
        raise ApiError(413, "payload_too_large", "Превышен допустимый размер архива")
    if size == 0:
        raise ApiError(400, "invalid_request", "Архив пуст")
    return size


def _receive(destination, size):
    remaining = size
    stream = request.environ["wsgi.input"]
    with destination.open("xb") as output:
        while remaining:
            try:
                data = stream.read(min(remaining, BACKUP_STREAM_CHUNK_BYTES))
            except TimeoutError as error:
                raise ApiError(408, "request_timeout", "Приём архива остановился") from error
            except OSError as error:
                raise ApiError(400, "invalid_request", "Не удалось принять архив") from error
            if not data or len(data) > remaining:
                raise ApiError(400, "invalid_request", "Не удалось принять полный архив")
            output.write(data)
            remaining -= len(data)


def _blocks(file) -> Iterable[bytes]:
    while block := file.read(BACKUP_STREAM_CHUNK_BYTES):
        yield block


def register_backups(
    app: Bottle, state_dir: Path, *, admission: OperationAdmission | None = None,
    upload_limit: int = BACKUP_UPLOAD_MAX_BYTES,
) -> None:
    if admission is None:
        admission = OperationAdmission()
    authorize = authorized(state_dir)
    saved = user_backup.saved_path(state_dir)
    saved_lock = Lock()

    @contextmanager
    def saved_file():
        if not saved_lock.acquire(blocking=False):
            raise ApiError(409, "state_conflict", "Сохранённая копия используется")
        try:
            yield
        finally:
            saved_lock.release()

    def restore_at(upload_size=None):
        restored = False
        legacy = None
        if upload_size is None and not saved.is_file():
            legacy = user_backup.legacy_saved(state_dir)
            if legacy is None:
                raise ApiError(404, "not_found", "Сохранённой копии нет")
        saved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = Path(mkdtemp(prefix=".user-restore-", dir=saved.parent))
        response = {"restored": True}
        try:
            archive = saved
            if upload_size is not None:
                archive = stage / "incoming.zip"
                _receive(archive, upload_size)
            try:
                if legacy is None:
                    user_backup.restore(state_dir, archive, stage)
                else:
                    user_backup.restore_legacy_saved(state_dir, legacy, stage)
            except user_backup.BackupTooLarge as error:
                raise ApiError(413, "payload_too_large", "Распакованный архив превышает допустимый размер") from error
            except (ValueError, FileNotFoundError, sqlite3.IntegrityError) as error:
                raise ApiError(409, "archive_incomplete", "Архив не содержит полный пользовательский набор") from error
            restored = True
        finally:
            try:
                shutil.rmtree(stage)
            except OSError:
                if restored:
                    response["warnings"] = [{
                        "code": "temporary_cleanup_failed", "message": "Не удалось убрать временный архив",
                    }]
        return json_response(response)

    @app.post(_ROOT)
    @authorize
    def create():
        no_body()
        with admission.claim("backup_create"), saved_file():
            try:
                return json_response(user_backup.create(state_dir))
            except user_backup.BackupTooLarge as error:
                raise ApiError(413, "payload_too_large", "Копия превышает допустимый размер") from error

    @app.delete(_ROOT)
    @authorize
    def delete():
        no_body()
        with saved_file():
            user_backup.delete_saved(state_dir)
        return HTTPResponse(status=204, headers={"Cache-Control": "no-store"})

    @app.get(_ROOT + "/file")
    @authorize
    def download():
        no_body()
        stack = ExitStack()
        try:
            stack.enter_context(admission.claim("archive_download"))
            stack.enter_context(saved_file())
            try:
                file = stack.enter_context(saved.open("rb"))
            except FileNotFoundError as error:
                if user_backup.legacy_saved(state_dir) is not None:
                    raise ApiError(409, "archive_incomplete", "Прежняя копия содержит служебные данные") from error
                raise ApiError(404, "not_found", "Сохранённой копии нет") from error
            try:
                user_backup.validate_download(file, saved.parent)
            except ValueError as error:
                raise ApiError(409, "archive_incomplete", "Сохранённая копия не готова к безопасной выдаче") from error
            file.seek(0, 2)
            length = file.tell()
            file.seek(0)
            return HTTPResponse(ClosingIterator(_blocks(file), stack.close), headers={
                "Content-Type": "application/octet-stream", "Content-Length": str(length), "Cache-Control": "no-store",
                "Content-Disposition": 'attachment; filename="gp-plane-backup.zip"',
            })
        except BaseException:
            stack.close()
            raise

    @app.post(_ROOT + "/restore")
    @authorize
    def restore():
        no_body()
        with admission.claim("backup_restore"), saved_file():
            return restore_at()

    @app.post(_ROOT + "/restore-file")
    @authorize
    def restore_file():
        Query(set(), accepts_body=True).check()
        size = _file_body_size(upload_limit)
        with admission.claim("backup_restore"):
            return restore_at(size)
