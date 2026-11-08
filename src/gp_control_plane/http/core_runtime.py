"""Explicit startup owner for the Bottle/Cheroot Core listener."""

from ..application.api2_search import SearchController
from ..application.core_update import CoreUpdate
from ..application.operation_admission import OperationAdmission
from ..config import AppConfig
from ..engines.api2_blockcheck import BlockcheckEngine
from ..state import read_state
from ..storage import connect_api2
from ..zapret2 import cleanup_owned_nft_table, recover_registered_process_runs
from .api_v2.app import create_api2_app
from .cheroot_connection import WebStreamLifecycle, require_pinned_cheroot
from .http_runtime import WebHttpRuntime


def create_core_runtime(config: AppConfig, host: str, port: int, *, ui_enabled: bool = False) -> WebHttpRuntime:
    """Recover owned processes and migrate before accepting any request."""
    if ui_enabled:
        raise ValueError("Core no longer embeds Web; install GP Plane Web separately")

    from .api_server import _clear_stale_current_run, _recover_runtime_before_serve

    require_pinned_cheroot()
    state_dir = config.output.state_dir
    current = read_state(state_dir)
    table = current.get("current_run_nft_table")
    if table and current.get("current_run_id"):
        if not recover_registered_process_runs():
            raise RuntimeError("Managed search process cleanup could not be verified")
        cleanup_owned_nft_table(table)
        _clear_stale_current_run(config, recovered_quarantine_run_id=current["current_run_id"])
    else:
        _recover_runtime_before_serve(config)
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
