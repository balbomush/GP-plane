"""Milestones and stored runs share the same database as discovery."""

from collections.abc import Callable
from pathlib import Path
import sqlite3

from fastapi import FastAPI, Request
from fastapi.responses import Response

from ...application.operation_admission import OperationAdmission, OperationBusy
from ...repositories import catalog, search_history
from ...storage import open_api2
from .access import authorized
from .openapi import describe
from .protocol import ApiError, no_body
from .query import Query


def register_history(
    app: FastAPI, state_dir: Path, *, resume_available: Callable[[sqlite3.Row, int], bool],
    admission: OperationAdmission | None = None,
) -> None:
    if admission is None:
        admission = OperationAdmission()
    authorize = authorized(state_dir)

    def require_run(conn, run_id):
        row = search_history.read(conn, run_id)
        if row is None:
            raise ApiError(404, "not_found", "Поиск не найден")
        return row

    @app.get("/api/v2/data/runs", **describe("listRuns"))
    @authorize
    def runs(request: Request):
        query = Query(request, {"offset", "limit", "status", "engine_code", "milestone_id"})
        offset, limit = query.page()
        filters = {
            "status": query.text("status", values=set(search_history.ACTIVE + search_history.TERMINAL)),
            "engine_code": query.text("engine_code"),
            "milestone_id": query.integer("milestone_id"),
        }
        query.check()
        with open_api2(state_dir) as conn:
            conn.execute("BEGIN")
            rows, total = search_history.page(conn, filters, offset, limit)
            current = catalog.milestone(conn, None)
            items = [search_history.view(row, resumable=resume_available(row, current)) for row in rows]
            return {"items": items, "total": total, "offset": offset, "limit": limit}

    @app.get("/api/v2/data/runs/{run_id}", **describe("getRun"))
    @authorize
    def get_run(request: Request, run_id: str):
        Query(request, set()).check()
        with open_api2(state_dir) as conn:
            conn.execute("BEGIN")
            row = require_run(conn, run_id)
            current = catalog.milestone(conn, None)
            return search_history.view(row, resumable=resume_available(row, current))

    @app.delete("/api/v2/data/runs/{run_id}", **describe("deleteRunHistory"))
    @authorize
    def delete_run(request: Request, run_id: str):
        no_body(request)
        try:
            with admission.user_write(), open_api2(state_dir) as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = search_history.read(conn, run_id)
                if row is not None and row["status"] not in search_history.TERMINAL:
                    raise ApiError(409, "state_conflict", "Активный поиск нельзя удалить")
                conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
        except OperationBusy as error:
            raise ApiError(409, "state_conflict", "Пользовательские данные восстанавливаются") from error
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.get("/api/v2/data/milestones", **describe("listMilestones"))
    @authorize
    def milestones(request: Request):
        query = Query(request, {"offset", "limit"})
        offset, limit = query.page()
        query.check()
        with open_api2(state_dir) as conn:
            conn.execute("BEGIN")
            total = conn.execute("SELECT count(*) FROM milestones").fetchone()[0]
            rows = conn.execute("SELECT id FROM milestones ORDER BY id LIMIT ? OFFSET ?", (limit, offset))
            return {
                "current_id": catalog.milestone(conn, None), "items": [dict(row) for row in rows],
                "total": total, "offset": offset, "limit": limit,
            }

    @app.post("/api/v2/data/milestones", **describe("createMilestone"))
    @authorize
    def create_milestone(request: Request):
        no_body(request)
        try:
            with admission.user_write(), open_api2(state_dir) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if search_history.active(conn):
                    raise ApiError(409, "state_conflict", "Дождитесь окончания активного поиска")
                next_id = catalog.milestone(conn, None) + 1
                conn.execute("INSERT INTO milestones(id) VALUES(?)", (next_id,))
        except OperationBusy as error:
            raise ApiError(409, "state_conflict", "Пользовательские данные восстанавливаются") from error
        return {"id": next_id}
