"""Public search lifecycle and the shared Core-owned SSE stream."""

from functools import wraps
import json
from threading import Event
from time import monotonic

from bottle import HTTPResponse, request

from ...application.api2_search import SearchError
from ..core_app import _stream_peer
from ..stream_admission import ClosingIterator, StreamAdmission
from .access import authorized, require_access
from .protocol import ApiError, detail, invalid_parameters, json_body, json_response, no_body, query_fields
from .query import Query
from .search_input import validate_start


def register_search(app, state_dir, search, *, stream_lifecycle=None, json_limit):
    authorize = authorized(state_dir)
    streams = StreamAdmission()

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

    def start(code):
        _, errors = query_fields(set())
        spec, problems = validate_start(code, json_body(json_limit))
        invalid_parameters(errors + problems)
        return json_response(search.start(code, spec))

    @app.post("/api/v2/engines/blockcheck2/start")
    @checked
    def stock():
        return start("blockcheck2")

    @app.post("/api/v2/engines/blockcheck2-gp/start")
    @checked
    def gp():
        return start("blockcheck2-gp")

    @app.post("/api/v2/engines/blockcheck2-gp/runs/<run_id>/resume")
    @checked
    def resume(run_id):
        no_body()
        return json_response(search.start("blockcheck2-gp", None, resume_id=run_id))

    @app.get("/api/v2/search/current")
    @checked
    def current():
        no_body()
        return json_response(search.current())

    @app.post("/api/v2/search/stop")
    @checked
    def stop():
        no_body()
        return json_response(search.stop())

    @app.get("/api/v2/data/runs/<run_id>/log")
    @checked
    def logs(run_id):
        query = Query({"after", "limit"})
        after = query.text("after")
        limit = query.integer("limit", default=100, minimum=1, maximum=200)
        query.check()
        return json_response(search.log_page(run_id, after, limit))

    @app.get("/api/v2/search/events")
    @checked
    def events():
        no_body()
        permit = streams.acquire("sse")
        if permit is None:
            raise ApiError(503, "stream_capacity_exhausted", "Достигнут предел подключений SSE")
        authorization = request.headers.get("Authorization")
        peer = _stream_peer(request.environ)
        terminal = Event()
        close = terminal.set
        stream_id = stream_lifecycle.register(peer, close) if stream_lifecycle is not None else None
        snapshots = search.subscribe()

        def release():
            close()
            search.unsubscribe(snapshots)
            if stream_lifecycle is not None:
                stream_lifecycle.release(stream_id, peer, close)
            permit.release()

        def stream():
            heartbeat = monotonic()
            while not terminal.is_set():
                try:
                    require_access(state_dir, authorization)
                    available, overflow = search.drain(snapshots)
                    if overflow:
                        return
                    for snapshot in available:
                        require_access(state_dir, authorization)
                        data = json.dumps(snapshot, separators=(",", ":"), allow_nan=False)
                        yield f"event: search\ndata: {data}\n\n".encode("utf-8")
                    if monotonic() - heartbeat >= 5:
                        yield b": keepalive\n\n"
                        heartbeat = monotonic()
                    terminal.wait(0.25)
                except ApiError:
                    return

        return HTTPResponse(ClosingIterator(stream(), release), headers={
            "Content-Type": "text/event-stream", "Cache-Control": "no-store",
        })
