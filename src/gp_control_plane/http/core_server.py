from __future__ import annotations

from ..config import AppConfig


def serve_core(config: AppConfig, host: str = "127.0.0.1", port: int = 8081) -> None:
    from .core_runtime import serve_core_runtime

    serve_core_runtime(config, host=host, port=port, ui_enabled=False)
