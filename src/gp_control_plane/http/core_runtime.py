"""Explicit startup owner for the Bottle/Cheroot Core listener."""

from ..application.api2_search import SearchController
from ..application.core_update import CoreUpdate
from ..application.operation_admission import OperationAdmission
from ..config import AppConfig
from ..engines.api2_blockcheck import BlockcheckEngine
from ..storage import connect_api2
from .api_v2.app import create_api2_app
from .cheroot_connection import WebStreamLifecycle, require_pinned_cheroot
from .http_runtime import WebHttpRuntime


def create_core_runtime(config: AppConfig, host: str, port: int, *, ui_enabled: bool = False) -> WebHttpRuntime:
    """Recover owned processes and migrate before accepting any request."""
    if ui_enabled:
        raise ValueError("Core no longer embeds Web; install GP Plane Web separately")

    from .api_server import _recover_runtime_before_serve

    require_pinned_cheroot()
    _recover_runtime_before_serve(config)
    state_dir = config.output.state_dir
    with connect_api2(state_dir) as conn:
        conn.execute(
            """UPDATE runs SET status='failed',
                      error_json='{"code":"interrupted","message":"Core restarted during search"}'
               WHERE status IN ('starting','running','stopping')"""
        )
    lifecycle = WebStreamLifecycle()
    admission = OperationAdmission()
    search = SearchController(state_dir, admission, BlockcheckEngine())
    updater = CoreUpdate(state_dir, admission)
    app = create_api2_app(state_dir, admission=admission, search=search, updater=updater, stream_lifecycle=lifecycle)
    return WebHttpRuntime(app, host, port, stream_lifecycle=lifecycle)


def serve_core_runtime(config: AppConfig, host: str, port: int, *, ui_enabled: bool = False) -> None:
    runtime = create_core_runtime(config, host, port, ui_enabled=ui_enabled)
    print(f"GP control plane core API listening on http://{host}:{port}")
    runtime.serve_forever()


__all__ = ["create_core_runtime", "serve_core_runtime"]
