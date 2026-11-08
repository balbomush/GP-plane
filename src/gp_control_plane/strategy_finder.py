from __future__ import annotations

from . import discovery_progress as _progress_calculation, candidate_queries as _candidate_queries
from .discovery_constants import CRITICAL_DOMAINS, DIAGNOSTIC_DOMAINS, COVERAGE_DOMAINS, GOOGLE_YOUTUBE_DOMAINS, DISCORD_DOMAINS, CLOUDFLARE_DOMAINS, AMAZON_AWS_DOMAINS, ATTEMPT_TIMEOUT_ESTIMATE_MS, ETA_SAMPLE_MIN_ATTEMPTS, ETA_SAMPLE_MAX_POINTS, ETA_SAMPLE_WINSORIZE_MIN_INTERVALS, ETA_SAMPLE_WINSORIZE_RATIO, ETA_RECALC_SMALL_STEP, ETA_RECALC_LARGE_STEP, ETA_RECALC_LARGE_AFTER, LIVE_CANDIDATE_FLUSH_SIZE, LIVE_CANDIDATE_QUEUE_MAX_BATCHES, LIVE_CANDIDATE_SAMPLE_LIMIT, METRICS_INTERVAL_SECONDS, METRICS_MAX_BYTES, STDOUT_LOG_MAX_BYTES, DEBUG_STDOUT_LOG_MAX_BYTES, LOG_RETENTION_MAX_FILES, LOG_RETENTION_MAX_TOTAL_BYTES, LOG_RETENTION_SUFFIXES, PHASE_CHECK_VPN, PHASE_CHECK_ZAPRET, PHASE_CHECK_DOMAIN, PHASE_DISCOVERY, PHASE_SUMMARY, PHASE_SAVING, PHASE_COMPLETE, PHASE_LABELS, _ATTEMPT_RE, _SCRIPT_RE, _HOSTNAME_RE, _DOMAIN_LIST_PREFIXES, _SERVICE_DOMAIN_SUFFIXES, _CURL_FAILURE_INFO, DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, CORE_CANDIDATE_JSON_MAX_RESULTS, CANDIDATE_RELATION_BATCH_SIZE, NFQUEUE_MAXLEN_MISSING_RE
from .discovery_values import _truthy
from .discovery_values import _bounded_int
from .discovery_values import _minimum_int
from .discovery_domains import domain_sets
from .discovery_domains import classify_domain_input
from .discovery_domains import validate_domain_inputs
from .discovery_domains import curl_failure_info
from .discovery_domains import _domain_classification
from .discovery_domains import _is_service_domain
from .discovery_domains import _clean_domains
from .discovery_domains import _clean_domain_list
from .discovery_parsing import classify_stderr_diagnostics
from .discovery_parsing import parse_blockcheck_stdout
from .discovery_parsing import _summary_sections
from .discovery_parsing import _summary_lines
from .discovery_parsing import _dedupe_candidate_lines
from .discovery_parsing import _candidate_lines
from .discovery_parsing import _candidate_from_result_line
from .discovery_parsing import _parse_result_line
from .discovery_parsing import _live_success_lines
from .discovery_parsing import _candidate_from_live_success_line
from .discovery_parsing import _live_available_lines
from .discovery_parsing import _diagnostic_counts_from_stdout
from .discovery_parsing import _increment_nested
from .discovery_parsing import _curl_summary
from .discovery_parsing import _live_attempt_line
from .discovery_parsing import _curl_code_from_line
from .discovery_parsing import _is_strategy_failure
from .discovery_parsing import _domain_status_info
from .discovery_parsing import _domain_diagnostics_from_counts
from .discovery_parsing import _dominant_failure_from_counts
from .discovery_parsing import _dominant_status
from .discovery_parsing import _protocol_from_test
from .discovery_progress import _strategy_progress_from_attempts
from .discovery_progress import _attempts_by_script
from .discovery_progress import _average_attempt_ms
from .discovery_progress import _winsorized
from .discovery_progress import _phase_label
from .discovery_progress import _phase_from_line
from .discovery_progress import _script_name_from_line
from .discovery_progress import _eta_parallelism_for_run
from .discovery_progress import _eta_ms_per_attempt_for_run
from .discovery_progress import _eta_recalculation_step
from .discovery_progress import _eta_recalculation_attempts
from .discovery_progress import _elapsed_average_ms_per_attempt
from .discovery_progress import _eta_from_remaining_attempts
from .candidate_queries import _read_candidate_page_sql
from .candidate_queries import _candidate_core_filters
from .candidate_queries import _candidate_filter_payload
from .candidate_queries import _filtered_candidate_total
from .candidate_queries import _iter_filtered_candidate_rows
from .candidate_queries import _filtered_candidate_where
from .candidate_queries import _placeholders
from .candidate_queries import _unique_nonempty_strings
from .candidate_queries import _read_candidate_domain_index_sql
from .candidate_queries import _strategy_query_clause
from .candidate_queries import _clean_fragmentation_classes
from .candidate_queries import _fragmentation_query_clause
from .candidate_queries import _iter_db_candidates
from .candidate_queries import _candidates_from_db_rows
from .candidate_queries import _iter_candidates_from_db_rows
from .candidate_queries import _candidate_domain_maps
from .candidate_queries import _candidate_from_db
from .candidate_queries import _tested_domains_from_db
from .candidate_queries import _candidate_domains
from .candidate_queries import _candidate_common_domains
from .candidate_queries import _compact_candidate


import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .state import active_runtime_payload, append_jsonl, now_iso
from .jobs import ManagedRuntimeQuarantinedError
from .strategy_safety import analyze_strategy
from .storage import (
    append_run,
    connect,
    count_latest_run_payloads,
    read_latest_run_payloads,
    upsert_candidate_event,
    upsert_candidate_event_conn,
)
from .zapret2 import (
    BLOCKCHECK_ENV_KEYS,
    _cleanup_nft_blockcheck_tables,
    _stop_process_group,
    acknowledge_registered_process_run_terminal,
    root_command,
    signal_registered_process_run,
)



_CANDIDATE_WRITER_STOP = object()
_ATTEMPT_PLAN_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


@dataclass(frozen=True)
class DiscoveryOptions:
    enable_http: bool = False
    enable_tls12: bool = True
    enable_tls13: bool = False
    enable_quic: bool = True
    enable_ipv6: bool = False
    scan_level: str = "standard"
    repeats: int = 1
    repeat_parallel: bool = False
    skip_dnscheck: bool = True
    skip_ipblock: bool = True
    curl_max_time: int = 2
    curl_max_time_quic: int = 2
    curl_max_time_doh: int = 2

    def normalized(self) -> "DiscoveryOptions":
        scan_level = self.scan_level if self.scan_level in {"quick", "standard", "force"} else "standard"
        repeats = _bounded_int(self.repeats, default=1, minimum=1, maximum=10)
        curl_max_time = _minimum_int(self.curl_max_time, default=2, minimum=1)
        curl_max_time_quic = _minimum_int(self.curl_max_time_quic, default=2, minimum=1)
        curl_max_time_doh = _minimum_int(self.curl_max_time_doh, default=2, minimum=1)
        if not any([self.enable_http, self.enable_tls12, self.enable_tls13, self.enable_quic]):
            raise ValueError("at least one protocol check must be enabled")
        return DiscoveryOptions(
            enable_http=bool(self.enable_http),
            enable_tls12=bool(self.enable_tls12),
            enable_tls13=bool(self.enable_tls13),
            enable_quic=bool(self.enable_quic),
            enable_ipv6=bool(self.enable_ipv6),
            scan_level=scan_level,
            repeats=repeats,
            repeat_parallel=bool(self.repeat_parallel),
            skip_dnscheck=bool(self.skip_dnscheck),
            skip_ipblock=bool(self.skip_ipblock),
            curl_max_time=curl_max_time,
            curl_max_time_quic=curl_max_time_quic,
            curl_max_time_doh=curl_max_time_doh,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enable_http": self.enable_http,
            "enable_tls12": self.enable_tls12,
            "enable_tls13": self.enable_tls13,
            "enable_quic": self.enable_quic,
            "enable_ipv6": self.enable_ipv6,
            "scan_level": self.scan_level,
            "repeats": self.repeats,
            "repeat_parallel": self.repeat_parallel,
            "skip_dnscheck": self.skip_dnscheck,
            "skip_ipblock": self.skip_ipblock,
            "curl_max_time": self.curl_max_time,
            "curl_max_time_quic": self.curl_max_time_quic,
            "curl_max_time_doh": self.curl_max_time_doh,
        }

    def to_blockcheck_env(self, *, validated: bool = False) -> dict[str, str]:
        options = self if validated else self.normalized()
        return {
            "SKIP_DNSCHECK": "1" if options.skip_dnscheck else "0",
            "SKIP_IPBLOCK": "1" if options.skip_ipblock else "0",
            "ENABLE_HTTP": "1" if options.enable_http else "0",
            "ENABLE_HTTPS_TLS12": "1" if options.enable_tls12 else "0",
            "ENABLE_HTTPS_TLS13": "1" if options.enable_tls13 else "0",
            "ENABLE_HTTP3": "1" if options.enable_quic else "0",
            "SCANLEVEL": options.scan_level,
            "REPEATS": str(options.repeats),
            "PARALLEL": "1" if options.repeat_parallel else "0",
            "CURL_MAX_TIME": str(options.curl_max_time),
            "CURL_MAX_TIME_QUIC": str(options.curl_max_time_quic),
            "CURL_MAX_TIME_DOH": str(options.curl_max_time_doh),
        }

    def to_run_fields(self) -> dict[str, Any]:
        options = self.normalized()
        mapping = options.to_mapping()
        return {
            **mapping,
            "enable_tls": options.enable_tls12,
            "discovery_options": mapping,
        }




















def _domain_validation_run_fields(validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "domain_input_count": int(validation.get("input_count") or 0),
        "domain_valid_count": int(validation.get("valid_count") or 0),
        "domain_skipped_count": int(validation.get("skipped_count") or 0),
        "domain_skipped": list(validation.get("domain_skipped") or [])[:50],
        "domain_classification": list(validation.get("domain_classification") or [])[:100],
        "domain_validation_summary": dict(validation.get("summary") or {}),
    }


def allocate_discovery_run_id() -> str:
    return f"{now_iso().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"


def _discovery_run_id(run_id: str | None) -> str:
    value = str(run_id or "").strip()
    return value or allocate_discovery_run_id()

def _ipvs_value(options: DiscoveryOptions) -> str:
    return "4 6" if options.enable_ipv6 else "4"


def run_standard_discovery(
    domains: list[str],
    state_dir: Path,
    timeout_seconds: int,
    include_quic: bool = True,
    enable_http: bool = False,
    enable_tls12: bool = True,
    enable_tls13: bool = False,
    enable_ipv6: bool = False,
    scan_level: str = "standard",
    repeats: int = 1,
    repeat_parallel: bool = False,
    skip_dnscheck: bool = True,
    skip_ipblock: bool = True,
    curl_max_time: int = 2,
    curl_max_time_quic: int = 2,
    curl_max_time_doh: int = 2,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    options = DiscoveryOptions(
        enable_http=enable_http,
        enable_tls12=enable_tls12,
        enable_tls13=enable_tls13,
        enable_quic=include_quic,
        enable_ipv6=enable_ipv6,
        scan_level=scan_level,
        repeats=repeats,
        repeat_parallel=repeat_parallel,
        skip_dnscheck=skip_dnscheck,
        skip_ipblock=skip_ipblock,
        curl_max_time=curl_max_time,
        curl_max_time_quic=curl_max_time_quic,
        curl_max_time_doh=curl_max_time_doh,
    ).normalized()
    domain_validation = validate_domain_inputs(domains, default_to_critical=True)
    if not domain_validation["domains"]:
        raise ValueError("no valid domains to check")
    return _run_blockcheck_live(
        state_dir=state_dir,
        kind="standard-discovery",
        domains=list(domain_validation["domains"]),
        timeout_seconds=timeout_seconds,
        test="standard",
        options=options,
        domain_validation=domain_validation,
        debug_stdout=debug_stdout,
        stop_event=stop_event,
        run_id=run_id,
    )


def run_multi_domain_discovery(
    domains: list[str],
    state_dir: Path,
    timeout_seconds: int,
    include_quic: bool = True,
    enable_http: bool = False,
    enable_tls12: bool = True,
    enable_tls13: bool = False,
    enable_ipv6: bool = False,
    scan_level: str = "standard",
    repeats: int = 1,
    repeat_parallel: bool = False,
    skip_dnscheck: bool = True,
    skip_ipblock: bool = True,
    curl_max_time: int = 2,
    curl_max_time_quic: int = 2,
    curl_max_time_doh: int = 2,
    curl_parallelism: int = 4,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    options = DiscoveryOptions(
        enable_http=enable_http,
        enable_tls12=enable_tls12,
        enable_tls13=enable_tls13,
        enable_quic=include_quic,
        enable_ipv6=enable_ipv6,
        scan_level=scan_level,
        repeats=repeats,
        repeat_parallel=repeat_parallel,
        skip_dnscheck=skip_dnscheck,
        skip_ipblock=skip_ipblock,
        curl_max_time=curl_max_time,
        curl_max_time_quic=curl_max_time_quic,
        curl_max_time_doh=curl_max_time_doh,
    ).normalized()
    domain_validation = validate_domain_inputs(domains, default_to_critical=True)
    if not domain_validation["domains"]:
        raise ValueError("no valid domains to check")
    return _run_multidomain_blockcheck_live(
        state_dir=state_dir,
        domains=list(domain_validation["domains"]),
        timeout_seconds=timeout_seconds,
        options=options,
        curl_parallelism=curl_parallelism,
        domain_validation=domain_validation,
        debug_stdout=debug_stdout,
        stop_event=stop_event,
        run_id=run_id,
    )


def read_candidates(state_dir: Path) -> list[dict[str, Any]]:
    return _candidate_queries.read_candidates(state_dir, _connect=connect)



def read_strategy_candidates_filtered(
    state_dir: Path,
    *,
    domains: list[str] | None = None,
    strategy_ids: list[str] | None = None,
    protocols: list[str] | None = None,
    source_modes: list[str] | None = None,
    families: list[str] | None = None,
    query: str = "",
    max_results: int = CORE_CANDIDATE_JSON_MAX_RESULTS,
) -> dict[str, Any]:
    return _candidate_queries.read_strategy_candidates_filtered(state_dir, domains=domains, strategy_ids=strategy_ids, protocols=protocols, source_modes=source_modes, families=families, query=query, max_results=max_results, _connect=connect)



def iter_strategy_candidates_filtered(
    state_dir: Path,
    *,
    domains: list[str] | None = None,
    strategy_ids: list[str] | None = None,
    protocols: list[str] | None = None,
    source_modes: list[str] | None = None,
    families: list[str] | None = None,
    query: str = "",
) -> Iterator[dict[str, Any]]:
    return _candidate_queries.iter_strategy_candidates_filtered(state_dir, domains=domains, strategy_ids=strategy_ids, protocols=protocols, source_modes=source_modes, families=families, query=query, _connect=connect)



def read_candidate_page(
    state_dir: Path,
    *,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    query: str = "",
    view: str = "domain",
    domains: list[str] | None = None,
    domain: str = "",
    fragmentation_classes: list[str] | None = None,
) -> dict[str, Any]:
    return _candidate_queries.read_candidate_page(state_dir, limit=limit, offset=offset, query=query, view=view, domains=domains, domain=domain, fragmentation_classes=fragmentation_classes, _connect=connect)



def candidate_storage_version(state_dir: Path) -> dict[str, int]:
    return _candidate_queries.candidate_storage_version(state_dir, _connect=connect)



def read_candidate_domain_index(
    state_dir: Path,
    *,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    query: str = "",
    fragmentation_classes: list[str] | None = None,
) -> dict[str, Any]:
    return _candidate_queries.read_candidate_domain_index(state_dir, limit=limit, offset=offset, query=query, fragmentation_classes=fragmentation_classes, _connect=connect)







































def read_runs(state_dir: Path, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    return [_compact_run(run) for run in read_latest_run_payloads(state_dir, limit=limit, offset=offset)]


def read_runs_page(state_dir: Path, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    limit = _bounded_int(limit, default=50, minimum=1, maximum=1000)
    offset = max(0, _bounded_int(offset, default=0, minimum=0, maximum=10_000_000))
    runs = [_compact_run(run) for run in read_latest_run_payloads(state_dir, limit=limit, offset=offset)]
    total = count_latest_run_payloads(state_dir)
    return {
        "runs": runs,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(runs) < total,
    }


def _compact_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in run.items()
        if key
        not in {
            "summary",
            "common",
            "live_summary",
            "results",
            "common_results",
            "direct_available",
            "not_working",
        }
    }


def close_stale_running_runs(state_dir: Path) -> int:
    root = _finder_dir(state_dir)
    runs = read_runs(state_dir, limit=200)
    latest_by_id: dict[str, dict[str, Any]] = {}
    for run in runs:
        run_id = str(run.get("id") or "")
        if run_id:
            latest_by_id[run_id] = run
    closed = 0
    for run in latest_by_id.values():
        if str(run.get("status") or "") not in {"queued", "running", "stopping"}:
            continue
        progress = run.get("progress")
        if not isinstance(progress, dict):
            progress = _read_progress_log(run)
        update = {
            "id": run.get("id"),
            "kind": run.get("kind"),
            "candidate_id": run.get("candidate_id", ""),
            "status": "stopped",
            "timestamp": run.get("timestamp") or now_iso(),
            "started_at": run.get("started_at") or run.get("timestamp") or "",
            "completed_at": now_iso(),
            "domains": run.get("domains") or [],
            "returncode": run.get("returncode"),
            "stdout_log": run.get("stdout_log", ""),
            "stderr_log": run.get("stderr_log", ""),
            "progress_log": run.get("progress_log", ""),
            "metrics_log": run.get("metrics_log", ""),
            "summary_fallback_log": run.get("summary_fallback_log", ""),
            "candidate_count": int(run.get("candidate_count") or 0),
            "common_candidate_count": int(run.get("common_candidate_count") or 0),
            "total_candidates": int(run.get("total_candidates") or 0),
            "phase": run.get("phase") or (progress.get("phase") if isinstance(progress, dict) else ""),
            "stopped": True,
            "interrupted": True,
            "interrupted_reason": "web service stopped while run was marked active",
            "test": run.get("test", "standard"),
            "attempt_plan": run.get("attempt_plan") or {},
        }
        if isinstance(progress, dict):
            update["progress"] = progress
        for key in (
            "enable_http",
            "enable_tls",
            "enable_tls13",
            "enable_quic",
            "scan_level",
            "repeats",
            "repeat_parallel",
            "skip_dnscheck",
            "skip_ipblock",
            "curl_parallelism",
            "discovery_options",
        ):
            if key in run:
                update[key] = run[key]
        append_run(state_dir, update)
        closed += 1
    return closed


def latest_log_tail(
    state_dir: Path,
    max_lines: int = 200,
    *,
    run_id: str | None = None,
    stdout_from_size: int | None = None,
    stdout_log_match: str | None = None,
    stderr_from_size: int | None = None,
    stderr_log_match: str | None = None,
) -> dict[str, Any]:
    requested_run_id = str(run_id or "") if run_id is not None else None
    for run in reversed(read_runs(state_dir, limit=200)):
        if requested_run_id is not None and str(run.get("id") or "") != requested_run_id:
            continue
        stdout_log = Path(str(run.get("stdout_log") or ""))
        if not stdout_log.is_file():
            continue
        stderr_log_raw = str(run.get("stderr_log") or "")
        stderr_log = Path(stderr_log_raw) if stderr_log_raw else None
        stdout_delta = _log_delta(stdout_log, stdout_log_match, stdout_from_size)
        stderr_delta = _log_delta(stderr_log, stderr_log_match, stderr_from_size) if stderr_log and stderr_log.is_file() else None
        if stdout_delta is None:
            stdout_tail = "\n".join(_tail_lines(stdout_log, max_lines))
            stdout_append = ""
        else:
            stdout_tail = ""
            stdout_append = stdout_delta
        if stderr_delta is None:
            stderr_tail = "\n".join(_tail_lines(stderr_log, max_lines)) if stderr_log and stderr_log.is_file() else ""
            stderr_append = ""
        else:
            stderr_tail = ""
            stderr_append = stderr_delta
        stderr_diagnostics = classify_stderr_diagnostics("\n".join(part for part in (stderr_tail, stderr_append) if part))
        progress = run.get("progress")
        if not isinstance(progress, dict):
            progress = _read_progress_log(run)
        if not isinstance(progress, dict):
            if not stdout_tail:
                stdout_tail = "\n".join(_tail_lines(stdout_log, max_lines))
            progress = progress_from_stdout(stdout_tail, run)
            progress["partial"] = True
        return {
            "run_id": run.get("id"),
            "kind": run.get("kind"),
            "status": run.get("status"),
            "stdout_tail": stdout_tail,
            "stdout_append": stdout_append,
            "stderr_tail": stderr_tail,
            "stderr_append": stderr_append,
            "stderr_diagnostics": stderr_diagnostics,
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log) if stderr_log else "",
            "stdout_size": _file_version(stdout_log)["size"],
            "stderr_size": _file_version(stderr_log)["size"] if stderr_log and stderr_log.is_file() else 0,
            "progress": progress,
            "metrics": _read_latest_metrics(run),
            "run_settings": _run_settings_for_progress(run),
        }
    return {
        "run_id": requested_run_id,
        "kind": None,
        "status": None,
        "stdout_tail": "",
        "stdout_append": "",
        "stderr_tail": "",
        "stderr_append": "",
        "stderr_diagnostics": [],
        "stdout_size": 0,
        "stderr_size": 0,
        "progress": {} if requested_run_id is not None else progress_from_stdout("", {}),
        "metrics": {},
        "run_settings": {},
    }





def _run_settings_for_progress(run: dict[str, Any]) -> dict[str, Any]:
    options = run.get("discovery_options") if isinstance(run.get("discovery_options"), dict) else {}

    def option_value(key: str, fallback_keys: tuple[str, ...] = (), default: Any = None) -> Any:
        if key in options:
            return options[key]
        for fallback_key in fallback_keys:
            if fallback_key in run:
                return run[fallback_key]
        if key in run:
            return run[key]
        return default

    return {
        "domain_count": len(run.get("domains") or []),
        "kind": run.get("kind") or "",
        "enable_http": bool(option_value("enable_http", default=False)),
        "enable_tls12": bool(option_value("enable_tls12", ("enable_tls",), True)),
        "enable_tls13": bool(option_value("enable_tls13", default=False)),
        "enable_quic": bool(option_value("enable_quic", ("include_quic",), True)),
        "enable_ipv6": bool(option_value("enable_ipv6", default=False)),
        "scan_level": str(option_value("scan_level", default="standard") or "standard"),
        "repeats": _bounded_int(option_value("repeats", default=1), default=1, minimum=1, maximum=10),
        "repeat_parallel": bool(option_value("repeat_parallel", default=False)),
        "skip_dnscheck": bool(option_value("skip_dnscheck", default=True)),
        "skip_ipblock": bool(option_value("skip_ipblock", default=True)),
        "curl_parallelism": _minimum_int(run.get("curl_parallelism"), default=4, minimum=1)
        if str(run.get("kind") or "") == "multi-domain-discovery"
        else None,
        "timeout_seconds": _minimum_int(run.get("timeout_seconds"), default=0, minimum=0),
        "curl_max_time": _minimum_int(option_value("curl_max_time", default=2), default=2, minimum=1),
        "curl_max_time_quic": _minimum_int(option_value("curl_max_time_quic", default=2), default=2, minimum=1),
        "curl_max_time_doh": _minimum_int(option_value("curl_max_time_doh", default=2), default=2, minimum=1),
    }


def _read_progress_log(run: dict[str, Any]) -> dict[str, Any] | None:
    progress_log = str(run.get("progress_log") or "")
    if not progress_log:
        return None
    path = Path(progress_log)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_latest_metrics(run: dict[str, Any]) -> dict[str, Any]:
    metrics_log = str(run.get("metrics_log") or "")
    if not metrics_log:
        return {}
    path = Path(metrics_log)
    if not path.is_file():
        return {}
    for line in reversed(_tail_lines(path, 20)):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}





def upsert_candidates(state_dir: Path, parsed: dict[str, Any], run: dict[str, Any]) -> int:
    now = now_iso()
    for raw in parsed.get("candidates") or []:
        if not isinstance(raw, dict):
            continue
        candidate_id = candidate_id_for(str(raw.get("protocol")), str(raw.get("args")))
        upsert_candidate_event(
            state_dir,
            candidate_id=candidate_id,
            protocol=str(raw.get("protocol") or ""),
            args=str(raw.get("args") or ""),
            status="candidate",
            run_id=str(run.get("id") or ""),
            domain=str(raw.get("domain") or ""),
            domains=[],
            test=str(raw.get("test") or ""),
            ip_version=str(raw.get("ip_version") or ""),
            seen_at=now,
            common=False,
        )
    for raw in parsed.get("common_candidates") or []:
        if not isinstance(raw, dict):
            continue
        candidate_id = candidate_id_for(str(raw.get("protocol")), str(raw.get("args")))
        upsert_candidate_event(
            state_dir,
            candidate_id=candidate_id,
            protocol=str(raw.get("protocol") or ""),
            args=str(raw.get("args") or ""),
            status="candidate",
            run_id=str(run.get("id") or ""),
            domain="",
            domains=[str(item or "") for item in run.get("domains", [])] if isinstance(run.get("domains"), list) else [],
            test=str(raw.get("test") or ""),
            ip_version=str(raw.get("ip_version") or ""),
            seen_at=now,
            common=True,
        )
    return candidate_total(state_dir)


def candidate_total(state_dir: Path) -> int:
    with connect(state_dir) as conn:
        return int(conn.execute("SELECT COUNT(*) AS count FROM strategies").fetchone()["count"])


def candidate_id_for(protocol: str, args: str) -> str:
    digest = hashlib.sha256(f"{protocol}\n{args}".encode("utf-8")).hexdigest()[:12]
    return f"{protocol}-{digest}"


class _LiveStdoutRecorder:
    def __init__(self, state_dir: Path, run: dict[str, Any]):
        self._lock = threading.Lock()
        self._state_dir = state_dir
        self._run = run
        progress_log = str(run.get("progress_log") or "")
        self._progress_log = Path(progress_log) if progress_log else None
        fallback_log = str(run.get("summary_fallback_log") or "")
        self._summary_fallback_log = Path(fallback_log) if fallback_log else None
        self._metrics = _RuntimeMetricsSampler(state_dir, run)
        self._last_progress_attempted = -1
        self._last_progress_written_at = 0.0
        self._eta_baseline_attempted = 0
        self._eta_baseline_elapsed_seconds: int | None = None
        self._section = ""
        self._current_script = ""
        self._phase = PHASE_CHECK_VPN
        self._pending_attempt: str | None = None
        self._attempted = 0
        self._attempts_by_script: dict[str, int] = {}
        self._attempt_times: deque[float] = deque(maxlen=ETA_SAMPLE_MAX_POINTS)
        self._summary_verified = 0
        self._summary_fallbacks = 0
        self._summary_common_seen = 0
        self._summary_line_count = 0
        self._common_line_count = 0
        self._result_count = 0
        self._common_result_count = 0
        self._direct_available_count = 0
        self._not_working_count = 0
        self._candidate_count = 0
        self._common_candidate_count = 0
        self._domain_status_counts: dict[str, dict[str, int]] = {}
        self._domain_code_counts: dict[str, dict[str, int]] = {}
        self._curl_code_counts: dict[str, int] = {}
        self._curl_diagnostics: list[dict[str, Any]] = []
        self._candidate_keys: set[tuple[str, str, str, str, str]] = set()
        self._common_candidate_keys: set[tuple[str, str, str, str, str]] = set()
        self._successful_strategy_keys: set[tuple[str, str]] = set()
        self._candidate_samples: list[dict[str, Any]] = []
        self._common_candidate_samples: list[dict[str, Any]] = []
        self._pending_candidate_events: list[dict[str, Any]] = []
        self._candidate_writer_queue: queue.Queue[list[dict[str, Any]] | object] = queue.Queue(
            maxsize=LIVE_CANDIDATE_QUEUE_MAX_BATCHES
        )
        self._candidate_writer: threading.Thread | None = None
        self._candidate_writer_closed = False
        self._candidate_writer_error: BaseException | None = None

    def record_line(self, raw_line: str) -> None:
        line = raw_line.strip()
        if not line:
            return
        with self._lock:
            self._record_line_locked(line)
            self._write_progress_locked()

    def parsed(self) -> dict[str, Any]:
        self.close()
        with self._lock:
            candidates = list(self._candidate_samples)
            common_candidates = list(self._common_candidate_samples)
            return {
                "summary": [],
                "common": [],
                "live_summary": [str(item.get("raw") or "") for item in candidates if item.get("raw")],
                "candidates": candidates,
                "common_candidates": common_candidates,
                "results": [],
                "common_results": [],
                "direct_available": [],
                "not_working": [],
                "summary_line_count": self._summary_line_count,
                "common_line_count": self._common_line_count,
                "result_count": self._result_count,
                "common_result_count": self._common_result_count,
                "direct_available_count": self._direct_available_count,
                "not_working_count": self._not_working_count,
                "candidate_count": self._candidate_count,
                "common_candidate_count": self._common_candidate_count,
                "domain_diagnostics": _domain_diagnostics_from_counts(
                    self._domain_status_counts,
                    self._domain_code_counts,
                ),
                "curl_diagnostics": list(self._curl_diagnostics),
                "curl_diagnostics_summary": dict(self._curl_code_counts),
                "dominant_failure": _dominant_failure_from_counts(self._domain_status_counts),
                "phase": self._phase,
                "summary_verified": self._summary_verified,
                "summary_fallbacks": self._summary_fallbacks,
                "summary_common_seen": self._summary_common_seen,
                "live_recorded": True,
            }

    def progress(self, run: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            progress = self._progress_locked(run)
            self._write_progress_locked(force=True, run=run)
            return progress

    def mark_phase(self, phase: str, run: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._phase = phase
            self._write_progress_locked(force=True, run=run or self._run)

    def close(self) -> None:
        writer: threading.Thread | None = None
        with self._lock:
            if not self._candidate_writer_closed:
                self._enqueue_pending_candidate_events_locked()
                self._candidate_writer_closed = True
                if self._candidate_writer is not None:
                    self._candidate_writer_queue.put(_CANDIDATE_WRITER_STOP)
                    writer = self._candidate_writer
            elif self._candidate_writer is not None and self._candidate_writer.is_alive():
                writer = self._candidate_writer
        if writer is not None:
            writer.join()
        if self._candidate_writer_error is not None:
            raise RuntimeError("live candidate writer failed") from self._candidate_writer_error

    def _record_line_locked(self, line: str) -> None:
        if line == "* SUMMARY":
            self._section = "summary"
            self._phase = PHASE_SUMMARY
            return
        if line == "* COMMON":
            self._section = "common"
            self._phase = PHASE_SUMMARY
            return
        self._phase = _phase_from_line(line, self._phase)
        script = _script_name_from_line(line)
        if script:
            self._current_script = script
            self._phase = PHASE_DISCOVERY
            self._attempts_by_script.setdefault(script, 0)
            return
        if _ATTEMPT_RE.match(line):
            self._attempted += 1
            self._phase = PHASE_DISCOVERY
            self._attempt_times.append(time.monotonic())
            self._attempts_by_script[self._current_script] = self._attempts_by_script.get(self._current_script, 0) + 1
            self._maybe_update_eta_baseline_locked()
        attempt = _live_attempt_line(line)
        if attempt:
            self._pending_attempt = attempt
            return
        if line == "!!!!! AVAILABLE !!!!!" and self._pending_attempt:
            candidate = _candidate_from_result_line(self._pending_attempt, scope="domain")
            if candidate:
                self._record_candidate_locked(candidate, common=False)
            self._pending_attempt = None
            return
        if line.startswith("UNAVAILABLE") or line.startswith("FAILED"):
            self._record_unavailable_locked(line)
            self._pending_attempt = None
            return
        live_success = _candidate_from_live_success_line(line)
        if live_success:
            self._record_candidate_locked(live_success, common=False)
            return
        if self._section in {"summary", "common"}:
            if self._section == "common":
                self._common_line_count += 1
            else:
                self._summary_line_count += 1
            parsed = _parse_result_line(line)
            if parsed:
                if self._section == "common":
                    self._common_result_count += 1
                else:
                    self._result_count += 1
                    if parsed.get("result") == "working without bypass":
                        self._direct_available_count += 1
                        self._record_domain_status_locked(
                            str(parsed.get("domain") or ""),
                            "direct_available",
                        )
                    if "not working" in str(parsed.get("result") or ""):
                        self._not_working_count += 1
                        self._record_domain_status_locked(
                            str(parsed.get("domain") or ""),
                            "needs_discovery",
                        )
            candidate = _candidate_from_result_line(line, scope=self._section)
            if candidate:
                self._record_summary_candidate_locked(candidate, common=self._section == "common")

    def _maybe_update_eta_baseline_locked(self) -> None:
        baseline_attempted = _eta_recalculation_attempts(self._attempted)
        if baseline_attempted <= 0 or baseline_attempted == self._eta_baseline_attempted:
            return
        self._eta_baseline_attempted = baseline_attempted
        self._eta_baseline_elapsed_seconds = _elapsed_seconds(self._run.get("started_at") or self._run.get("timestamp"))

    def _record_unavailable_locked(self, line: str) -> None:
        if not self._pending_attempt:
            return
        parsed = _parse_result_line(self._pending_attempt)
        if not parsed:
            return
        domain = str(parsed.get("domain") or "")
        if not domain:
            return
        code = _curl_code_from_line(line)
        test = str(parsed.get("test") or "")
        info = curl_failure_info(code, test=test, domain=domain)
        status = str(info.get("status") or "curl_error")
        self._record_domain_status_locked(domain, status)
        if code:
            self._curl_code_counts[code] = self._curl_code_counts.get(code, 0) + 1
            domain_codes = self._domain_code_counts.setdefault(domain, {})
            domain_codes[code] = domain_codes.get(code, 0) + 1
        if len(self._curl_diagnostics) < LIVE_CANDIDATE_SAMPLE_LIMIT:
            self._curl_diagnostics.append(
                {
                    "domain": domain,
                    "test": test,
                    "protocol": _protocol_from_test(test),
                    "code": code,
                    "status": status,
                    "label": info.get("label") or status,
                    "message": info.get("message") or "",
                    "strategy_failure": _is_strategy_failure(info),
                }
            )

    def _record_domain_status_locked(self, domain: str, status: str) -> None:
        if not domain:
            return
        counts = self._domain_status_counts.setdefault(domain, {})
        counts[status] = counts.get(status, 0) + 1

    def _record_summary_candidate_locked(self, candidate: dict[str, Any], common: bool) -> None:
        if common:
            self._summary_common_seen += 1
            return
        live_key = (
            "domain",
            str(candidate.get("test") or ""),
            str(candidate.get("ip_version") or ""),
            str(candidate.get("domain") or ""),
            str(candidate.get("args") or ""),
        )
        if live_key in self._candidate_keys:
            self._summary_verified += 1
            return
        fallback = {**candidate, "scope": "domain", "source": "summary_fallback"}
        self._summary_fallbacks += 1
        self._record_candidate_locked(fallback, common=False)
        if self._summary_fallback_log:
            append_jsonl(
                self._summary_fallback_log,
                {
                    "run_id": self._run["id"],
                    "seen_at": now_iso(),
                    "reason": "summary candidate was not recorded by live parser",
                    "candidate": fallback,
                },
            )

    def _record_candidate_locked(self, candidate: dict[str, Any], common: bool) -> None:
        key = (
            str(candidate.get("scope") or ""),
            str(candidate.get("test") or ""),
            str(candidate.get("ip_version") or ""),
            str(candidate.get("domain") or ""),
            str(candidate.get("args") or ""),
        )
        target = self._common_candidate_keys if common else self._candidate_keys
        if key in target:
            return
        target.add(key)
        if common:
            self._common_candidate_count += 1
            if len(self._common_candidate_samples) < LIVE_CANDIDATE_SAMPLE_LIMIT:
                self._common_candidate_samples.append(candidate)
        else:
            self._candidate_count += 1
            if len(self._candidate_samples) < LIVE_CANDIDATE_SAMPLE_LIMIT:
                self._candidate_samples.append(candidate)
        self._successful_strategy_keys.add((str(candidate.get("protocol") or ""), str(candidate.get("args") or "")))
        candidate_id = candidate_id_for(str(candidate.get("protocol") or ""), str(candidate.get("args") or ""))
        self._pending_candidate_events.append(
            {
                "candidate_id": candidate_id,
                "protocol": str(candidate.get("protocol") or ""),
                "args": str(candidate.get("args") or ""),
                "status": "candidate",
                "run_id": str(self._run.get("id") or ""),
                "domain": str(candidate.get("domain") or ""),
                "domains": (
                    [str(item or "") for item in self._run.get("domains", [])]
                    if common and isinstance(self._run.get("domains"), list)
                    else []
                ),
                "test": str(candidate.get("test") or ""),
                "ip_version": str(candidate.get("ip_version") or ""),
                "seen_at": now_iso(),
                "common": common,
            }
        )
        if len(self._pending_candidate_events) >= LIVE_CANDIDATE_FLUSH_SIZE:
            self._enqueue_pending_candidate_events_locked()

    def _progress_locked(self, run: dict[str, Any]) -> dict[str, Any]:
        successful = len(self._successful_strategy_keys)
        sample = _average_attempt_ms(self._attempt_times)
        return _progress_from_counts(
            run=run,
            attempted=self._attempted,
            attempts_by_script=dict(self._attempts_by_script),
            successful=successful,
            current_script=self._current_script,
            phase=self._phase,
            runtime_ms_per_attempt=sample,
            runtime_sample_count=len(self._attempt_times),
            summary_verified=self._summary_verified,
            summary_fallbacks=self._summary_fallbacks,
            eta_recalculation_attempts_override=self._eta_baseline_attempted,
            eta_elapsed_seconds_override=self._eta_baseline_elapsed_seconds,
        )

    def _write_progress_locked(self, force: bool = False, run: dict[str, Any] | None = None) -> None:
        if not self._progress_log:
            return
        now = time.monotonic()
        attempt_delta = self._attempted - self._last_progress_attempted
        if not force and attempt_delta < _eta_recalculation_step(self._attempted) and now - self._last_progress_written_at < 2.0:
            return
        progress = self._progress_locked(run or self._run)
        tmp = self._progress_log.with_suffix(".json.tmp")
        try:
            self._progress_log.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(progress, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
            tmp.replace(self._progress_log)
            self._last_progress_attempted = self._attempted
            self._last_progress_written_at = now
            self._metrics.maybe_write(progress)
        except OSError:
            return

    def _enqueue_pending_candidate_events_locked(self) -> None:
        if not self._pending_candidate_events:
            return
        if self._candidate_writer_closed:
            raise RuntimeError("live candidate writer is already closed")
        if self._candidate_writer_error is not None:
            raise RuntimeError("live candidate writer failed") from self._candidate_writer_error
        self._ensure_candidate_writer_locked()
        events = self._pending_candidate_events
        self._pending_candidate_events = []
        self._candidate_writer_queue.put(events)

    def _ensure_candidate_writer_locked(self) -> None:
        if self._candidate_writer is not None and self._candidate_writer.is_alive():
            return
        if self._candidate_writer_error is not None:
            raise RuntimeError("live candidate writer failed") from self._candidate_writer_error
        self._candidate_writer = threading.Thread(
            target=self._candidate_writer_loop,
            name=f"gp-live-candidate-writer-{self._run.get('id') or 'run'}",
            daemon=True,
        )
        self._candidate_writer.start()

    def _candidate_writer_loop(self) -> None:
        try:
            while True:
                item = self._candidate_writer_queue.get()
                if item is _CANDIDATE_WRITER_STOP:
                    return
                events = item
                if not isinstance(events, list):
                    continue
                # A live run can last for hours.  Keep each candidate batch in
                # its own connection/transaction so the background writer does
                # not retain SQLite state between batches and authentication
                # readers remain independent of the discovery lifecycle.
                conn = connect(self._state_dir)
                try:
                    with conn:
                        for event in events:
                            upsert_candidate_event_conn(conn, **event)
                finally:
                    # sqlite3.Connection.__exit__ commits or rolls back but
                    # does not close a standard connection.  Closing here is
                    # deliberate: the writer must release its exact batch
                    # handle before waiting for the next queue item.
                    conn.close()
        except BaseException as exc:  # pragma: no cover - covered through close()
            self._candidate_writer_error = exc


class _RuntimeMetricsSampler:
    def __init__(self, state_dir: Path, run: dict[str, Any]):
        self._state_dir = state_dir
        self._run = run
        metrics_log = str(run.get("metrics_log") or "")
        self._path = Path(metrics_log) if metrics_log else None
        self._last_written_at = 0.0
        self._last_cpu: tuple[int, int, int] | None = None

    def maybe_write(self, progress: dict[str, Any]) -> None:
        if not self._path:
            return
        now = time.monotonic()
        if now - self._last_written_at < METRICS_INTERVAL_SECONDS:
            return
        self._last_written_at = now
        payload = {
            "timestamp": now_iso(),
            "run_id": self._run.get("id"),
            "phase": progress.get("phase"),
            "phase_label": progress.get("phase_label"),
            "current_script": progress.get("current_script"),
            "attempted": progress.get("attempted"),
            "attempt_total": progress.get("attempt_total"),
            "remaining_attempts": progress.get("remaining_attempts"),
            "successful": progress.get("successful"),
            "eta_seconds": progress.get("eta_seconds"),
            "eta_status": progress.get("eta_status"),
            "progress_status": progress.get("progress_status"),
            "processes": _process_counts(),
            "system": self._system_metrics(),
            "files": _runtime_file_sizes(self._state_dir, self._run),
        }
        try:
            _rotate_metrics_file(self._path)
            append_jsonl(self._path, payload)
        except OSError:
            return

    def _system_metrics(self) -> dict[str, Any]:
        return {
            "loadavg": _loadavg(),
            "cpu_percent": self._cpu_percent(),
            "memory": _meminfo(),
        }

    def _cpu_percent(self) -> dict[str, float] | None:
        current = _read_cpu_totals()
        if current is None:
            return None
        previous = self._last_cpu
        self._last_cpu = current
        if previous is None:
            return None
        total, idle, iowait = current
        prev_total, prev_idle, prev_iowait = previous
        delta_total = total - prev_total
        if delta_total <= 0:
            return None
        busy = max(0, delta_total - (idle - prev_idle))
        iowait_delta = max(0, iowait - prev_iowait)
        return {
            "busy": round((busy / delta_total) * 100.0, 1),
            "iowait": round((iowait_delta / delta_total) * 100.0, 1),
        }


def _rotate_metrics_file(path: Path) -> None:
    if not path.exists() or path.stat().st_size <= METRICS_MAX_BYTES:
        return
    rotated = path.with_suffix(path.suffix + ".1")
    try:
        if rotated.exists():
            rotated.unlink()
        path.replace(rotated)
    except OSError:
        return


def _runtime_file_sizes(state_dir: Path, run: dict[str, Any]) -> dict[str, int]:
    root = _finder_dir(state_dir)
    paths = {
        "stdout_log": Path(str(run.get("stdout_log") or "")),
        "stderr_log": Path(str(run.get("stderr_log") or "")),
        "progress_log": Path(str(run.get("progress_log") or "")),
        "sqlite": root / "state.sqlite3",
        "sqlite_wal": root / "state.sqlite3-wal",
    }
    return {name: _file_size(path) for name, path in paths.items()}


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size) if path.is_file() else 0
    except OSError:
        return 0


class _RotatingTextWriter:
    def __init__(self, path: Path, max_bytes: int):
        self._path = path
        self._max_bytes = max(1024, int(max_bytes))
        self._handle: Any | None = None

    def __enter__(self) -> "_RotatingTextWriter":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("w", encoding="utf-8")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def write(self, text: str) -> None:
        if self._handle is None:
            raise ValueError("writer is closed")
        self._handle.write(text)
        self._handle.flush()
        self._rotate_if_needed()

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def _rotate_if_needed(self) -> None:
        try:
            if not self._path.is_file() or self._path.stat().st_size <= self._max_bytes:
                return
        except OSError:
            return
        self.close()
        rotated = self._path.with_suffix(self._path.suffix + ".1")
        try:
            if rotated.exists():
                rotated.unlink()
            self._path.replace(rotated)
            self._handle = self._path.open("w", encoding="utf-8")
            self._handle.write(f"# log rotated, previous chunk: {rotated.name}\n")
            self._handle.flush()
        except OSError:
            self._handle = self._path.open("a", encoding="utf-8")


def _loadavg() -> list[float]:
    try:
        return [round(float(item), 2) for item in os.getloadavg()]
    except (AttributeError, OSError):
        return []


def _meminfo() -> dict[str, int]:
    path = Path("/proc/meminfo")
    if not path.is_file():
        return {}
    wanted = {"MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached", "SwapTotal", "SwapFree"}
    result: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            key, _, raw_value = line.partition(":")
            if key not in wanted:
                continue
            parts = raw_value.strip().split()
            if parts:
                result[key] = int(parts[0])
    except (OSError, ValueError):
        return {}
    return result


def _read_cpu_totals() -> tuple[int, int, int] | None:
    path = Path("/proc/stat")
    if not path.is_file():
        return None
    try:
        first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        parts = [int(item) for item in first.split()[1:]]
    except (OSError, IndexError, ValueError):
        return None
    if len(parts) < 5:
        return None
    idle = parts[3] + parts[4]
    iowait = parts[4]
    return (sum(parts), idle, iowait)


def _process_counts() -> dict[str, int]:
    proc = Path("/proc")
    if not proc.is_dir():
        return {"curl": 0, "nfqws2": 0, "blockcheck2": 0}
    counts = {"curl": 0, "nfqws2": 0, "blockcheck2": 0}
    for child in proc.iterdir():
        if not child.name.isdigit():
            continue
        try:
            cmdline = (child / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
        except OSError:
            continue
        if not cmdline:
            continue
        if "curl" in cmdline:
            counts["curl"] += 1
        if "nfqws2" in cmdline:
            counts["nfqws2"] += 1
        if "blockcheck2" in cmdline:
            counts["blockcheck2"] += 1
    return counts


class _CompactStdoutWriter:
    def __init__(self, handle: Any):
        self._handle = handle
        self._pending_attempt = ""
        self._attempt_lines = 0
        self._summary_lines = 0
        self._section = ""

    def write(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        if stripped in {"* SUMMARY", "* COMMON"}:
            self._flush_attempt_counter()
            self._section = stripped.removeprefix("* ").lower()
            self._write(line)
            return
        script = _script_name_from_line(stripped)
        if script:
            self._flush_attempt_counter()
            self._section = ""
            self._write(line)
            return
        attempt = _live_attempt_line(stripped)
        if attempt:
            self._pending_attempt = attempt
            self._attempt_lines += 1
            if self._attempt_lines % 1000 == 0:
                self._write(f"# compact-log: skipped {self._attempt_lines} attempt lines\n")
            return
        if stripped == "!!!!! AVAILABLE !!!!!":
            if self._pending_attempt:
                self._write(self._pending_attempt + "\n")
            self._write(line)
            self._pending_attempt = ""
            return
        if _candidate_from_live_success_line(stripped):
            self._write(line)
            return
        if stripped.startswith("UNAVAILABLE") or stripped.startswith("FAILED"):
            self._pending_attempt = ""
            return
        if self._section in {"summary", "common"}:
            self._summary_lines += 1
            if self._summary_lines % 1000 == 0:
                self._write(f"# compact-log: skipped {self._summary_lines} summary/common lines\n")
            return
        if stripped.startswith("* "):
            self._write(line)

    def close(self) -> None:
        self._flush_attempt_counter()

    def _flush_attempt_counter(self) -> None:
        if self._attempt_lines:
            self._write(f"# compact-log: total attempt lines skipped {self._attempt_lines}\n")
            self._attempt_lines = 0
        self._pending_attempt = ""

    def _write(self, line: str) -> None:
        self._handle.write(line)
        self._handle.flush()


def _stdout_log_mode(env: dict[str, str]) -> str:
    if _truthy(env.get("GP_DEBUG_STDOUT"), default=False):
        return "debug"
    if _truthy(env.get("GP_COMPACT_STDOUT"), default=False):
        return "compact"
    return "raw"


def _set_debug_stdout_env(env: dict[str, str], debug_stdout: bool | None) -> None:
    if debug_stdout is True:
        env["GP_DEBUG_STDOUT"] = "1"
    elif debug_stdout is False:
        env.pop("GP_DEBUG_STDOUT", None)


def stop_active_blockcheck_runtime(state_dir: Path | None = None) -> None:
    run_id = str(active_runtime_payload(state_dir).get("run_id") or "").strip() if state_dir else ""
    if run_id:
        try:
            signal_registered_process_run(run_id, "TERM")
        except RuntimeError:
            pass
    _cleanup_nft_blockcheck_tables()


def _stop_requested(stop_event: threading.Event | None) -> bool:
    return stop_event is not None and stop_event.is_set()


@contextmanager
def _claim_child_launch(stop_event: threading.Event | None) -> Iterator[bool]:
    """Use JobRunner's atomic cancellation claim when available."""
    claim = getattr(stop_event, "claim_child_launch", None)
    if callable(claim):
        with claim() as claimed:
            yield bool(claimed)
        return
    yield not _stop_requested(stop_event)


def _stopped_process_result(recorder: _LiveStdoutRecorder) -> dict[str, Any]:
    recorder.mark_phase(PHASE_SAVING)
    return {
        "status": "stopped",
        "returncode": None,
        "timed_out": False,
        "stopped": True,
    }


def _root_command_unless_stopped(
    command: list[str],
    *,
    env: dict[str, str],
    stop_event: threading.Event | None,
    **kwargs: Any,
) -> list[str] | None:
    if _stop_requested(stop_event):
        return None
    try:
        rooted_command = root_command(command, env=env, **kwargs)
    except Exception:
        if _stop_requested(stop_event):
            return None
        raise
    return None if _stop_requested(stop_event) else rooted_command


def _run_process_with_live_stdout(
    command: list[str],
    env: dict[str, str],
    stdout_log: Path | None,
    stderr_log: Path,
    debug_stdout_log: Path | None,
    timeout_seconds: int,
    stop_event: threading.Event | None,
    recorder: _LiveStdoutRecorder,
    run_id: str = "",
    *,
    stdin=None,
    stream_stderr: bool = False,
    preserve_natural_exit: bool = False,
) -> dict[str, Any]:
    if _stop_requested(stop_event):
        return _stopped_process_result(recorder)

    status = "success"
    returncode: int | None = None
    timed_out = False
    stopped = False
    reader_errors: list[BaseException] = []

    stdout_mode = _stdout_log_mode(env)
    with ExitStack() as log_stack:
        try:
            debug_handle = (
                log_stack.enter_context(_RotatingTextWriter(debug_stdout_log, DEBUG_STDOUT_LOG_MAX_BYTES))
                if debug_stdout_log and stdout_mode == "debug"
                else None
            )
            stdout_handle = (
                log_stack.enter_context(_RotatingTextWriter(stdout_log, STDOUT_LOG_MAX_BYTES))
                if stdout_log is not None else log_stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
            )
            stderr_handle = log_stack.enter_context(stderr_log.open("w", encoding="utf-8"))
        except Exception:
            if _stop_requested(stop_event):
                return _stopped_process_result(recorder)
            raise

        compact_writer = _CompactStdoutWriter(stdout_handle)
        try:
            with _claim_child_launch(stop_event) as claimed:
                if not claimed:
                    return _stopped_process_result(recorder)
                process = subprocess.Popen(
                    command,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdin=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT if stream_stderr else stderr_handle,
                    env=env,
                    start_new_session=hasattr(os, "setsid"),
                )
            def read_stdout() -> None:
                try:
                    if process.stdout is None:
                        return
                    for line in process.stdout:
                        if stdout_mode == "compact":
                            compact_writer.write(line)
                        else:
                            stdout_handle.write(line)
                            stdout_handle.flush()
                        if debug_handle:
                            debug_handle.write(line)
                        recorder.record_line(line)
                except BaseException as exc:  # noqa: BLE001
                    reader_errors.append(exc)
                finally:
                    if process.stdout is not None:
                        process.stdout.close()
                    compact_writer.close()
                    if debug_handle:
                        debug_handle.flush()

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            deadline = None if timeout_seconds <= 0 else time.monotonic() + timeout_seconds
            while True:
                cancel_requested = stop_event is not None and stop_event.is_set()
                if (reader_errors or cancel_requested) and (not preserve_natural_exit or process.poll() is None):
                    try:
                        _stop_process_group(process, run_id)
                        _cleanup_nft_blockcheck_tables()
                        recorder.mark_phase(PHASE_SAVING)
                        returncode = _wait_process_after_stop(process, run_id)
                    except (RuntimeError, subprocess.TimeoutExpired) as exc:
                        raise ManagedRuntimeQuarantinedError(f"managed process cleanup is unverified: {exc}") from exc
                    stopped = cancel_requested
                    status = "stopped" if stopped else "failed"
                    break
                wait_timeout = 1.0
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        status = "timeout"
                        try:
                            _stop_process_group(process, run_id)
                            _cleanup_nft_blockcheck_tables()
                            recorder.mark_phase(PHASE_SAVING)
                            returncode = _wait_process_after_stop(process, run_id)
                        except (RuntimeError, subprocess.TimeoutExpired) as exc:
                            raise ManagedRuntimeQuarantinedError(f"managed process cleanup is unverified: {exc}") from exc
                        break
                    wait_timeout = min(1.0, remaining)
                try:
                    returncode = process.wait(timeout=wait_timeout)
                    if not preserve_natural_exit and stop_event is not None and stop_event.is_set():
                        stopped = True
                        status = "stopped"
                        if run_id:
                            try:
                                acknowledge_registered_process_run_terminal(run_id)
                            except RuntimeError as exc:
                                raise ManagedRuntimeQuarantinedError(
                                    f"managed process cleanup is unverified: {exc}"
                                ) from exc
                        _cleanup_nft_blockcheck_tables()
                        recorder.mark_phase(PHASE_SAVING)
                        break
                    if returncode != 0:
                        status = "failed"
                    break
                except subprocess.TimeoutExpired:
                    continue
            reader.join(timeout=5)
            if reader.is_alive():
                raise ManagedRuntimeQuarantinedError("managed stdout reader did not terminate")
        finally:
            compact_writer.close()

    if reader_errors:
        raise RuntimeError(f"failed to read blockcheck stdout: {reader_errors[0]}")
    return {
        "status": status,
        "returncode": returncode,
        "timed_out": timed_out,
        "stopped": stopped,
    }


def _wait_process_after_stop(
    process: subprocess.Popen[str], run_id: str | None, timeout_seconds: float = 5.0
) -> int | None:
    if process.returncode is not None:
        return process.returncode
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _stop_process_group(process, run_id)
        try:
            return process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            raise RuntimeError("managed process did not terminate after stop")


def _run_blockcheck_live(
    state_dir: Path,
    kind: str,
    domains: list[str],
    timeout_seconds: int,
    test: str,
    options: DiscoveryOptions,
    candidate_id: str = "",
    domain_validation: dict[str, Any] | None = None,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    if not blockcheck:
        raise RuntimeError("blockcheck2.sh/blockcheck.sh not found in PATH")
    options = options.normalized()
    domain_validation = domain_validation or validate_domain_inputs(domains, default_to_critical=True)
    clean_domains = list(domain_validation["domains"])
    if not clean_domains:
        raise ValueError("no valid domains to check")
    validation_fields = _domain_validation_run_fields(domain_validation)
    blockcheck_path = _resolve_blockcheck_script(Path(blockcheck))
    full_env = os.environ.copy()
    full_env.update(
        {
            "BATCH": "1",
            "DOMAINS": " ".join(clean_domains),
            "IPVS": _ipvs_value(options),
            "TEST": test,
            **options.to_blockcheck_env(),
        }
    )
    _set_debug_stdout_env(full_env, debug_stdout)
    root = _finder_dir(state_dir)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    _cleanup_old_strategy_logs(logs)
    run_id = _discovery_run_id(run_id)
    stdout_log = logs / f"{run_id}.{kind}.stdout.log"
    stderr_log = logs / f"{run_id}.{kind}.stderr.log"
    progress_log = logs / f"{run_id}.{kind}.progress.json"
    metrics_log = logs / f"{run_id}.{kind}.metrics.ndjson"
    summary_fallback_log = logs / f"{run_id}.{kind}.summary-fallback.ndjson"
    debug_stdout_log = logs / f"{run_id}.{kind}.debug.stdout.log"
    attempt_plan = _standard_attempt_plan(
        domains=clean_domains,
        test=test,
        enable_http=options.enable_http,
        enable_tls=options.enable_tls12,
        enable_tls13=options.enable_tls13,
        enable_quic=options.enable_quic,
        enable_ipv6=options.enable_ipv6,
    )
    option_fields = options.to_run_fields()
    started_at = now_iso()
    started = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": "running",
        "timestamp": started_at,
        "started_at": started_at,
        "domains": clean_domains,
        "returncode": None,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "metrics_log": str(metrics_log),
        "summary_fallback_log": str(summary_fallback_log),
        "debug_stdout_log": str(debug_stdout_log),
        "stdout_log_mode": _stdout_log_mode(full_env),
        "debug_stdout": _stdout_log_mode(full_env) == "debug",
        "candidate_count": 0,
        "phase": PHASE_CHECK_VPN,
        "test": test,
        **option_fields,
        **validation_fields,
        "attempt_plan": attempt_plan,
    }
    append_run(state_dir, started)

    recorder = _LiveStdoutRecorder(state_dir, started)
    command = _root_command_unless_stopped(
        [str(blockcheck_path)],
        env=full_env,
        pass_env_keys=BLOCKCHECK_ENV_KEYS,
        run_id=run_id,
        stop_event=stop_event,
    )
    if command is None:
        process_result = _stopped_process_result(recorder)
    else:
        process_result = _run_process_with_live_stdout(
            command=command,
            env=full_env,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            debug_stdout_log=debug_stdout_log,
            timeout_seconds=timeout_seconds,
            stop_event=stop_event,
            recorder=recorder,
            run_id=run_id,
        )
    parsed = recorder.parsed()
    completed_at = now_iso()
    run = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": process_result["status"],
        "timestamp": started_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "domains": clean_domains,
        "returncode": process_result["returncode"],
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "metrics_log": str(metrics_log),
        "summary_fallback_log": str(summary_fallback_log),
        "debug_stdout_log": str(debug_stdout_log),
        "stdout_log_mode": _stdout_log_mode(full_env),
        "debug_stdout": _stdout_log_mode(full_env) == "debug",
        "candidate_count": int(parsed.get("candidate_count") or len(parsed["candidates"])),
        "common_candidate_count": int(parsed.get("common_candidate_count") or len(parsed["common_candidates"])),
        "summary_line_count": int(parsed.get("summary_line_count") or 0),
        "common_line_count": int(parsed.get("common_line_count") or 0),
        "result_count": int(parsed.get("result_count") or 0),
        "common_result_count": int(parsed.get("common_result_count") or 0),
        "direct_available_count": int(parsed.get("direct_available_count") or 0),
        "not_working_count": int(parsed.get("not_working_count") or 0),
        "phase": parsed.get("phase") or PHASE_COMPLETE,
        "domain_diagnostics": parsed.get("domain_diagnostics") or [],
        "curl_diagnostics": parsed.get("curl_diagnostics") or [],
        "curl_diagnostics_summary": parsed.get("curl_diagnostics_summary") or {},
        "dominant_failure": parsed.get("dominant_failure") or {},
        "summary_verified": parsed.get("summary_verified", 0),
        "summary_fallbacks": parsed.get("summary_fallbacks", 0),
        "summary_common_seen": parsed.get("summary_common_seen", 0),
        "timed_out": process_result["timed_out"],
        "stopped": process_result["stopped"],
        "timeout_seconds": timeout_seconds,
        "test": test,
        **option_fields,
        **validation_fields,
        "attempt_plan": attempt_plan,
    }
    run["progress"] = recorder.progress(run)
    if kind in {"standard-discovery", "multi-domain-discovery"}:
        run["total_candidates"] = candidate_total(state_dir) if parsed.get("live_recorded") else upsert_candidates(state_dir, parsed, run)
    recorder.close()
    append_run(state_dir, run)
    return run


def _run_multidomain_blockcheck_live(
    state_dir: Path,
    domains: list[str],
    timeout_seconds: int,
    options: DiscoveryOptions,
    curl_parallelism: int,
    domain_validation: dict[str, Any] | None = None,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    if not blockcheck:
        raise RuntimeError("blockcheck2.sh/blockcheck.sh not found in PATH")
    options = options.normalized()
    domain_validation = domain_validation or validate_domain_inputs(domains, default_to_critical=True)
    clean_domains = list(domain_validation["domains"])
    if not clean_domains:
        raise ValueError("no valid domains to check")
    blockcheck_path = _resolve_blockcheck_script(Path(blockcheck))
    zapret_base = blockcheck_path.parent
    normalized_parallelism = _minimum_int(curl_parallelism, default=4, minimum=1)

    full_env = os.environ.copy()
    full_env.update(
        {
            "BATCH": "1",
            "DOMAINS": " ".join(clean_domains),
            "IPVS": _ipvs_value(options),
            "TEST": "standard",
            **options.to_blockcheck_env(),
            "GP_MD_CURL_PARALLELISM": str(normalized_parallelism),
            "ZAPRET_BASE": str(zapret_base),
            "ZAPRET_RW": str(zapret_base),
        }
    )
    _set_debug_stdout_env(full_env, debug_stdout)
    run_id = _discovery_run_id(run_id)
    command = _root_command_unless_stopped(
        [str(blockcheck_path)],
        env=full_env,
        pass_env_keys=BLOCKCHECK_ENV_KEYS,
        helper_command="run-multidomain",
        run_id=run_id,
        stop_event=stop_event,
    )
    return _run_blockcheck_command_live(
        command=command,
        env=full_env,
        state_dir=state_dir,
        kind="multi-domain-discovery",
        domains=clean_domains,
        timeout_seconds=timeout_seconds,
        test="standard",
        options=options,
        curl_parallelism=normalized_parallelism,
        domain_validation=domain_validation,
        debug_stdout=debug_stdout,
        stop_event=stop_event,
        run_id=run_id,
    )


def _run_blockcheck_command_live(
    command: list[str] | None,
    env: dict[str, str],
    state_dir: Path,
    kind: str,
    domains: list[str],
    timeout_seconds: int,
    test: str,
    options: DiscoveryOptions,
    curl_parallelism: int | None = None,
    candidate_id: str = "",
    domain_validation: dict[str, Any] | None = None,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    options = options.normalized()
    domain_validation = domain_validation or validate_domain_inputs(domains, default_to_critical=True)
    domains = list(domain_validation["domains"])
    if not domains:
        raise ValueError("no valid domains to check")
    validation_fields = _domain_validation_run_fields(domain_validation)
    _set_debug_stdout_env(env, debug_stdout)
    root = _finder_dir(state_dir)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    run_id = _discovery_run_id(run_id)
    stdout_log = logs / f"{run_id}.{kind}.stdout.log"
    stderr_log = logs / f"{run_id}.{kind}.stderr.log"
    progress_log = logs / f"{run_id}.{kind}.progress.json"
    metrics_log = logs / f"{run_id}.{kind}.metrics.ndjson"
    summary_fallback_log = logs / f"{run_id}.{kind}.summary-fallback.ndjson"
    debug_stdout_log = logs / f"{run_id}.{kind}.debug.stdout.log"
    attempt_plan = (
        _empty_attempt_plan(test)
        if command is None
        else _standard_attempt_plan(
            domains=domains,
            test=test,
            enable_http=options.enable_http,
            enable_tls=options.enable_tls12,
            enable_tls13=options.enable_tls13,
            enable_quic=options.enable_quic,
            enable_ipv6=options.enable_ipv6,
        )
    )
    option_fields = options.to_run_fields()
    started_at = now_iso()
    started = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": "stopped" if command is None else "running",
        "timestamp": started_at,
        "started_at": started_at,
        "domains": domains,
        "returncode": None,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "metrics_log": str(metrics_log),
        "summary_fallback_log": str(summary_fallback_log),
        "debug_stdout_log": str(debug_stdout_log),
        "stdout_log_mode": _stdout_log_mode(env),
        "debug_stdout": _stdout_log_mode(env) == "debug",
        "candidate_count": 0,
        "phase": PHASE_CHECK_VPN,
        "test": test,
        **option_fields,
        "curl_parallelism": curl_parallelism,
        **validation_fields,
        "attempt_plan": attempt_plan,
    }
    append_run(state_dir, started)

    recorder = _LiveStdoutRecorder(state_dir, started)
    process_result = (
        _stopped_process_result(recorder)
        if command is None
        else _run_process_with_live_stdout(
            command=command,
            env=env,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            debug_stdout_log=debug_stdout_log,
            timeout_seconds=timeout_seconds,
            stop_event=stop_event,
            recorder=recorder,
            run_id=run_id,
        )
    )
    parsed = recorder.parsed()
    completed_at = now_iso()
    run = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": process_result["status"],
        "timestamp": started_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "domains": domains,
        "returncode": process_result["returncode"],
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "metrics_log": str(metrics_log),
        "summary_fallback_log": str(summary_fallback_log),
        "debug_stdout_log": str(debug_stdout_log),
        "stdout_log_mode": _stdout_log_mode(env),
        "debug_stdout": _stdout_log_mode(env) == "debug",
        "candidate_count": int(parsed.get("candidate_count") or len(parsed["candidates"])),
        "common_candidate_count": int(parsed.get("common_candidate_count") or len(parsed["common_candidates"])),
        "summary_line_count": int(parsed.get("summary_line_count") or 0),
        "common_line_count": int(parsed.get("common_line_count") or 0),
        "result_count": int(parsed.get("result_count") or 0),
        "common_result_count": int(parsed.get("common_result_count") or 0),
        "direct_available_count": int(parsed.get("direct_available_count") or 0),
        "not_working_count": int(parsed.get("not_working_count") or 0),
        "phase": parsed.get("phase") or PHASE_COMPLETE,
        "domain_diagnostics": parsed.get("domain_diagnostics") or [],
        "curl_diagnostics": parsed.get("curl_diagnostics") or [],
        "curl_diagnostics_summary": parsed.get("curl_diagnostics_summary") or {},
        "dominant_failure": parsed.get("dominant_failure") or {},
        "summary_verified": parsed.get("summary_verified", 0),
        "summary_fallbacks": parsed.get("summary_fallbacks", 0),
        "summary_common_seen": parsed.get("summary_common_seen", 0),
        "timed_out": process_result["timed_out"],
        "stopped": process_result["stopped"],
        "timeout_seconds": timeout_seconds,
        "test": test,
        **option_fields,
        "curl_parallelism": curl_parallelism,
        **validation_fields,
        "attempt_plan": attempt_plan,
    }
    run["progress"] = recorder.progress(run)
    if kind in {"standard-discovery", "multi-domain-discovery"}:
        run["total_candidates"] = candidate_total(state_dir) if parsed.get("live_recorded") else upsert_candidates(state_dir, parsed, run)
    recorder.close()
    append_run(state_dir, run)
    return run


def progress_from_stdout(stdout: str, run: dict[str, Any]) -> dict[str, Any]:
    return _progress_from_counts(run=run, **_progress_calculation.stdout_observations(stdout))


def _progress_from_counts(
    *,
    run: dict[str, Any],
    attempted: int,
    attempts_by_script: dict[str, int],
    successful: int,
    current_script: str,
    phase: str = PHASE_CHECK_VPN,
    runtime_ms_per_attempt: int | None = None,
    runtime_sample_count: int | None = None,
    summary_verified: int = 0,
    summary_fallbacks: int = 0,
    elapsed_seconds_override: int | None = None,
    eta_recalculation_attempts_override: int | None = None,
    eta_elapsed_seconds_override: int | None = None,
) -> dict[str, Any]:
    attempt_plan = _attempt_plan_for_run(run, current_script)
    observed_scripts = [f"standard/{path.name}" for path in _standard_scripts()] if not attempt_plan.get("script_order") and current_script.startswith("standard/") else []
    return _progress_calculation._progress_from_counts(
        run=run, attempted=attempted, attempts_by_script=attempts_by_script, successful=successful, current_script=current_script, phase=phase, runtime_ms_per_attempt=runtime_ms_per_attempt, runtime_sample_count=runtime_sample_count, summary_verified=summary_verified, summary_fallbacks=summary_fallbacks, elapsed_seconds_override=elapsed_seconds_override, eta_recalculation_attempts_override=eta_recalculation_attempts_override, eta_elapsed_seconds_override=eta_elapsed_seconds_override,
        attempt_plan=attempt_plan, observed_script_order=observed_scripts,
        observed_elapsed_seconds=_elapsed_seconds(run.get("started_at") or run.get("timestamp")) if elapsed_seconds_override is None else elapsed_seconds_override,
    )
























def _attempt_plan_for_run(run: dict[str, Any], current_script: str) -> dict[str, Any]:
    raw_plan = run.get("attempt_plan")
    if isinstance(raw_plan, dict):
        if int(raw_plan.get("total") or 0) > 0 or run.get("status") == "stopped" or run.get("stopped"):
            return raw_plan
    if not current_script.startswith("standard/") and str(run.get("test") or "standard") != "standard":
        return _empty_attempt_plan(str(run.get("test") or ""))
    return _standard_attempt_plan(
        domains=[str(item) for item in run.get("domains") or []],
        test=str(run.get("test") or "standard"),
        enable_http=_truthy(run.get("enable_http"), default=False),
        enable_tls=_truthy(run.get("enable_tls"), default=True),
        enable_tls13=_truthy(run.get("enable_tls13"), default=False),
        enable_quic=_truthy(run.get("enable_quic"), default=True),
        enable_ipv6=_truthy(run.get("enable_ipv6"), default=False),
    )


def _standard_attempt_plan(
    domains: list[str],
    test: str = "standard",
    enable_http: bool = False,
    enable_tls: bool = True,
    enable_tls13: bool = False,
    enable_quic: bool = True,
    enable_ipv6: bool = False,
    root: Path | None = None,
) -> dict[str, Any]:
    if test != "standard":
        return _empty_attempt_plan(test)
    root = root or _blockcheck_test_dir(test)
    if not root.exists():
        return _empty_attempt_plan(test)
    scripts = _standard_scripts(root)
    domain_count = len(_clean_domains(domains))
    ip_version_count = 2 if enable_ipv6 else 1
    fingerprint = tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in scripts)
    key = (
        str(root),
        fingerprint,
        domain_count,
        bool(enable_http),
        bool(enable_tls),
        bool(enable_tls13),
        bool(enable_quic),
        bool(enable_ipv6),
    )
    cached = _ATTEMPT_PLAN_CACHE.get(key)
    if cached:
        return cached

    enabled_functions: list[str] = []
    if enable_http:
        enabled_functions.append("pktws_check_http")
    if enable_tls:
        enabled_functions.append("pktws_check_https_tls12")
    if enable_tls13:
        enabled_functions.append("pktws_check_https_tls13")
    if enable_quic:
        enabled_functions.append("pktws_check_http3")

    script_totals: dict[str, int] = {}
    strategy_script_totals: dict[str, int] = {}
    script_order: list[str] = []
    source = "shell"
    for script in scripts:
        name = f"{test}/{script.name}"
        script_order.append(name)
        per_domain = 0
        for function_name in enabled_functions:
            counted = _count_script_function_attempts(root, script, function_name)
            if counted is None:
                source = "static"
                per_domain = _count_script_attempts_static(script)
                break
            per_domain += counted
        script_totals[name] = per_domain * domain_count * ip_version_count
        strategy_script_totals[name] = per_domain

    total = sum(script_totals.values())
    strategy_total = sum(strategy_script_totals.values())
    plan = {
        "test": test,
        "total": total,
        "scripts": script_totals,
        "strategy_total": strategy_total,
        "strategy_scripts": strategy_script_totals,
        "script_order": script_order,
        "domain_count": domain_count,
        "ip_version_count": ip_version_count,
        "source": source if total else "",
    }
    _ATTEMPT_PLAN_CACHE[key] = plan
    return plan


def _empty_attempt_plan(test: str) -> dict[str, Any]:
    return {
        "test": test,
        "total": 0,
        "scripts": {},
        "strategy_total": 0,
        "strategy_scripts": {},
        "script_order": [],
        "domain_count": 0,
        "ip_version_count": 1,
        "source": "",
    }


def _blockcheck_test_dir(test: str) -> Path:
    base = Path(os.environ.get("GP_BLOCKCHECK2D", "/opt/zapret2/blockcheck2.d"))
    return base / test


def _standard_scripts(root: Path | None = None) -> list[Path]:
    root = root or _blockcheck_test_dir("standard")
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*.sh") if path.is_file() and path.name != "def.inc")


def _count_script_function_attempts(root: Path, script: Path, function_name: str) -> int | None:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", function_name):
        return None
    shell = shutil.which("sh")
    if not shell:
        return None
    probe = "\n".join(
        [
            f"TESTDIR={shlex.quote(str(root))}",
            "SCANLEVEL=force",
            "IPV=4",
            "IPVV=",
            "UNAME=Linux",
            "pktws_curl_test_update() { echo __GP_ATTEMPT__; return 1; }",
            f". {shlex.quote(str(script))}",
            f"if command -v {function_name} >/dev/null 2>&1; then {function_name} curl_test_probe example.com; fi",
        ]
    )
    try:
        result = subprocess.run(
            [shell, "-c", probe],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    count = sum(1 for line in result.stdout.splitlines() if line.strip() == "__GP_ATTEMPT__")
    if result.returncode != 0 and count == 0:
        return None
    return count


def _count_script_attempts_static(script: Path) -> int:
    try:
        text = script.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    loop_stack: list[int] = []
    total = 0
    for raw_line in text.splitlines():
        line = _strip_shell_comment(raw_line).strip()
        if not line:
            continue
        loop_match = re.match(r"^for\s+\w+\s+in\s+(.+?);?\s+do\s*$", line)
        if loop_match:
            loop_stack.append(max(1, _shell_word_count(loop_match.group(1))))
            continue
        if "pktws_curl_test_update" in line:
            multiplier = 1
            for value in loop_stack:
                multiplier *= value
            total += multiplier
        if line == "done" or line.endswith("; done"):
            if loop_stack:
                loop_stack.pop()
    return total


def _strip_shell_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    result: list[str] = []
    for char in line:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            break
        result.append(char)
    return "".join(result)


def _shell_word_count(value: str) -> int:
    try:
        return len(shlex.split(value))
    except ValueError:
        return len([part for part in value.split() if part])






















































































def _standard_script_total() -> int:
    return len(_standard_scripts())


def _standard_script_index(current_script: str, script_order: list[str] | None = None) -> int:
    if not current_script.startswith("standard/"):
        return 0
    scripts = script_order or [f"standard/{path.name}" for path in _standard_scripts()]
    try:
        return scripts.index(current_script) + 1
    except ValueError:
        return 0


def _elapsed_seconds(value: Any) -> int | None:
    # Observe wall time here; the calculation accepts an explicit clock value.
    if not value:
        return None
    try:
        started = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _progress_calculation.elapsed_seconds(value, datetime.now(started.tzinfo))





def _write_multidomain_runner(root: Path, blockcheck: Path) -> Path:
    source = blockcheck.read_text(encoding="utf-8", errors="replace")
    marker = "\nfsleep_setup\n"
    if marker not in source:
        raise RuntimeError("unsupported blockcheck2.sh layout: main marker not found")
    prefix = source.split(marker, 1)[0]
    runner = root / "gp-multidomain-blockcheck.sh"
    runner.write_text(prefix + MULTIDOMAIN_BLOCKCHECK_MAIN, encoding="utf-8")
    runner.chmod(0o700)
    return runner


def _resolve_blockcheck_script(path: Path) -> Path:
    current = path.resolve()
    seen: set[Path] = set()
    for _ in range(5):
        if current in seen:
            break
        seen.add(current)
        try:
            text = current.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return current
        if "\nfsleep_setup\n" in text:
            return current
        target = _exec_target_from_shell_wrapper(text)
        if not target:
            return current
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = current.parent / candidate
        current = candidate.resolve()
    return current


def _exec_target_from_shell_wrapper(text: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("exec "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        for part in parts[1:]:
            if part.endswith(("blockcheck2.sh", "blockcheck.sh")):
                return part
    return ""


MULTIDOMAIN_BLOCKCHECK_MAIN = r'''

gp_md_primary_domain()
{
	local d
	for d in $DOMAINS; do
		echo "$d"
		return
	done
}

gp_md_resolve_all_ips()
{
	local d ips all_ips
	for d in $DOMAINS; do
		mdig_resolve_all $IPV ips "$d"
		all_ips="${all_ips:+$all_ips }$ips"
	done
	echo "$all_ips" | tr ' ' '\n' | sort -u | tr '\n' ' '
}

gp_md_normalize_ip_list()
{
	local ip result
	for ip in $1; do
		result="${result:+$result }$ip"
	done
	echo "$result"
}

gp_md_parallel_limit()
{
	local n="${GP_MD_CURL_PARALLELISM:-4}"
	case "$n" in
		""|*[!0-9]*) n=4 ;;
	esac
	n=$((n + 0))
	[ "$n" -lt 1 ] && n=1
	echo "$n"
}

gp_md_out_file()
{
	echo "${PARALLEL_OUT}_md_$1.out"
}

gp_md_code_file()
{
	echo "${PARALLEL_OUT}_md_$1.code"
}

gp_md_run_domain_curl()
{
	# $1 - index
	# $2 - test function
	# $3 - domain
	local idx=$1 testf=$2 gp_domain="$3" code out codefile
	out="$(gp_md_out_file "$idx")"
	codefile="$(gp_md_code_file "$idx")"
	curl_test "$testf" "$gp_domain" >"$out" 2>&1
	code=$?
	echo "$code" >"$codefile"
	return 0
}

gp_md_collect_record()
{
	# $1 - pid:index:domain
	# $2 - test function
	# $3 - strategy text
	local record="$1" testf=$2 strategy_text="$3" pid rest idx gp_domain code out codefile
	pid="${record%%:*}"
	rest="${record#*:}"
	idx="${rest%%:*}"
	gp_domain="${rest#*:}"

	wait "$pid" 2>/dev/null
	out="$(gp_md_out_file "$idx")"
	codefile="$(gp_md_code_file "$idx")"
	code="$(cat "$codefile" 2>/dev/null)"
	[ -n "$code" ] || code=1

	echo "- $testf ipv$IPV $gp_domain : $PKTWSD ${WF:+$WF }$strategy_text"
	[ -f "$out" ] && cat "$out"
	rm -f "$out" "$codefile"
	if [ "$code" = 0 ]; then
		echo "!!!!! $testf: working strategy found for ipv$IPV $gp_domain : nfqws2 ${WF:+$WF }$strategy_text !!!!!"
		report_append "$gp_domain" "$testf ipv${IPV}" "$PKTWSD ${WF:+$WF }$strategy_text"
		return 0
	fi
	echo "GP-MULTIDOMAIN unavailable code=$code"
	return 1
}

pktws_curl_test_update()
{
	# $1 - curl test function
	# $2 - sample domain from the standard zapret2 script
	# $3+ - nfqws2 args
	local testf=$1 dom="$2" strategy ok=0 total=0 gp_domain idx=0 limit active=0 pending record
	shift
	shift
	strategy="$*"
	limit="$(gp_md_parallel_limit)"
	rm -f "${PARALLEL_OUT}_md_"*

	echo
	echo "- gp_multidomain_strategy ipv$IPV parallel=$limit : $PKTWSD ${WF:+$WF }$strategy"
	pktws_start "$@"
	for gp_domain in $DOMAINS; do
		idx=$(($idx + 1))
		total=$(($total + 1))
		gp_md_run_domain_curl "$idx" "$testf" "$gp_domain" &
		record="$!:$idx:$gp_domain"
		pending="${pending:+$pending }$record"
		active=$(($active + 1))
		if [ "$active" -ge "$limit" ]; then
			record="${pending%% *}"
			if [ "$record" = "$pending" ]; then
				pending=
			else
				pending="${pending#* }"
			fi
			gp_md_collect_record "$record" "$testf" "$strategy" && ok=$(($ok + 1))
			active=$(($active - 1))
		fi
	done
	while [ -n "$pending" ]; do
		record="${pending%% *}"
		if [ "$record" = "$pending" ]; then
			pending=
		else
			pending="${pending#* }"
		fi
		gp_md_collect_record "$record" "$testf" "$strategy" && ok=$(($ok + 1))
	done
	ws_kill
	rm -f "${PARALLEL_OUT}_md_"*
	echo "GP-MULTIDOMAIN result: $ok/$total domains available"
	[ "$ok" = "$total" ]
}

gp_md_run_protocol()
{
	# $1 - standard script function
	# $2 - curl test function
	# $3 - tcp/udp
	# $4 - port
	local func=$1 testf=$2 proto=$3 port=$4 ips primary
	primary="$(gp_md_primary_domain)"
	[ -n "$primary" ] || return 1
	ips="$(gp_md_resolve_all_ips)"
	ips="$(gp_md_normalize_ip_list "$ips")"
	[ -n "$ips" ] || {
		echo "GP-MULTIDOMAIN no resolved ip addresses for $proto/$port"
		return 1
	}

	echo
	echo "GP-MULTIDOMAIN preparing $PKTWSD redirection for $proto/$port"
	case "$proto" in
		tcp) pktws_ipt_prepare_tcp "$port" "$ips" ;;
		udp) pktws_ipt_prepare_udp "$port" "$ips" ;;
		*) return 1 ;;
	esac
	test_runner "$func" "$testf" "$primary"
	echo "GP-MULTIDOMAIN clearing $PKTWSD redirection for $proto/$port"
	case "$proto" in
		tcp) pktws_ipt_unprepare_tcp "$port" ;;
		udp) pktws_ipt_unprepare_udp "$port" ;;
	esac
}

fsleep_setup
fix_sbin_path
check_system
check_already
[ "$UNAME" != CYGWIN  -a "$SKIP_PKTWS" != 1 ] && require_root
check_prerequisites
trap sigint_cleanup INT
check_dns
check_virt
ask_params
trap - INT

PID=
NREPORT=
unset WF
trap sigint INT
trap sigsilent PIPE
trap sigsilent HUP
for IPV in $IPVS; do
	configure_ip_version
	[ "$ENABLE_HTTP" = 1 ] && gp_md_run_protocol pktws_check_http curl_test_http tcp "$HTTP_PORT"
	[ "$ENABLE_HTTPS_TLS12" = 1 ] && gp_md_run_protocol pktws_check_https_tls12 curl_test_https_tls12 tcp "$HTTPS_PORT"
	[ "$ENABLE_HTTPS_TLS13" = 1 ] && gp_md_run_protocol pktws_check_https_tls13 curl_test_https_tls13 tcp "$HTTPS_PORT"
	[ "$ENABLE_HTTP3" = 1 ] && gp_md_run_protocol pktws_check_http3 curl_test_http3 udp "$QUIC_PORT"
done
trap - HUP
trap - PIPE
trap - INT

cleanup

echo
echo \* SUMMARY
report_print
[ "$DOMAINS_COUNT" -gt 1 ] && {
	echo
	echo \* COMMON
	result_intersection_print
}
'''








def _finder_dir(state_dir: Path) -> Path:
    path = state_dir / "strategy-finder"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_old_strategy_logs(logs: Path) -> dict[str, int]:
    if not logs.is_dir():
        return {"removed_files": 0, "removed_bytes": 0}
    files: list[tuple[float, int, Path]] = []
    for path in logs.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if not any(name.endswith(suffix) for suffix in LOG_RETENTION_SUFFIXES):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((stat.st_mtime, int(stat.st_size), path))
    files.sort(key=lambda item: item[0], reverse=True)
    kept_count = 0
    kept_bytes = 0
    removed_files = 0
    removed_bytes = 0
    for _mtime, size, path in files:
        keep = kept_count < LOG_RETENTION_MAX_FILES and kept_bytes + size <= LOG_RETENTION_MAX_TOTAL_BYTES
        if keep:
            kept_count += 1
            kept_bytes += size
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed_files += 1
        removed_bytes += size
    return {"removed_files": removed_files, "removed_bytes": removed_bytes}




















def _storage_version(state_dir: Path) -> dict[str, int]:
    return _file_version(state_dir / "strategy-finder" / "state.sqlite3")


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    max_lines = max(0, max_lines)
    if max_lines <= 0 or not path.exists():
        return []
    block_size = 8192
    blocks: list[bytes] = []
    line_count = 0
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        while position > 0 and line_count <= max_lines:
            read_size = min(block_size, position)
            position -= read_size
            handle.seek(position)
            block = handle.read(read_size)
            blocks.append(block)
            line_count += block.count(b"\n")
    data = b"".join(reversed(blocks))
    return data.decode("utf-8", errors="replace").splitlines()[-max_lines:]


def _log_delta(path: Path | None, expected_path: str | None, from_size: int | None, max_bytes: int = 200_000) -> str | None:
    if path is None or from_size is None or not expected_path:
        return None
    if str(path) != str(expected_path):
        return None
    if from_size < 0 or not path.exists():
        return None
    current_size = path.stat().st_size
    if current_size < from_size:
        return None
    if current_size - from_size > max_bytes:
        return None
    with path.open("rb") as handle:
        handle.seek(from_size)
        return handle.read(current_size - from_size).decode("utf-8", errors="replace")


def _file_version(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"size": 0, "mtime_ns": 0}
    stat = path.stat()
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}









