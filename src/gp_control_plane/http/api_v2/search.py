"""Public search lifecycle and the shared Core-owned SSE stream."""

from collections.abc import AsyncIterator, Iterator
from functools import wraps
from threading import BoundedSemaphore, Event
from time import monotonic

from anyio import sleep, to_thread

from fastapi import Depends, FastAPI, Request, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent

from ...application.api2_search import SearchError
from .access import authorized, require_access
from .openapi import describe
from .protocol import ApiError, as_api_error, detail, invalid_parameters, json_body, no_body, query_fields
from .query import Query
from .search_input import validate_start


def register_search(app: FastAPI, state_dir, search, *, shutdown: Event, json_limit: int):
    authorize = authorized(state_dir)
    streams = BoundedSemaphore(2)

    def checked(action):
        @wraps(action)
        @authorize
        def call(*args, **kwargs):
            try:
                return action(*args, **kwargs)
            except SearchError as error:
                details = None
                if error.reason:
                    if error.code == "invalid_parameters":
                        field = "query:after" if error.reason == "after" else "body:/" + error.reason
                        details = [detail(field, "invalid_value", str(error))]
                    else:
                        details = [{"code": error.reason}]
                raise ApiError(error.status, error.code, str(error), details) from error
        return call

    def start(request: Request, code):
        _, errors = query_fields(request, set())
        spec, problems = validate_start(code, json_body(request, json_limit))
        invalid_parameters(errors + problems)
        return search.start(code, spec)

    @app.post("/api/v2/engines/blockcheck2/start", **describe("blockcheck2Start"))
    @checked
    def stock(request: Request):
        return start(request, "blockcheck2")

    @app.post("/api/v2/engines/blockcheck2-gp/start", **describe("blockcheck2GpStart"))
    @checked
    def gp(request: Request):
        return start(request, "blockcheck2-gp")

    @app.post("/api/v2/engines/blockcheck2-gp/runs/{run_id}/resume", **describe("blockcheck2GpResume"))
    @checked
    def resume(request: Request, run_id: str):
        no_body(request)
        return search.start("blockcheck2-gp", None, resume_id=run_id)

    @app.get("/api/v2/search/current", **describe("getCurrentSearch"))
    @checked
    def current(request: Request):
        no_body(request)
        return search.current()

    @app.post("/api/v2/search/stop", **describe("stopCurrentSearch"))
    @checked
    def stop(request: Request):
        no_body(request)
        return search.stop()

    @app.get("/api/v2/data/runs/{run_id}/log", **describe("readRunLog"))
    @checked
    def logs(request: Request, run_id: str):
        query = Query(request, {"after", "limit"})
        after = query.text("after")
        limit = query.integer("limit", default=50, minimum=1, maximum=200)
        query.check()
        return search.log_page(run_id, after, limit)

    def subscription(request: Request, response: Response) -> Iterator[tuple[str, str | None]]:
        authorization = request.headers.get("Authorization")
        try:
            require_access(state_dir, authorization)
            no_body(request)
            if not streams.acquire(blocking=False):
                raise ApiError(503, "stream_capacity_exhausted", "Достигнут предел подключений SSE")
            try:
                snapshots = search.subscribe()
            except BaseException:
                streams.release()
                raise
        except Exception as error:
            raise as_api_error("watchCurrentSearch", error) from None
        try:
            response.headers["Cache-Control"] = "no-store"
            yield snapshots, authorization
        finally:
            try:
                search.unsubscribe(snapshots)
            except Exception as error:
                as_api_error("watchCurrentSearch.unsubscribe", error)
            finally:
                streams.release()

    @app.get("/api/v2/search/events", **describe("watchCurrentSearch", response_class=EventSourceResponse))
    async def events(stream: tuple[str, str | None] = Depends(subscription)) -> AsyncIterator[ServerSentEvent]:
        snapshots, authorization = stream
        heartbeat = monotonic()
        while not shutdown.is_set():
            try:
                await to_thread.run_sync(require_access, state_dir, authorization)
                available, overflow = await to_thread.run_sync(search.drain, snapshots)
                if overflow:
                    return
                for snapshot in available:
                    await to_thread.run_sync(require_access, state_dir, authorization)
                    yield ServerSentEvent(event="search", data=snapshot)
                if monotonic() - heartbeat >= 5:
                    yield ServerSentEvent(comment="keepalive")
                    heartbeat = monotonic()
                await sleep(0.25)
            except Exception as error:
                as_api_error("watchCurrentSearch", error)
                return
