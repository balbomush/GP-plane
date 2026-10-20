"""Explicit startup owner for the Bottle/Cheroot Core listener."""

from __future__ import annotations

from ..application.discovery import DiscoveryService
from ..config import AppConfig
from ..strategy_finder import close_stale_running_runs
from .cheroot_connection import require_pinned_cheroot
from .cheroot_connection import WebStreamLifecycle
from .core_app import CoreServices, create_core_app
from .http_runtime import WebHttpRuntime


def create_core_runtime(config: AppConfig, host: str, port: int, *, ui_enabled: bool = False) -> WebHttpRuntime:
    """Recover once and assemble the one Core listener without starting it."""

    if ui_enabled:
        raise ValueError("Core no longer embeds Web; install GP Plane Web separately")

    # Import lazily to retain api_server's compatibility payload helpers while
    # removing it from the production request/listener path.
    from . import api_server

    require_pinned_cheroot()
    api_server._recover_runtime_before_serve(config)
    close_stale_running_runs(config.output.state_dir)
    # Keep the public compatibility patch seam on api_server while runner
    # ownership remains here.  This is a callback lookup, not duplicated
    # backup behavior.
    runner = api_server.JobRunner(
        config.output.state_dir,
        on_idle=lambda: api_server.create_post_run_snapshot(config.output.state_dir),
    )
    discovery = DiscoveryService(
        config,
        runner,
        execute_standard=lambda spec, stop_event, run_id: api_server._job_zapret_standard_discovery(
            config, dict(spec.payload), stop_event, run_id
        ),
        execute_multi_domain=lambda spec, stop_event, run_id: api_server._job_zapret_multi_domain_discovery(
            config, dict(spec.payload), stop_event, run_id
        ),
        # Preserve the established compatibility patch seam for the A3
        # cancellation hook while Core runtime remains its only owner.
        cancel_hook=api_server.cleanup_nft_blockcheck_tables,
    )
    stream_lifecycle = WebStreamLifecycle()
    services = CoreServices(
        config=config,
        discovery=discovery,
        runtime_role="monolith" if ui_enabled else "core",
        web_install_enabled=True if ui_enabled else None,
        ui_enabled=ui_enabled,
        # Retain existing public test/config seam while the bounded parser is
        # shared with Web; normal values are the resource-budget constants.
        json_max_bytes=api_server.MAX_JSON_REQUEST_BYTES,
        backup_max_bytes=api_server.MAX_BACKUP_UPLOAD_BYTES,
        stream_lifecycle=stream_lifecycle,
    )
    # Runtime injects the retained Core auth facade; auth implementation stays
    # Core-owned and factory callers can inject a controlled adapter.
    return WebHttpRuntime(
        create_core_app(
            services,
            auth_adapter=lambda state_dir, authorization: api_server.require_bearer_token(state_dir, authorization),
        ),
        host,
        port,
        stream_lifecycle=stream_lifecycle,
    )


def serve_core_runtime(config: AppConfig, host: str, port: int, *, ui_enabled: bool = False) -> None:
    """Preserved blocking startup facade; runtime owns listener lifecycle."""

    runtime = create_core_runtime(config, host, port, ui_enabled=ui_enabled)
    mode = "web UI" if ui_enabled else "core API"
    print(f"GP control plane {mode} listening on http://{host}:{port}")
    runtime.serve_forever()


__all__ = ["create_core_runtime", "serve_core_runtime"]
