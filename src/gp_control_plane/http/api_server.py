from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .. import __version__, core_api, service_api
from ..application.discovery import (
    MULTI_DOMAIN_DISCOVERY_JOB,
    STANDARD_DISCOVERY_JOB,
    DiscoveryService,
    DiscoverySpec,
)
from ..application import web_views
from ..application.web_view_contracts import (
    INTERNAL_AUTH_VERIFY_BEARER_PATH,
    INTERNAL_WEB_EVENTS_SNAPSHOT_PATH,
    INTERNAL_WEB_EVENTS_STREAM_PATH,
    INTERNAL_WEB_VIEW_GET_PATHS,
    INTERNAL_WEB_VIEW_POST_PATHS,
    external_web_view_path,
)
from ..auth import AuthenticationError, PasswordValidationError, change_password, health_payload, login, require_bearer_token
from ..backups import (
    create_post_run_snapshot,
    create_snapshot_if_idle,
    delete_snapshot_if_idle,
    import_snapshot_archive,
    restore_snapshot_if_idle,
    snapshot_file_path,
)
from ..config import AppConfig
from ..domain_sources import (
    builtin_preset_sources,
    fetch_v2fly_category_local,
    fetch_v2fly_revision,
    import_v2fly_preset,
    list_v2fly_categories_local,
    parse_v2fly_domains,
    parse_v2fly_revision,
    prepare_v2fly_local_storage,
    preview_v2fly_preset,
    read_v2fly_catalog_cache,
    read_v2fly_group_manifest,
    write_v2fly_catalog_cache,
)
from ..engines.blockcheck2 import Blockcheck2Adapter
from ..jobs import JobRunner
from ..releases import release_channel_info
from ..resource_budget import (
    BACKUP_STREAM_CHUNK_BYTES,
    BACKUP_UPLOAD_MAX_BYTES,
    JSON_REQUEST_MAX_BYTES,
)
from ..settings import (
    DEFAULT_SETTINGS,
    read_run_settings,
    read_service_settings,
    read_settings,
    save_run_settings,
    save_settings,
)
from ..state import active_job_lock_payload, has_active_runtime, now_iso, read_state, update_state
from ..storage import (
    delete_custom_preset,
    delete_user_presets,
    read_custom_preset_index,
    read_custom_presets,
    read_preset_domains_page,
    read_system_preset_index,
    read_system_presets,
    save_custom_preset,
    save_custom_presets,
    save_system_preset,
    set_preset_domain_enabled,
    is_storage_unavailable_error as _is_storage_unavailable_error,
    storage_unavailable_diagnostic,
)
from ..strategy_finder import (
    candidate_storage_version,
    close_stale_running_runs,
    domain_sets,
    latest_log_tail,
    read_candidate_domain_index,
    read_candidate_page,
    read_runs,
    run_multi_domain_discovery,
    run_standard_discovery,
)
from ..zapret2 import (
    check_install_cached,
    cleanup_nft_blockcheck_tables,
    recover_quarantined_process_run,
    recover_registered_process_runs,
)
from .errors import error_payload, normalize_error_payload
from .docs import (
    OPENAPI_JSON_CONTENT_TYPE,
    SWAGGER_HTML_CONTENT_TYPE,
    SWAGGER_PATHS,
    openapi_json_bytes,
    swagger_ui_html,
)


MAX_BACKUP_UPLOAD_BYTES = BACKUP_UPLOAD_MAX_BYTES
MAX_JSON_REQUEST_BYTES = JSON_REQUEST_MAX_BYTES
NDJSON_CONTENT_TYPE = "application/x-ndjson; charset=utf-8"
_SSE_HEARTBEAT_SECONDS = 15.0

_core_strategy_discovery_job_payload = core_api.strategy_discovery_job_payload
_LOGGER = logging.getLogger(__name__)
_EVENT_CURSOR_LOCK = threading.Lock()
_EVENT_CURSOR_STATE: dict[str, dict[str, Any]] = {}
_ROOT_MANAGED_DISCOVERY_NAMES = frozenset(
    {"zapret-standard-discovery", "zapret-multi-domain-discovery"}
)


def __getattr__(name: str) -> object:
    """Supply a test-only stdlib listener class without a product dispatch.

    Historical lifecycle-unit tests construct and patch their own synthetic
    listener through this name.  Production facades below never read it: Core
    has only the Bottle/Cheroot runtime.  Keeping the import lazy prevents it
    from becoming part of the Core listener's import/startup path.
    """

    if name == "ThreadingHTTPServer":
        from http.server import ThreadingHTTPServer

        return ThreadingHTTPServer
    raise AttributeError(name)


class RequestBodyTooLarge(ValueError):
    pass


class RuntimeBusyError(RuntimeError):
    pass


def index_html() -> str:
    raise RuntimeError("Core no longer embeds Web; install GP Plane Web separately")


def _clean_install_vault_public_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the HTTP contract limited to non-secret vault metadata."""
    return {
        "vault_id": str(payload.get("vault_id") or ""),
        "created_at": str(payload.get("created_at") or ""),
        "schema_version": str(payload.get("schema_version") or ""),
        "archive_sha256": str(payload.get("archive_sha256") or ""),
        "archive_size_bytes": int(payload.get("archive_size_bytes") or 0),
        "verification": str(payload.get("verification") or ""),
        "pending": bool(payload.get("pending")),
    }


def _clean_install_vault_create_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only public creation metadata from the protected vault handoff."""
    return {
        "vault_id": str(payload.get("vault_id") or ""),
        "archive_sha256": str(payload.get("archive_sha256") or ""),
        "archive_size_bytes": int(payload.get("archive_size_bytes") or 0),
        "schema_version": str(payload.get("schema_version") or ""),
        "semantic_manifest": payload.get("semantic_manifest") or {},
    }


def _clean_install_vault_restore_response(payload: dict[str, Any], vault_id: str) -> dict[str, Any]:
    """Expose only completion flags from the local restore."""
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    cleanup = payload.get("cleanup") if isinstance(payload.get("cleanup"), dict) else {}
    readiness = payload.get("storage_status") if isinstance(payload.get("storage_status"), dict) else {}
    return {
        "completed": bool(payload.get("completed")),
        "vault_id": str(payload.get("vault_id") or vault_id),
        "verification": {"verified": bool(verification.get("verified"))},
        "storage_status": {"ready": bool(readiness.get("ready"))},
        "cleanup": {"source_deleted": bool(cleanup.get("source_deleted"))},
    }


def serve(config: AppConfig, host: str, port: int, *, ui_enabled: bool = False) -> None:
    """Compatibility startup facade for the shared Bottle/Cheroot runtime."""
    from .core_runtime import serve_core_runtime

    return serve_core_runtime(config, host, port, ui_enabled=ui_enabled)


def serve_core(config: AppConfig, host: str, port: int) -> None:
    """Preserved headless Core compatibility entrypoint."""

    return serve(config, host, port, ui_enabled=False)


def serve_web_proxy(config: AppConfig, host: str, port: int, *, core_url: str) -> None:
    """Lazy compatibility facade that keeps Web out of the Core import graph."""

    from .proxy import serve_web_proxy as _serve_web_proxy

    return _serve_web_proxy(config, host, port, core_url=core_url)


def _event_payloads(config: AppConfig) -> dict[str, dict[str, Any]]:
    return web_views.event_payloads(config)


def web_json_get_payload(config: AppConfig, path: str, query: dict[str, list[str]]) -> dict[str, Any]:
    """Compatibility seam for the Core-owned A4 Web view DTOs."""

    return web_views.get_payload(config, path, query)


def _web_event_payloads(config: AppConfig) -> dict[str, dict[str, Any]]:
    return web_views.event_payloads(config)


def _core_event_payloads(config: AppConfig) -> dict[str, dict[str, Any]]:
    state_dir = config.output.state_dir
    status_event = dict(core_api.status_payload(config))
    status_event.pop("updated_at", None)
    run_settings_event = {"version": _event_fingerprint(read_run_settings(config))}
    domain_lists_event = {
        "version": _event_fingerprint(
            {
                "custom": read_custom_preset_index(state_dir),
                "system": read_system_preset_index(state_dir),
            }
        )
    }
    candidates_event = {"version": candidate_storage_version(state_dir)}
    return {
        "core.status": status_event,
        "strategy-discovery.progress": core_api.current_progress_payload(config),
        "strategy-discovery.log": _log_event_payload(state_dir),
        "strategy-candidates": candidates_event,
        "run-settings": run_settings_event,
        "domain-lists": domain_lists_event,
    }


def _runs_event_payload(state_dir: Path) -> dict[str, Any]:
    runs = read_runs(state_dir, limit=20)
    compact = [
        {
            "id": item.get("id"),
            "status": item.get("status"),
            "phase": item.get("phase"),
            "timestamp": item.get("timestamp"),
            "candidate_count": item.get("candidate_count"),
            "common_candidate_count": item.get("common_candidate_count"),
            "progress": item.get("progress"),
        }
        for item in runs
    ]
    return {"count": len(runs), "version": _event_fingerprint(compact)}


def _log_event_payload(state_dir: Path) -> dict[str, Any]:
    for run in reversed(read_runs(state_dir, limit=20)):
        stdout_log = Path(str(run.get("stdout_log") or ""))
        if not stdout_log.is_file():
            continue
        stderr_log_raw = str(run.get("stderr_log") or "")
        stderr_log = Path(stderr_log_raw) if stderr_log_raw else None
        return {
            "run_id": run.get("id"),
            "status": run.get("status"),
            "stdout": _path_version(stdout_log),
            "stderr": _path_version(stderr_log) if stderr_log else {"size": 0, "mtime_ns": 0},
            "progress": _path_version(_optional_path(run.get("progress_log"))),
            "metrics": _path_version(_optional_path(run.get("metrics_log"))),
        }
    return {"run_id": None, "status": None, "stdout": {"size": 0, "mtime_ns": 0}}


def _optional_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    return Path(text) if text else None


def _path_version(path: Path | None) -> dict[str, int]:
    if path is None or not path.exists():
        return {"size": 0, "mtime_ns": 0}
    stat = path.stat()
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _event_fingerprint(payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


def _latest_log_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return latest_log_tail(
        config.output.state_dir,
        run_id=_query_one(query, "run_id") or None,
        stdout_from_size=_query_int(query, "stdout_size", -1),
        stdout_log_match=_query_one(query, "stdout_log"),
        stderr_from_size=_query_int(query, "stderr_size", -1),
        stderr_log_match=_query_one(query, "stderr_log"),
    )


def _current_run_latest_log_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    state = read_state(config.output.state_dir)
    return latest_log_tail(
        config.output.state_dir,
        run_id=str(state.get("current_run_id") or ""),
        stdout_from_size=_query_int(query, "stdout_size", -1),
        stdout_log_match=_query_one(query, "stdout_log"),
        stderr_from_size=_query_int(query, "stderr_size", -1),
        stderr_log_match=_query_one(query, "stderr_log"),
    )


DEFAULT_RUN_PREFERENCES = {
    "domains": [],
    "domain_preset": "system:required",
    "discovery_profile": "standard",
    "run_mode": "standard",
    "curl_parallelism": 4,
    "enable_http": False,
    "enable_tls12": True,
    "enable_tls13": False,
    "include_quic": True,
    "enable_ipv6": False,
    "scan_level": "standard",
    "repeats": 1,
    "repeat_parallel": False,
    "skip_dnscheck": True,
    "skip_ipblock": True,
    "limit_time_enabled": False,
    "timeout_hours": 6,
}


DEFAULT_DISCOVERY_PROFILES = {
    "quick": {
        "name": "quick",
        "title": "Быстрый",
        "enable_http": False,
        "enable_tls12": True,
        "enable_tls13": False,
        "include_quic": True,
        "enable_ipv6": False,
        "scan_level": "quick",
        "repeats": 1,
        "repeat_parallel": False,
        "skip_dnscheck": True,
        "skip_ipblock": True,
        "curl_parallelism": 4,
        "limit_time_enabled": False,
        "timeout_hours": 6,
    },
    "standard": {
        "name": "standard",
        "title": "Стандартный",
        "enable_http": False,
        "enable_tls12": True,
        "enable_tls13": False,
        "include_quic": True,
        "enable_ipv6": False,
        "scan_level": "standard",
        "repeats": 1,
        "repeat_parallel": False,
        "skip_dnscheck": True,
        "skip_ipblock": True,
        "curl_parallelism": 4,
        "limit_time_enabled": False,
        "timeout_hours": 6,
    },
    "force": {
        "name": "force",
        "title": "Глубокий",
        "enable_http": True,
        "enable_tls12": True,
        "enable_tls13": True,
        "include_quic": True,
        "enable_ipv6": False,
        "scan_level": "force",
        "repeats": 1,
        "repeat_parallel": False,
        "skip_dnscheck": False,
        "skip_ipblock": False,
        "curl_parallelism": 4,
        "limit_time_enabled": False,
        "timeout_hours": 6,
    },
}


def read_run_preferences(config: AppConfig) -> dict[str, Any]:
    state = read_state(config.output.state_dir)
    stored = state.get("run_preferences") if isinstance(state.get("run_preferences"), dict) else {}
    return _normalize_run_preferences({**DEFAULT_RUN_PREFERENCES, **stored})


def save_run_preferences(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    preferences = _normalize_run_preferences(
        {**read_run_preferences(config), **(payload if isinstance(payload, dict) else {})}
    )
    update_state(config.output.state_dir, lambda state: state | {"run_preferences": preferences})
    return preferences


def _normalize_run_preferences(raw: dict[str, Any]) -> dict[str, Any]:
    run_mode = str(raw.get("run_mode") or "standard")
    if run_mode not in {"standard", "multi"}:
        run_mode = "standard"
    scan_level = str(raw.get("scan_level") or "standard")
    if scan_level not in {"quick", "standard", "force"}:
        scan_level = "standard"
    discovery_profile = str(raw.get("discovery_profile") or scan_level)
    if discovery_profile not in {"quick", "standard", "force", "custom"}:
        discovery_profile = scan_level if scan_level in {"quick", "standard", "force"} else "custom"
    timeout_hours_raw = raw.get("timeout_hours")
    try:
        timeout_hours = float(timeout_hours_raw)
    except (TypeError, ValueError):
        timeout_hours = 6.0
    timeout_hours = max(0.1, min(24.0, timeout_hours))
    return {
        "domains": _clean_domain_list(raw.get("domains") or []),
        "domain_preset": str(raw.get("domain_preset") or "system:required")[:160],
        "discovery_profile": discovery_profile,
        "run_mode": run_mode,
        "curl_parallelism": _minimum_int(raw.get("curl_parallelism"), default=4, minimum=1),
        "enable_http": bool(raw.get("enable_http")),
        "enable_tls12": bool(raw.get("enable_tls12", True)),
        "enable_tls13": bool(raw.get("enable_tls13")),
        "include_quic": bool(raw.get("include_quic", True)),
        "enable_ipv6": bool(raw.get("enable_ipv6")),
        "scan_level": scan_level,
        "repeats": _bounded_int(raw.get("repeats"), default=1, minimum=1, maximum=10),
        "repeat_parallel": bool(raw.get("repeat_parallel")),
        "skip_dnscheck": bool(raw.get("skip_dnscheck", True)),
        "skip_ipblock": bool(raw.get("skip_ipblock", True)),
        "limit_time_enabled": bool(raw.get("limit_time_enabled")),
        "timeout_hours": timeout_hours,
    }


def _clean_domain_list(value: Any) -> list[str]:
    items = value if isinstance(value, list) else str(value or "").replace(",", "\n").splitlines()
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        domain = str(item or "").strip().lower()
        if not domain or domain in seen:
            continue
        seen.add(domain)
        result.append(domain)
        if len(result) >= 5000:
            break
    return result


def read_discovery_profiles(config: AppConfig) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for name, profile in DEFAULT_DISCOVERY_PROFILES.items():
        merged[name] = _normalize_discovery_profile(name, profile)
    return dict(sorted(merged.items()))


def save_discovery_profiles(config: AppConfig, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    update_state(config.output.state_dir, lambda state: state | {"discovery_profiles": {}})
    return read_discovery_profiles(config)


def _normalize_discovery_profile(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    scan_level = str(raw.get("scan_level") or "standard")
    if scan_level not in {"quick", "standard", "force"}:
        scan_level = "standard"
    return {
        "name": name,
        "title": str(raw.get("title") or name),
        "enable_http": _payload_bool(raw, "enable_http", False),
        "enable_tls12": _payload_bool(raw, "enable_tls12", True),
        "enable_tls13": _payload_bool(raw, "enable_tls13", False),
        "include_quic": _payload_bool(raw, "include_quic", True),
        "enable_ipv6": _payload_bool(raw, "enable_ipv6", False),
        "scan_level": scan_level,
        "repeats": _bounded_int(raw.get("repeats"), default=1, minimum=1, maximum=10),
        "repeat_parallel": _payload_bool(raw, "repeat_parallel", False),
        "skip_dnscheck": _payload_bool(raw, "skip_dnscheck", True),
        "skip_ipblock": _payload_bool(raw, "skip_ipblock", True),
        "curl_parallelism": _minimum_int(raw.get("curl_parallelism"), default=4, minimum=1),
        "limit_time_enabled": _payload_bool(raw, "limit_time_enabled", False),
        "timeout_hours": _bounded_int(raw.get("timeout_hours"), default=6, minimum=1, maximum=24),
    }


def _profile_name(value: Any) -> str:
    name = str(value or "").strip().lower()
    allowed = []
    for char in name:
        if char.isalnum() or char in {"-", "_"}:
            allowed.append(char)
    return "".join(allowed)[:64]


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


def _candidate_page_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return read_candidate_page(
        config.output.state_dir,
        limit=_query_int(query, "limit", 50),
        offset=_query_int(query, "offset", 0),
        query=_query_str(query, "query", ""),
        view=_query_str(query, "view", "domain"),
        domains=_query_domains(query, "domains"),
        domain=_query_str(query, "domain", ""),
        fragmentation_classes=_query_domains(query, "fragmentation_class"),
    )


def _candidate_domain_index_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return read_candidate_domain_index(
        config.output.state_dir,
        limit=_query_int(query, "limit", 50),
        offset=_query_int(query, "offset", 0),
        query=_query_str(query, "query", ""),
        fragmentation_classes=_query_domains(query, "fragmentation_class"),
    )


def _runs_page_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return core_api.runs_history_page_payload(config, query)


def _presets_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "metadata": read_custom_preset_index(config.output.state_dir),
        "system_metadata": read_system_preset_index(config.output.state_dir),
        "system": read_system_presets(config.output.state_dir),
    }
    if _query_bool(query, "include_domains", False):
        payload["custom"] = read_custom_presets(config.output.state_dir)
    else:
        payload["custom"] = {"finder": {}, "common": {}}
    return payload


def _web_presets_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return _presets_payload(config, query) | {
        "domain_sets": domain_sets(),
        "builtin": builtin_preset_sources(),
    }


def _release_info_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    settings = read_service_settings(config)
    channel = _query_str(query, "channel", str(settings.get("update_channel") or "stable"))
    stable = release_channel_info(current_version=__version__, channel="stable")
    prerelease = release_channel_info(current_version=__version__, channel="prerelease")
    selected = prerelease if channel == "prerelease" else stable
    return {"release": selected, "releases": {"stable": stable, "prerelease": prerelease}}


def _preset_domains_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return read_preset_domains_page(
        config.output.state_dir,
        scope=_query_str(query, "scope", ""),
        name=_query_str(query, "name", ""),
        kind=_query_str(query, "kind", "user"),
        query=_query_str(query, "query", ""),
        limit=_query_int(query, "limit", 200),
        offset=_query_int(query, "offset", 0),
        include_disabled=_query_bool(query, "include_disabled", True),
    )


def _v2fly_categories_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return list_v2fly_categories_local(
        config.output.state_dir,
        query=_query_str(query, "query", ""),
        limit=_query_int(query, "limit", 2000),
    )


def _v2fly_preview_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    state_dir = config.output.state_dir
    return preview_v2fly_preset(
        state_dir,
        scope=str(payload.get("scope") or "finder"),
        name=str(payload.get("name") or ""),
        categories=_payload_string_list(payload, "categories"),
        domains=_payload_string_list(payload, "domains"),
        fetcher=lambda category: fetch_v2fly_category_local(state_dir, category),
    )


def _v2fly_import_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    state_dir = config.output.state_dir
    return import_v2fly_preset(
        state_dir,
        scope=str(payload.get("scope") or "finder"),
        name=str(payload.get("name") or ""),
        categories=_payload_string_list(payload, "categories"),
        domains=_payload_string_list(payload, "domains"),
        fetcher=lambda category: fetch_v2fly_category_local(state_dir, category),
    )


def _events_response_payload(config: AppConfig, query: dict[str, list[str]], *, stream: str) -> dict[str, Any]:
    if stream == "web":
        return web_views.events_snapshot_payload(config, query)
    payloads = _core_event_payloads(config)
    events = []
    created_at = now_iso()
    after_id = _query_one(query, "after_id")
    after_sequence = _event_sequence(stream, after_id)
    limit = _bounded_int(_query_str(query, "limit", "100"), default=100, minimum=1, maximum=500)
    for event_type, payload in payloads.items():
        event_id = _event_cursor(stream, event_type, payload)
        if _event_sequence(stream, event_id) <= after_sequence:
            continue
        events.append({"event_id": event_id, "type": event_type, "created_at": created_at, "payload": payload})
        if len(events) >= limit:
            break
    return {"events": events, "next_after_id": str(events[-1]["event_id"]) if events else after_id}


def _event_cursor(stream: str, event_type: str, payload: dict[str, Any]) -> str:
    fingerprint = _event_fingerprint(payload)
    with _EVENT_CURSOR_LOCK:
        stream_state = _EVENT_CURSOR_STATE.setdefault(stream, {"next": 0, "events": {}})
        event_state = stream_state["events"].get(event_type)
        if event_state and event_state.get("fingerprint") == fingerprint:
            return str(event_state["event_id"])
        stream_state["next"] = int(stream_state.get("next") or 0) + 1
        event_id = f"{stream}:{stream_state['next']:012d}"
        stream_state["events"][event_type] = {"fingerprint": fingerprint, "event_id": event_id}
        return event_id


def _event_sequence(stream: str, event_id: str) -> int:
    prefix = f"{stream}:"
    if not event_id.startswith(prefix):
        return 0
    raw_sequence = event_id[len(prefix) :]
    try:
        return int(raw_sequence)
    except ValueError:
        return 0


def _query_str(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key) or []
    return values[0] if values else default


def _query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = _query_str(query, key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _query_bool(query: dict[str, list[str]], key: str, default: bool) -> bool:
    raw = _query_str(query, key, "1" if default else "0").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _query_domains(query: dict[str, list[str]], key: str) -> list[str]:
    values = query.get(key) or []
    domains: list[str] = []
    for value in values:
        domains.extend(item.strip() for item in value.split(",") if item.strip())
    return domains


def _query_one(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[0]).strip() if values else ""


def _payload_string_list(payload: dict[str, Any], key: str) -> list[str]:
    raw = payload.get(key) or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _multipart_file_bytes(body: bytes, boundary: str) -> bytes:
    delimiter = ("--" + boundary).encode("utf-8")
    for part in body.split(delimiter):
        if b"Content-Disposition:" not in part or b"filename=" not in part:
            continue
        header, sep, payload = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        payload = payload.rstrip(b"\r\n")
        if payload.endswith(b"--"):
            payload = payload[:-2].rstrip(b"\r\n")
        if payload:
            return payload
    raise ValueError("backup file is missing")


def _job_zapret_standard_discovery(
    config: AppConfig, payload: dict[str, Any], stop_event: Any, run_id: str = ""
) -> dict[str, Any]:
    return Blockcheck2Adapter(
        config,
        standard_discovery=run_standard_discovery,
        multi_domain_discovery=run_multi_domain_discovery,
    ).execute_standard(
        DiscoverySpec(name=STANDARD_DISCOVERY_JOB, payload=payload),
        stop_event,
        run_id,
    )


def _job_zapret_multi_domain_discovery(
    config: AppConfig, payload: dict[str, Any], stop_event: Any, run_id: str = ""
) -> dict[str, Any]:
    return Blockcheck2Adapter(
        config,
        standard_discovery=run_standard_discovery,
        multi_domain_discovery=run_multi_domain_discovery,
    ).execute_multi_domain(
        DiscoverySpec(name=MULTI_DOMAIN_DISCOVERY_JOB, payload=payload),
        stop_event,
        run_id,
    )

def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _minimum_int(value: Any, default: int, minimum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, number)


def _payload_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)
