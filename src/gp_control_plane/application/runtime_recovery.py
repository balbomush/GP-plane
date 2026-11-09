"""Recovery of Core-owned processes and stale markers before serving."""

from typing import Any

from ..config import AppConfig
from ..state import active_job_lock_payload, read_state, update_state
from ..zapret2 import recover_quarantined_process_run, recover_registered_process_runs

_ROOT_MANAGED_DISCOVERY_NAMES = frozenset({"zapret-standard-discovery", "zapret-multi-domain-discovery"})


def _recover_runtime_before_serve(config: AppConfig) -> None:
    state = read_state(config.output.state_dir)
    if str(state.get("current_run_status") or "") == "quarantined":
        run_id = str(state.get("current_run_id") or "").strip()
        if not run_id:
            raise RuntimeError("quarantined runtime has no run id")
        recover_quarantined_process_run(run_id)
        _clear_stale_current_run(config, recovered_quarantine_run_id=run_id)
        return
    recovered = recover_registered_process_runs()
    if _requires_verified_root_recovery(state) and not recovered:
        raise RuntimeError("managed runtime recovery could not be verified")
    _clear_stale_current_run(config)


def _requires_verified_root_recovery(state: dict[str, Any]) -> bool:
    return (
        bool(str(state.get("current_run_id") or "").strip())
        and str(state.get("current_run_name") or "") in _ROOT_MANAGED_DISCOVERY_NAMES
        and str(state.get("current_run_status") or "") in {"queued", "running", "stopping"}
    )


def _clear_stale_current_run(config: AppConfig, *, recovered_quarantine_run_id: str = "") -> None:
    state = read_state(config.output.state_dir)
    if not state.get("current_run_id"):
        return
    if str(state.get("current_run_status") or "") == "quarantined":
        if not recovered_quarantine_run_id or recovered_quarantine_run_id != str(state.get("current_run_id") or ""):
            return
    if active_job_lock_payload(config.output.state_dir, cleanup_stale=True):
        return

    def clear_current_run(current: dict[str, Any]) -> dict[str, Any]:
        current["current_run_id"] = None
        current["current_run_name"] = None
        current["current_run_status"] = None
        return current

    update_state(config.output.state_dir, clear_current_run)
