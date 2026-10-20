from __future__ import annotations

from typing import Any

from .config import AppConfig
from .resource_budget import RASPBERRY_PI2_CURL_PARALLELISM_SAFE_MAX
from .state import now_iso, read_state, update_state
from .storage import read_app_setting, save_app_setting


RUN_SETTINGS_KEY = "run_settings"
SERVICE_SETTINGS_KEY = "service_settings"

DEFAULT_RUN_SETTINGS = {
    "curl_parallelism_default": 4,
    "curl_parallelism_max": RASPBERRY_PI2_CURL_PARALLELISM_SAFE_MAX,
    "curl_max_time": 2,
    "curl_max_time_quic": 2,
    "curl_max_time_doh": 2,
    "enable_ipv6": False,
    "debug_stdout": False,
}

DEFAULT_SERVICE_SETTINGS = {
    "update_channel": "stable",
    "stable_release_url": "https://github.com/balbomush/GP-plane/releases/latest",
    "prerelease_url": "https://github.com/balbomush/GP-plane/releases",
}

DEFAULT_SETTINGS = {**DEFAULT_RUN_SETTINGS, **DEFAULT_SERVICE_SETTINGS}
RUN_SETTING_KEYS = frozenset(DEFAULT_RUN_SETTINGS)
SERVICE_SETTING_KEYS = frozenset(DEFAULT_SERVICE_SETTINGS)


def read_run_settings(config: AppConfig) -> dict[str, Any]:
    stored = read_app_setting(config.output.state_dir, RUN_SETTINGS_KEY)
    if isinstance(stored, dict):
        _migrate_service_settings(config, stored)
        settings = normalize_run_settings({**DEFAULT_RUN_SETTINGS, **stored})
        if any(key in stored for key in SERVICE_SETTING_KEYS):
            save_app_setting(config.output.state_dir, RUN_SETTINGS_KEY, settings, now_iso())
        return settings
    legacy = _legacy_state_settings(config)
    settings = normalize_run_settings({**DEFAULT_RUN_SETTINGS, **legacy})
    if legacy:
        _migrate_service_settings(config, legacy)
        save_app_setting(config.output.state_dir, RUN_SETTINGS_KEY, settings, now_iso())
    return settings


def save_run_settings(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    selected = _select_keys(payload, RUN_SETTING_KEYS)
    settings = normalize_run_settings({**read_run_settings(config), **selected})
    save_app_setting(config.output.state_dir, RUN_SETTINGS_KEY, settings, now_iso())
    sync_legacy_settings(config)
    return settings


def read_service_settings(config: AppConfig) -> dict[str, Any]:
    stored = read_app_setting(config.output.state_dir, SERVICE_SETTINGS_KEY)
    if isinstance(stored, dict):
        return normalize_service_settings({**DEFAULT_SERVICE_SETTINGS, **stored})
    legacy_source = read_app_setting(config.output.state_dir, RUN_SETTINGS_KEY)
    if not isinstance(legacy_source, dict):
        legacy_source = _legacy_state_settings(config)
    settings = normalize_service_settings({**DEFAULT_SERVICE_SETTINGS, **(legacy_source if isinstance(legacy_source, dict) else {})})
    if isinstance(legacy_source, dict) and any(key in legacy_source for key in SERVICE_SETTING_KEYS):
        save_app_setting(config.output.state_dir, SERVICE_SETTINGS_KEY, settings, now_iso())
    return settings


def save_service_settings(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    selected = _select_keys(payload, SERVICE_SETTING_KEYS)
    settings = normalize_service_settings({**read_service_settings(config), **selected})
    save_app_setting(config.output.state_dir, SERVICE_SETTINGS_KEY, settings, now_iso())
    sync_legacy_settings(config)
    return settings


def read_settings(config: AppConfig) -> dict[str, Any]:
    return {**read_run_settings(config), **read_service_settings(config)}


def save_settings(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    save_run_settings(config, payload)
    save_service_settings(config, payload)
    return sync_legacy_settings(config)


def sync_legacy_settings(config: AppConfig) -> dict[str, Any]:
    settings = {**read_run_settings(config), **read_service_settings(config)}
    update_state(config.output.state_dir, lambda state: state | {"settings": settings})
    return settings


def normalize_run_settings(raw: dict[str, Any]) -> dict[str, Any]:
    max_parallelism = _minimum_int(raw.get("curl_parallelism_max"), default=10, minimum=1)
    default_parallelism = _bounded_int(raw.get("curl_parallelism_default"), default=4, minimum=1, maximum=max_parallelism)
    return {
        "curl_parallelism_default": default_parallelism,
        "curl_parallelism_max": max_parallelism,
        "curl_max_time": _minimum_int(raw.get("curl_max_time"), default=2, minimum=1),
        "curl_max_time_quic": _minimum_int(raw.get("curl_max_time_quic"), default=2, minimum=1),
        "curl_max_time_doh": _minimum_int(raw.get("curl_max_time_doh"), default=2, minimum=1),
        "enable_ipv6": bool(raw.get("enable_ipv6")),
        "debug_stdout": bool(raw.get("debug_stdout")),
    }


def normalize_service_settings(raw: dict[str, Any]) -> dict[str, Any]:
    channel = str(raw.get("update_channel") or "stable")
    if channel not in {"stable", "prerelease"}:
        channel = "stable"
    return {
        "update_channel": channel,
        "stable_release_url": DEFAULT_SERVICE_SETTINGS["stable_release_url"],
        "prerelease_url": DEFAULT_SERVICE_SETTINGS["prerelease_url"],
    }


def _legacy_state_settings(config: AppConfig) -> dict[str, Any]:
    state = read_state(config.output.state_dir)
    legacy = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    return dict(legacy)


def _migrate_service_settings(config: AppConfig, source: dict[str, Any]) -> None:
    if read_app_setting(config.output.state_dir, SERVICE_SETTINGS_KEY) is not None:
        return
    if any(key in source for key in SERVICE_SETTING_KEYS):
        save_app_setting(config.output.state_dir, SERVICE_SETTINGS_KEY, normalize_service_settings(source), now_iso())


def _select_keys(payload: dict[str, Any], keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if str(key) in keys}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _minimum_int(value: Any, default: int, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)
