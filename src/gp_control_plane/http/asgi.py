"""One Core ASGI server with bounded shutdown."""

from threading import Event
from types import FrameType

from fastapi import FastAPI
from uvicorn import Config, Server


class CoreHttpServer(Server):
    def __init__(self, app: FastAPI, host: str, port: int, *, shutdown: Event | None = None):
        super().__init__(Config(
            app, host=host, port=port, workers=1, loop="asyncio", http="h11",
            access_log=False, backlog=8, limit_concurrency=16, timeout_keep_alive=10,
            h11_max_incomplete_event_size=16 * 1024, timeout_graceful_shutdown=3,
        ))
        self.shutdown_requested = shutdown if shutdown is not None else Event()

    def serve_forever(self) -> None:
        self.run()

    def stop(self) -> None:
        self.shutdown_requested.set()
        self.should_exit = True

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self.shutdown_requested.set()
        super().handle_exit(sig, frame)
