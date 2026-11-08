#!/bin/sh
set -eu
PATH='/usr/sbin:/usr/bin:/sbin:/bin'
export PATH
readonly PATH

if [ "${1:-}" != core-update ]; then
  CONFIG_FILE="${GP_ROOT_HELPER_CONFIG:-/etc/default/gp-control-plane-root-helper}"
  [ -r "$CONFIG_FILE" ] && . "$CONFIG_FILE"
fi
ZAPRET_DIR="${ZAPRET_DIR:-/opt/zapret2}"
RUN_REGISTRY_DIR="${GP_ROOT_HELPER_RUN_DIR:-/run/gp-control-plane/runs}"
DISCOVERY_GATE_DIR='/run/gp-control-plane/gates'
DISCOVERY_GATE_FILE="$DISCOVERY_GATE_DIR/discovery-update.lock"

fail() {
  printf 'gp-root-helper: %s\n' "$1" >&2
  exit 126
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail "must be executed as root"
}

real_path() {
  readlink -f "$1" 2>/dev/null || printf '%s\n' "$1"
}

validate_signal() {
  case "$1" in
    TERM|KILL|INT|HUP) printf '%s\n' "$1" ;;
    *) fail "unsupported signal: $1" ;;
  esac
}

is_valid_pid() {
  case "$1" in
    ''|0|0*|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

is_valid_process_start_marker() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

validate_pid() {
  is_valid_pid "$1" || fail "invalid pid: $1"
  printf '%s\n' "$1"
}

validate_env_assignment() {
  case "$1" in
    *=*) ;;
    *) fail "invalid env assignment" ;;
  esac
  key="${1%%=*}"
  case "$key" in
    BATCH|DOMAINS|IPVS|TEST|SKIP_DNSCHECK|SKIP_IPBLOCK|ENABLE_HTTP|ENABLE_HTTPS_TLS12|ENABLE_HTTPS_TLS13|ENABLE_HTTP3|SCANLEVEL|REPEATS|PARALLEL|CURL_MAX_TIME|CURL_MAX_TIME_QUIC|CURL_MAX_TIME_DOH|GP_MD_CURL_PARALLELISM|GP_API2_TRACE|ZAPRET_BASE|ZAPRET_RW) ;;
    *) fail "unsupported env key: $key" ;;
  esac
  if [ "$key" = GP_API2_TRACE ]; then
    value="${1#*=}"
    case "$value" in ''|*[!a-f0-9]*) fail "invalid API 2 trace identity" ;; esac
    [ "${#value}" -eq 32 ] || fail "invalid API 2 trace identity"
  fi
}

validate_run_target() {
  [ "$#" -ge 1 ] || fail "run target is required"
  target="$(real_path "$1")"
  zapret_blockcheck="$(real_path "$ZAPRET_DIR/blockcheck2.sh")"
  case "$target" in
    "$zapret_blockcheck") ;;
    *) fail "unsupported run target: $1" ;;
  esac
  [ -x "$target" ] || fail "run target is not executable: $target"
  printf '%s\n' "$target"
}

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

run_target() {
  target="$(validate_run_target "$@")"
  shift
  exec "$target" "$@"
}

ensure_discovery_gate() {
  [ ! -L "$DISCOVERY_GATE_DIR" ] || fail "discovery gate directory must not be a symlink: $DISCOVERY_GATE_DIR"
  if [ ! -e "$DISCOVERY_GATE_DIR" ]; then
    install -d -m 0700 -o root -g root "$DISCOVERY_GATE_DIR"
  fi
  [ -d "$DISCOVERY_GATE_DIR" ] && [ ! -L "$DISCOVERY_GATE_DIR" ] || fail "discovery gate directory is unsafe: $DISCOVERY_GATE_DIR"
  [ "$(stat -c '%u:%g:%a' "$DISCOVERY_GATE_DIR" 2>/dev/null || true)" = '0:0:700' ] || fail "discovery gate directory must be root:root mode 0700: $DISCOVERY_GATE_DIR"
  if [ ! -e "$DISCOVERY_GATE_FILE" ] && [ ! -L "$DISCOVERY_GATE_FILE" ]; then
    umask 077
    : > "$DISCOVERY_GATE_FILE" || fail "cannot create discovery gate: $DISCOVERY_GATE_FILE"
    chown root:root "$DISCOVERY_GATE_FILE" || fail "cannot own discovery gate: $DISCOVERY_GATE_FILE"
    chmod 0600 "$DISCOVERY_GATE_FILE" || fail "cannot protect discovery gate: $DISCOVERY_GATE_FILE"
  fi
  [ -f "$DISCOVERY_GATE_FILE" ] && [ ! -L "$DISCOVERY_GATE_FILE" ] || fail "discovery gate must be a regular non-symlink file: $DISCOVERY_GATE_FILE"
  [ "$(stat -c '%u:%g:%a' "$DISCOVERY_GATE_FILE" 2>/dev/null || true)" = '0:0:600' ] || fail "discovery gate must be root:root mode 0600: $DISCOVERY_GATE_FILE"
}

with_discovery_gate() {
  ensure_discovery_gate
  command -v flock >/dev/null 2>&1 || fail "flock is required for discovery gate"
  # The FD is intentionally inherited by the privileged target so the shared
  # lock covers every blockcheck child, including exec-based entrypoints.
  exec 9<>"$DISCOVERY_GATE_FILE"
  flock -n -s 9 || {
    printf 'gp-root-helper: discovery blocked by active maintenance gate\n' >&2
    return 75
  }
  "$@"
}

with_recovery_gate() {
  ensure_discovery_gate
  command -v flock >/dev/null 2>&1 || fail "flock is required for recovery gate"
  exec 9<>"$DISCOVERY_GATE_FILE"
  flock -n -x 9 || {
    printf 'gp-root-helper: recovery blocked by active discovery or maintenance gate\n' >&2
    return 75
  }
  "$@"
}

# A managed launcher and signal-run may both need to inspect the same
# lifecycle directory while the launcher is exiting.  Serialize that narrow
# transition on a root-owned file inside the directory: the discovery gate is
# deliberately shared by running discovery commands and cannot provide this
# exclusion.
with_run_lifecycle_gate() {
  lifecycle_gate="$1"
  shift
  [ -f "$lifecycle_gate" ] && [ ! -L "$lifecycle_gate" ] || return 2
  [ "$(stat -c '%u:%g:%a' "$lifecycle_gate" 2>/dev/null || true)" = '0:0:600' ] || return 2
  command -v flock >/dev/null 2>&1 || return 2
  exec 8<>"$lifecycle_gate"
  flock -x 8 || return 2
  "$@"
  lifecycle_status="$?"
  flock -u 8 || return 2
  return "$lifecycle_status"
}

write_owned_run_record() {
  run_id="$(validate_run_id "$1")"
  pid="$(validate_pid "$2")"
  pgid="$(validate_pid "$3")"
  marker="$4"
  is_valid_process_start_marker "$marker" || fail "invalid process start marker"
  ensure_run_registry
  record="$(registry_record_path "$run_id")"
  umask 077
  tmp_record="$(mktemp "$RUN_REGISTRY_DIR/.${run_id}.XXXXXX")" || return 1
  if ! printf 'helper-v1 %s %s %s\n' "$pid" "$pgid" "$marker" > "$tmp_record" ||
    ! chown root:root "$tmp_record" ||
    ! chmod 0600 "$tmp_record" ||
    ! mv -f "$tmp_record" "$record"; then
    rm -f "$tmp_record"
    return 1
  fi
}

read_owned_run_ready() {
  ready_file="$1"
  [ -e "$ready_file" ] || return 1
  [ -f "$ready_file" ] && [ ! -L "$ready_file" ] || return 2
  ready_contents="$(cat "$ready_file")" || return 2
  case "$ready_contents" in
    'helper-ready-v1 '*) ;;
    *) return 2 ;;
  esac
  ready_pid="${ready_contents#helper-ready-v1 }"
  [ "$ready_contents" = "helper-ready-v1 $ready_pid" ] || return 2
  case "$ready_pid" in
    ''|0|0*|*[!0-9]*) return 2 ;;
  esac
  printf '%s\n' "$ready_pid"
}

write_owned_run_attestation() {
  ready_file="$1"
  attested_pid="$(validate_pid "$2")"
  attested_pgid="$(validate_pid "$3")"
  attested_marker="$4"
  [ "$attested_pid" = "$attested_pgid" ] || return 1
  is_valid_process_start_marker "$attested_marker" || return 1
  umask 077
  tmp_ready="$(mktemp "${ready_file}.XXXXXX")" || return 1
  if ! printf 'helper-ready-v2 %s %s %s\n' "$attested_pid" "$attested_pgid" "$attested_marker" > "$tmp_ready" ||
    ! chown root:root "$tmp_ready" ||
    ! chmod 0600 "$tmp_ready" ||
    ! mv -f "$tmp_ready" "$ready_file"; then
    rm -f "$tmp_ready"
    return 1
  fi
}

read_owned_run_attestation() {
  ready_file="$1"
  [ -e "$ready_file" ] || return 1
  [ -f "$ready_file" ] && [ ! -L "$ready_file" ] || return 2
  [ "$(stat -c '%u:%g:%a' "$ready_file" 2>/dev/null || true)" = '0:0:600' ] || return 2
  ready_contents="$(cat "$ready_file")" || return 2
  IFS=' ' read -r ready_version ready_pid ready_pgid ready_marker ready_extra <<EOF
$ready_contents
EOF
  [ "$ready_version" = helper-ready-v2 ] || return 2
  [ -n "${ready_pid:-}" ] && [ -n "${ready_pgid:-}" ] && [ -n "${ready_marker:-}" ] || return 2
  [ -z "${ready_extra:-}" ] || return 2
  [ "$ready_contents" = "helper-ready-v2 $ready_pid $ready_pgid $ready_marker" ] || return 2
  is_valid_pid "$ready_pid" && is_valid_pid "$ready_pgid" && is_valid_process_start_marker "$ready_marker" || return 2
  [ "$ready_pid" = "$ready_pgid" ] || return 2
  printf '%s %s %s\n' "$ready_pid" "$ready_pgid" "$ready_marker"
}

write_owned_run_go() {
  go_file="$1"
  go_pid="$(validate_pid "$2")"
  umask 077
  tmp_go="$(mktemp "${go_file}.XXXXXX")" || return 1
  if ! printf 'helper-go-v1 %s\n' "$go_pid" > "$tmp_go" ||
    ! chown root:root "$tmp_go" ||
    ! chmod 0600 "$tmp_go" ||
    ! mv -f "$tmp_go" "$go_file"; then
    rm -f "$tmp_go"
    return 1
  fi
}

write_owned_run_signal_delivery() {
  signal_file="$1"
  delivered_signal="$(validate_signal "$2")"
  delivered_pid="$(validate_pid "$3")"
  delivered_pgid="$(validate_pid "$4")"
  delivered_marker="$5"
  [ "$delivered_pid" = "$delivered_pgid" ] || return 1
  is_valid_process_start_marker "$delivered_marker" || return 1
  umask 077
  tmp_signal="$(mktemp "${signal_file}.XXXXXX")" || return 1
  if ! printf 'helper-signal-v1 %s %s %s %s\n' "$delivered_signal" "$delivered_pid" "$delivered_pgid" "$delivered_marker" > "$tmp_signal" ||
    ! chown root:root "$tmp_signal" ||
    ! chmod 0600 "$tmp_signal" ||
    ! mv -f "$tmp_signal" "$signal_file"; then
    rm -f "$tmp_signal"
    return 1
  fi
}

write_owned_invalid_status_quarantine() {
  quarantine_file="$1"
  umask 077
  tmp_quarantine="$(mktemp "${quarantine_file}.XXXXXX")" || return 1
  if ! printf 'helper-invalid-status-v1\n' > "$tmp_quarantine" ||
    ! chown root:root "$tmp_quarantine" ||
    ! chmod 0600 "$tmp_quarantine" ||
    ! mv -f "$tmp_quarantine" "$quarantine_file"; then
    rm -f "$tmp_quarantine"
    return 1
  fi
}

wait_for_owned_run_ready() {
  ready_file="$1"
  expected_pid="$(validate_pid "$2")"
  ready_waited=0
  # Keep the ten-second readiness budget, but do not add a full second to
  # each half of the ready/go handshake for an already prepared supervisor.
  while [ "$ready_waited" -lt 100 ]; do
    if ready_pid="$(read_owned_run_ready "$ready_file")"; then
      [ "$ready_pid" = "$expected_pid" ] || return 2
      # Keep the result in the owning shell.  The caller's TERM trap must be
      # live while this pre-record wait is in progress; command substitution
      # would move the wait into a subshell that can exit before the trap
      # reaps the just-created supervisor.
      owned_ready_pid="$ready_pid"
      return 0
    else
      ready_result="$?"
    fi
    [ "$ready_result" -eq 1 ] || return 2
    if ! kill -0 "$expected_pid" 2>/dev/null; then
      set +e
      wait "$expected_pid" 2>/dev/null
      set -e
      return 1
    fi
    sleep 0.1
    ready_waited=$((ready_waited + 1))
  done
  return 3
}

read_owned_run_status() {
  status_file="$1"
  # Test symlinks before `-e`: a dangling link is still hostile status
  # publication, not an absent status that could be mistaken for a normal
  # supervisor exit.
  [ ! -L "$status_file" ] || return 2
  [ -e "$status_file" ] || return 1
  [ -f "$status_file" ] || return 2
  [ "$(stat -c '%u:%g:%a' "$status_file" 2>/dev/null || true)" = '0:0:600' ] || return 2
  status_contents="$(cat "$status_file")" || return 2
  case "$status_contents" in
    'helper-status-v1 '*) ;;
    *) return 2 ;;
  esac
  status_code="${status_contents#helper-status-v1 }"
  [ "$status_contents" = "helper-status-v1 $status_code" ] || return 2
  case "$status_code" in
    [0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5]) printf '%s\n' "$status_code" ;;
    *) return 2 ;;
  esac
}

wait_for_owned_run_status() {
  status_file="$1"
  known_pid="$2"
  known_pgid="$3"
  known_marker="$4"
  while :; do
    if status_code="$(read_owned_run_status "$status_file")"; then
      printf '%s\n' "$status_code"
      return 0
    else
      status_result="$?"
    fi
    [ "$status_result" -eq 1 ] || return 2
    # A killed supervisor can remain as this shell's unreaped zombie.  Its
    # /proc identity still matches, but the attested process group is already
    # empty.  Treat that as a terminal missing-status result so the caller can
    # wait(1) for and reap its child instead of polling the zombie forever.
    if known_process_group_is_empty "$known_pid" "$known_pgid"; then
      return 1
    else
      group_status="$?"
      [ "$group_status" -eq 1 ] || return 3
    fi
    if managed_process_matches "$known_pid" "$known_pgid" "$known_marker"; then
      :
    else
      managed_status="$?"
      [ "$managed_status" -eq 1 ] && return 1
      return 3
    fi
    sleep 1
  done
}

run_owned_process() {
  run_id="$(validate_run_id "$1")"
  shift
  owned_cleanup_dir=""
  if [ "${1:-}" = --cleanup-dir ]; then
    [ "$#" -ge 3 ] || fail "run-owned cleanup directory requires a target"
    owned_cleanup_dir="$2"
    shift 2
    case "$owned_cleanup_dir" in
      "${TMPDIR:-/tmp}"/gp-root-helper.*) ;;
      *) fail "unsupported owned cleanup directory" ;;
    esac
  fi
  target="$1"
  shift
  lock_dir="$RUN_REGISTRY_DIR/.${run_id}.lock"
  record="$(registry_record_path "$run_id")"
  ready_file="$lock_dir/supervisor-ready"
  go_file="$lock_dir/supervisor-go"
  status_file="$lock_dir/target-status"
  invalid_status_file="$lock_dir/target-status-invalid"
  lifecycle_gate="$lock_dir/signal-gate"
  signal_file="$lock_dir/signal-delivery"
  lock_created=0
  supervisor_started=0
  supervisor_attested=0
  abort_in_progress=0
  cleanup_context_done=0
  pid=""
  pgid=""
  marker=""
  remove_unattested_run_lock() {
    [ "$lock_created" = 1 ] || return 0
    [ "$supervisor_started" = 0 ] || return 1
    rm -f -- "$ready_file" "$go_file" "$lifecycle_gate" "$signal_file"
    rmdir -- "$lock_dir" 2>/dev/null || return 1
    lock_created=0
  }
  remove_owned_run_artifacts() {
    [ "$supervisor_attested" = 1 ] || return 1
    if known_process_group_is_empty "$pid" "$pgid"; then
      :
    else
      return "$?"
    fi
    set +e
    wait "$pid" 2>/dev/null
    set -e
    if known_process_group_is_empty "$pid" "$pgid"; then
      :
    else
      return "$?"
    fi
    if managed_process_is_gone "$pid"; then
      :
    else
      return "$?"
    fi
    if [ -e "$signal_file" ] || [ -L "$signal_file" ]; then
      terminal_dir="$(recovery_terminal_path "$run_id")"
      [ ! -e "$terminal_dir" ] && [ ! -L "$terminal_dir" ] || return 1
      mv -- "$lock_dir" "$terminal_dir" || return 1
      rm -f -- "$record" || return 1
    else
      rm -f -- "$record" "$ready_file" "$go_file" "$status_file" "$invalid_status_file" "$lifecycle_gate"
      rmdir -- "$lock_dir" 2>/dev/null || return 1
    fi
    supervisor_attested=0
    lock_created=0
  }
  stop_unattested_supervisor() {
    [ "$supervisor_started" = 1 ] || return 0
    kill -TERM "$pid" 2>/dev/null || true
    set +e
    wait "$pid" 2>/dev/null
    set -e
    if managed_process_is_gone "$pid"; then
      :
    else
      return "$?"
    fi
    supervisor_started=0
  }
  cleanup_owned_run_locked() {
    if [ "$supervisor_attested" = 1 ]; then
      if terminate_known_process_group "$pid" "$pgid" "$marker" TERM; then
        remove_owned_run_artifacts
      else
        cleanup_status="$?"
        if known_process_group_is_empty "$pid" "$pgid" && managed_process_is_gone "$pid"; then
          remove_owned_run_artifacts || return "$cleanup_status"
          return 0
        fi
        return "$cleanup_status"
      fi
    elif [ "$supervisor_started" = 1 ]; then
      stop_unattested_supervisor || return 1
      remove_unattested_run_lock
    else
      remove_unattested_run_lock
    fi
  }
  cleanup_owned_run() {
    # Before the v2-ready/record attestation there is no public operation
    # that may safely act on this run.  The current root helper is the only
    # owner of its just-created lock, so a TERM trap must not depend on a
    # second flock acquisition while it is unwinding an absent-ready launch.
    # Once attested, keep the existing gate serialization for every group
    # signal and artifact transition.
    if [ "$supervisor_attested" != 1 ]; then
      cleanup_owned_run_locked
    elif [ "$lock_created" = 1 ]; then
      with_run_lifecycle_gate "$lifecycle_gate" cleanup_owned_run_locked
    else
      cleanup_owned_run_locked
    fi
  }
  quarantine_invalid_owned_lifecycle() {
    [ "$supervisor_attested" = 1 ] || return 1
    write_owned_invalid_status_quarantine "$invalid_status_file" || return 1
    terminate_known_process_group "$pid" "$pgid" "$marker" TERM || return 1
    set +e; wait "$pid" 2>/dev/null; set -e
    known_process_group_is_empty "$pid" "$pgid" && managed_process_is_gone "$pid" || return 1
    # The invalid root-owned lifecycle remains for fail-closed recovery.
    supervisor_attested=0
    supervisor_started=0
    lock_created=0
  }
  quarantine_invalid_owned_lifecycle_locked() {
    quarantine_invalid_owned_lifecycle
  }
  cleanup_owned_lifecycle() {
    if cleanup_owned_run; then
      cleanup_status=0
    else
      cleanup_status="$?"
    fi
    if [ -n "$owned_cleanup_dir" ] && [ "$cleanup_context_done" = 0 ]; then
      cleanup_context_done=1
      if [ -e "$owned_cleanup_dir" ] || [ -L "$owned_cleanup_dir" ]; then
        [ -d "$owned_cleanup_dir" ] && [ ! -L "$owned_cleanup_dir" ] || return 126
        rm -rf -- "$owned_cleanup_dir" || return 126
      fi
    fi
    return "$cleanup_status"
  }
  abort_owned_run() {
    abort_status="$1"
    [ "$abort_in_progress" = 0 ] || exit "$abort_status"
    abort_in_progress=1
    trap '' HUP INT TERM
    cleanup_owned_lifecycle || true
    trap - EXIT HUP INT TERM
    exit "$abort_status"
  }
  trap cleanup_owned_lifecycle EXIT
  trap 'abort_owned_run 129' HUP
  trap 'abort_owned_run 130' INT
  trap 'abort_owned_run 143' TERM
  ensure_run_registry
  umask 077
  mkdir "$lock_dir" 2>/dev/null || fail "run is already active"
  lock_created=1
  if ! : > "$lifecycle_gate" || ! chown root:root "$lifecycle_gate" || ! chmod 0600 "$lifecycle_gate"; then
    fail "cannot create managed run lifecycle gate"
  fi
  if registered_process_matches "$run_id" >/dev/null 2>&1; then
    fail "run is already active"
  else
    registered_status="$?"
    [ "$registered_status" -eq 1 ] || fail "registered process cannot be safely inspected"
  fi
  [ ! -e "$record" ] && [ ! -L "$record" ] || fail "run record exists; recover it before starting a new run"
  command -v setsid >/dev/null 2>&1 || fail "setsid is required for managed runs"
  exec 7<&0
  setsid /bin/sh -c '
    lock_dir="$1"
    ready_file="$2"
    go_file="$3"
    status_file="$4"
    lifecycle_gate="$5"
    record="$6"
    shift 6
    trap - HUP INT TERM
    umask 077
    tmp_ready="$(mktemp "${ready_file}.XXXXXX")" || exit 125
    if ! printf "helper-ready-v1 %s\\n" "$$" > "$tmp_ready" ||
      ! chown root:root "$tmp_ready" ||
      ! chmod 0600 "$tmp_ready" ||
      ! mv -f "$tmp_ready" "$ready_file"; then
      rm -f "$tmp_ready"
      exit 125
    fi
    while :; do
      [ -d "$lock_dir" ] || exit 125
      if [ -e "$go_file" ]; then
        [ -f "$go_file" ] && [ ! -L "$go_file" ] || exit 125
        go_contents="$(cat "$go_file")" || exit 125
        [ "$go_contents" = "helper-go-v1 $$" ] || exit 125
        break
      fi
      sleep 0.1
    done
    ( trap - HUP INT TERM; exec "$@" <&7 7<&- ) &
    target_pid="$!"
    set +e
    wait "$target_pid"
    target_code="$?"
    set -e
    # Status publication and signal-run terminal transition share this exact
    # root-owned gate.  The writer never holds another lifecycle lock, so the
    # ordering is gate-only: signal-run validates -> TERM -> receipt/record
    # transition, while a reaped target validates -> publish.  If signal-run
    # won, its receipt/move (or missing lock/record) suppresses this write.
    [ -d "$lock_dir" ] && [ ! -L "$lock_dir" ] || exit 125
    [ "$(stat -c "%u:%g:%a" "$lock_dir" 2>/dev/null || true)" = "0:0:700" ] || exit 125
    for owned_file in "$ready_file" "$go_file" "$lifecycle_gate" "$record"; do
      [ -f "$owned_file" ] && [ ! -L "$owned_file" ] || exit 125
      [ "$(stat -c "%u:%g:%a" "$owned_file" 2>/dev/null || true)" = "0:0:600" ] || exit 125
    done
    exec 8<>"$lifecycle_gate" || exit 125
    flock -x 8 || exit 125
    [ -d "$lock_dir" ] && [ ! -L "$lock_dir" ] || exit 125
    [ "$(stat -c "%u:%g:%a" "$lock_dir" 2>/dev/null || true)" = "0:0:700" ] || exit 125
    for owned_file in "$ready_file" "$go_file" "$lifecycle_gate" "$record"; do
      [ -f "$owned_file" ] && [ ! -L "$owned_file" ] || exit 125
      [ "$(stat -c "%u:%g:%a" "$owned_file" 2>/dev/null || true)" = "0:0:600" ] || exit 125
    done
    ready_contents="$(cat "$ready_file")" || exit 125
    case "$ready_contents" in "helper-ready-v2 $$ $$ "*) ready_marker="${ready_contents#helper-ready-v2 $$ $$ }" ;; *) exit 125 ;; esac
    case "$ready_marker" in ""|*[!0-9]*) exit 125 ;; esac
    [ "$ready_contents" = "helper-ready-v2 $$ $$ $ready_marker" ] || exit 125
    [ "$(cat "$go_file")" = "helper-go-v1 $$" ] || exit 125
    [ "$(cat "$record")" = "helper-v1 $$ $$ $ready_marker" ] || exit 125
    [ ! -e "$status_file" ] && [ ! -L "$status_file" ] || exit 125
    [ ! -e "$lock_dir/signal-delivery" ] && [ ! -L "$lock_dir/signal-delivery" ] || exit 125
    umask 077
    tmp_status="$(mktemp "${status_file}.XXXXXX")" || exit 125
    if ! printf "helper-status-v1 %s\\n" "$target_code" > "$tmp_status" ||
      ! chown root:root "$tmp_status" ||
      ! chmod 0600 "$tmp_status" ||
      ! mv -f "$tmp_status" "$status_file"; then
      rm -f "$tmp_status"
      exit 125
    fi
    # Do not retain the lifecycle gate while the supervisor waits for the
    # owner to consume this immutable status.  signal-run/recovery must be
    # able to take it for their terminal transition or cleanup.
    flock -u 8 || exit 125
    trap "" HUP INT TERM
    while :; do
      sleep 2147483647 &
      wait "$!"
    done
  ' gp-owned-supervisor "$lock_dir" "$ready_file" "$go_file" "$status_file" "$lifecycle_gate" "$record" "$target" "$@" &
  pid="$!"
  supervisor_started=1
  if wait_for_owned_run_ready "$ready_file" "$pid"; then
    ready_pid="$owned_ready_pid"
  else
    ready_result="$?"
    case "$ready_result" in
      2) fail "managed supervisor ready file is invalid" ;;
      3) fail "managed supervisor did not become ready" ;;
      *) fail "managed supervisor exited before ready" ;;
    esac
  fi
  marker="$(process_start_time "$pid" 2>/dev/null || true)"
  pgid="$(process_group_id "$pid" 2>/dev/null || true)"
  session="$(process_session_id "$pid" 2>/dev/null || true)"
  if [ -z "$marker" ] || [ -z "$pgid" ] || [ "$pgid" != "$pid" ] || [ "$session" != "$pid" ] ||
    ! managed_process_matches "$pid" "$pgid" "$marker"; then
    fail "managed process exited before registration"
  fi
  supervisor_attested=1
  if ! write_owned_run_attestation "$ready_file" "$pid" "$pgid" "$marker"; then
    abort_owned_run 126
  fi
  if ! write_owned_run_record "$run_id" "$pid" "$pgid" "$marker"; then
    abort_owned_run 126
  fi
  if ! write_owned_run_go "$go_file" "$pid"; then
    abort_owned_run 126
  fi
  if code="$(wait_for_owned_run_status "$status_file" "$pid" "$pgid" "$marker")"; then
    :
  else
    status_result="$?"
    trap - EXIT HUP INT TERM
    if [ "$status_result" -eq 2 ]; then
      with_run_lifecycle_gate "$lifecycle_gate" quarantine_invalid_owned_lifecycle_locked || fail "managed target status is invalid and cannot be quarantined"
      fail "managed target status is invalid"
    fi
    if [ "$status_result" -eq 3 ]; then
      fail "managed target status is unavailable and process identity is indeterminate"
    fi
    cleanup_owned_lifecycle || true
    fail "managed supervisor exited before target status"
  fi
  if cleanup_owned_lifecycle; then
    :
  else
    cleanup_status="$?"
    trap - EXIT HUP INT TERM
    fail "managed process group could not be safely cleaned up (status $cleanup_status)"
  fi
  trap - EXIT HUP INT TERM
  if [ -n "${GP_API2_TRACE:-}" ]; then
    printf 'GP-API2 %s process-clean\n' "$GP_API2_TRACE"
  fi
  return "$code"
}

run_owned_target() {
  [ "$#" -ge 2 ] || fail "run-owned requires run id and target"
  run_id="$(validate_run_id "$1")"
  shift
  target="$(validate_run_target "$@")"
  shift
  if [ -n "${GP_API2_TRACE:-}" ]; then
    run_api2_stock_target "$run_id" "$target" "$@"
  else
    run_owned_process "$run_id" "$target" "$@"
  fi
}

write_api2_trace() {
  cat <<'TRACE'

gp_api2_begin()
{
  gp_api2_test="$1"
  shift
  gp_api2_words="$(printf '%s\0' "--lua-init=@$ZAPRET_BASE/lua/zapret-lib.lua" "--lua-init=@$ZAPRET_BASE/lua/zapret-antidpi.lua" "$@" | base64 -w0)"
  gp_api2_digest="$(printf '%s\n%s\n%s\n' "$gp_api2_test" "$IPV" "$gp_api2_words" | sha256sum)"
  gp_api2_digest="${gp_api2_digest%% *}"
}

gp_api2_record_runtime()
{
  if [ "$FWTYPE" = nftables ]; then
    printf 'GP-API2 %s table %s\n' "$GP_API2_TRACE" "$NFT_TABLE"
  fi
}

gp_api2_record_start()
{
  printf 'GP-API2 %s begin %s %s %s\n' "$GP_API2_TRACE" "$gp_api2_digest" "$gp_api2_test" "$gp_api2_words"
}

gp_api2_record_domain()
{
  printf 'GP-API2 %s domain %s %s\n' "$GP_API2_TRACE" "$1" "$2"
}

gp_api2_record_end()
{
  printf 'GP-API2 %s end\n' "$GP_API2_TRACE"
}
TRACE
}

run_api2_stock_target() (
  run_id="$1"; source="$2"; shift 2
  stage="$(mktemp -d /tmp/gp-api2-stock.XXXXXX)" || exit 126
  trap 'rm -f -- "$stage/runner"; rmdir -- "$stage"' EXIT
  runner="$stage/runner"
  awk '
    $0 == "fsleep_setup" { found=1; exit }
    $0 == "pktws_start()" { print "gp_stock_pktws_start()"; starts++; next }
    { print }
    END { if (!found || starts != 1) exit 42 }
  ' "$source" >"$runner" || exit 126
  write_api2_trace >>"$runner" || exit 126
  cat >>"$runner" <<'STOCK' || exit 126
pktws_start()
{
  if [ -n "${gp_stock_test:-}" ]; then
    gp_api2_begin "$gp_stock_test" "$@"
    gp_api2_record_start
    gp_stock_recorded=1
  fi
  gp_stock_pktws_start "$@"
}

pktws_curl_test_update()
{
  local gp_stock_test="$1" gp_domain="$2" gp_code gp_stock_recorded=0
  xxxws_curl_test_update pktws_curl_test "$@"
  gp_code="$?"
  if [ "$gp_stock_recorded" = 1 ]; then
    gp_api2_record_domain "$gp_domain" "$gp_code"
    gp_api2_record_end
  fi
  return "$gp_code"
}
STOCK
  awk '
    $0 == "fsleep_setup" { found=1 }
    found { print; if ($0 == "ask_params") { print "gp_api2_record_runtime"; observed++ } }
    END { if (!found || observed != 1) exit 42 }
  ' "$source" >>"$runner" || exit 126
  chmod 0700 "$runner" || exit 126
  ( run_owned_process "$run_id" "$runner" "$@" )
)

write_multidomain_runner() {
  source="$1"
  runner="$2"
  if ! awk '
    $0 == "fsleep_setup" { found=1; exit }
    { print }
    END { if (!found) exit 42 }
  ' "$source" > "$runner"; then
    printf 'gp-root-helper: unsupported blockcheck2.sh layout: main marker not found\n' >&2
    return 126
  fi
  write_api2_trace >> "$runner" || return 126
  cat >> "$runner" <<'RUNNER' || return 126

if [ -n "${GP_API2_TRACE:-}" ]; then
  exec 7<&0
  exec </dev/null
fi

gp_api2_replay()
{
  [ "${gp_api2_replayed:-0}" = 0 ] || return 2
  IFS=' ' read -r gp_saved_digest gp_saved_success gp_extra <&7 || {
    echo 'API 2 replay ended without terminator' >&2
    cleanup
    exit 125
  }
  if [ "$gp_saved_digest" = end ] && [ -z "$gp_saved_success$gp_extra" ]; then
    gp_api2_replayed=1
    return 2
  fi
  if [ "$gp_saved_digest" != "$gp_api2_digest" ] || [ -n "$gp_extra" ]; then
    echo 'API 2 replay position is incompatible' >&2
    cleanup
    exit 125
  fi
  case "$gp_saved_success" in
    1) return 0 ;;
    0) return 1 ;;
    *) cleanup; exit 125 ;;
  esac
}

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

gp_md_repeat_width()
{
	local width
	width="$(gp_md_parallel_limit)"
	[ "$width" -le "$REPEATS" ] || width="$REPEATS"
	printf '%s\n' "$width"
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
	local idx=$1 testf=$2 gp_domain="$3" code out codefile remaining width chunk
	out="$(gp_md_out_file "$idx")"
	codefile="$(gp_md_code_file "$idx")"
	if [ -n "${GP_API2_TRACE:-}" ] && [ "${PARALLEL:-0}" = 1 ]; then
		# Upstream curl_test deletes every file under its repeat prefix.
		# Keep each domain's repeats separate from other domains and their result files.
		local PARALLEL_OUT="${PARALLEL_OUT}_domain_${idx}_"
		remaining="$REPEATS"
		width="$(gp_md_repeat_width)"
		code=0
		while [ "$remaining" -gt 0 ]; do
			chunk="$width"
			[ "$chunk" -le "$remaining" ] || chunk="$remaining"
			(REPEATS="$chunk"; curl_test "$testf" "$gp_domain") || code=$?
			remaining=$((remaining - chunk))
		done >"$out" 2>&1
	else
		curl_test "$testf" "$gp_domain" >"$out" 2>&1
		code=$?
	fi
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

	if [ -n "${GP_API2_TRACE:-}" ]; then
		gp_api2_record_domain "$gp_domain" "$code"
	fi
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
	local testf=$1 dom="$2" strategy ok=0 total=0 gp_domain idx=0 limit active=0 pending record gp_replay_status
	shift
	shift
	if [ -n "${GP_API2_TRACE:-}" ]; then
		gp_api2_begin "$testf" "$@"
		gp_api2_replay
		gp_replay_status="$?"
		[ "$gp_replay_status" = 2 ] || return "$gp_replay_status"
		gp_api2_record_start
	fi
	strategy="$*"
	limit="$(gp_md_parallel_limit)"
	if [ -n "${GP_API2_TRACE:-}" ] && [ "${PARALLEL:-0}" = 1 ]; then
		# The domain workers share the API limit with their parallel repeats.
		limit=$((limit / $(gp_md_repeat_width)))
	fi
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
	if [ -n "${GP_API2_TRACE:-}" ]; then
		gp_api2_record_end
	fi
	[ "$ok" = "$total" ]
}

gp_md_run_protocol()
{
	# $1 - standard script function
	# $2 - curl test function
	# $3 - tcp/udp
	# $4 - port
	local func=$1 testf=$2 proto=$3 port=$4 ips primary gp_domain diagnostic_parallel
	primary="$(gp_md_primary_domain)"
	[ -n "$primary" ] || return 1
	ips="$(gp_md_resolve_all_ips)"
	ips="$(gp_md_normalize_ip_list "$ips")"
	[ -n "$ips" ] || {
		echo "GP-MULTIDOMAIN no resolved ip addresses for $proto/$port"
		return 1
	}

	echo
	if [ -n "${GP_API2_TRACE:-}" ] && [ "$proto" = tcp ] && [ "${SKIP_IPBLOCK:-0}" != 1 ]; then
		diagnostic_parallel="${PARALLEL:-0}"
		if [ "$diagnostic_parallel" = 1 ] && [ "$REPEATS" -gt "$(gp_md_parallel_limit)" ]; then
			# Upstream diagnostic curl_test runs outside the strategy worker pool.
			diagnostic_parallel=0
		fi
		for gp_domain in $DOMAINS; do
			check_domain_port_block "$gp_domain" "$port"
			(PARALLEL="$diagnostic_parallel"; check_dpi_ip_block "$testf" "$gp_domain")
		done
	fi
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
if [ -n "${GP_API2_TRACE:-}" ]; then
  gp_api2_record_runtime
fi
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

if [ -n "${GP_API2_TRACE:-}" ] && [ "${gp_api2_replayed:-0}" = 0 ]; then
  IFS= read -r gp_last <&7
  if [ "$gp_last" != end ]; then
    echo 'API 2 replay contains an unreached position' >&2
    cleanup
    exit 125
  fi
fi
cleanup
RUNNER
  chmod 0700 "$runner" || return 126
}

run_multidomain_target() (
  target="$(validate_run_target "$@")"
  shift
  tmp_dir=""
  runner="$tmp_dir/gp-multidomain-blockcheck.sh"
  cleanup_runner() {
    [ -n "${tmp_dir:-}" ] || return 0
    rm -rf -- "$tmp_dir"
    tmp_dir=""
  }
  abort_multidomain_run() {
    abort_status="$1"
    trap '' HUP INT TERM
    cleanup_runner || true
    trap - EXIT HUP INT TERM
    exit "$abort_status"
  }
  trap cleanup_runner EXIT
  trap 'abort_multidomain_run 129' HUP
  trap 'abort_multidomain_run 130' INT
  trap 'abort_multidomain_run 143' TERM
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/gp-root-helper.XXXXXX")"
  runner="$tmp_dir/gp-multidomain-blockcheck.sh"
  write_multidomain_runner "$target" "$runner"
  set +e
  "$runner" "$@"
  code="$?"
  set -e
  exit "$code"
)

run_owned_multidomain_target() (
  [ "$#" -ge 2 ] || fail "run-multidomain-owned requires run id and target"
  run_id="$(validate_run_id "$1")"
  shift
  target="$(validate_run_target "$@")"
  shift
  tmp_dir=""
  cleanup_runner() {
    [ -n "${tmp_dir:-}" ] || return 0
    rm -rf -- "$tmp_dir"
    tmp_dir=""
  }
  abort_multidomain_owned_run() {
    abort_status="$1"
    trap '' HUP INT TERM
    cleanup_runner || true
    trap - EXIT HUP INT TERM
    exit "$abort_status"
  }
  trap cleanup_runner EXIT
  trap 'abort_multidomain_owned_run 129' HUP
  trap 'abort_multidomain_owned_run 130' INT
  trap 'abort_multidomain_owned_run 143' TERM
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/gp-root-helper.XXXXXX")"
  runner="$tmp_dir/gp-multidomain-blockcheck.sh"
  write_multidomain_runner "$target" "$runner"
  set +e
  # run_owned_process owns lifecycle traps for its supervisor.  Keep that
  # trap scope in a child shell so it cannot replace this runner's cleanup
  # callback; this outer subshell remains the sole owner of tmp_dir.
  ( run_owned_process "$run_id" "$runner" "$@" )
  code="$?"
  set -e
  exit "$code"
)

validate_run_id() {
  case "${1:-}" in
    ""|*[!A-Za-z0-9._-]*|.*|*..*) fail "invalid run id" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

registry_record_path() {
  run_id="$(validate_run_id "${1:-}")"
  printf '%s/%s\n' "$RUN_REGISTRY_DIR" "$run_id"
}

ensure_run_registry() {
  [ ! -L "$RUN_REGISTRY_DIR" ] || fail "run registry must not be a symlink: $RUN_REGISTRY_DIR"
  if [ ! -e "$RUN_REGISTRY_DIR" ]; then
    install -d -m 0750 -o root -g root "$RUN_REGISTRY_DIR"
  fi
  [ -d "$RUN_REGISTRY_DIR" ] && [ ! -L "$RUN_REGISTRY_DIR" ] || fail "run registry must be a directory: $RUN_REGISTRY_DIR"
  [ "$(stat -c '%u:%g:%a' "$RUN_REGISTRY_DIR" 2>/dev/null || true)" = '0:0:750' ] || fail "run registry must be root:root mode 0750: $RUN_REGISTRY_DIR"
}

process_start_time_from_stat() {
  stat_file="${1:-}"
  [ -n "$stat_file" ] && [ -r "$stat_file" ] || return 2
  awk '
    NR != 1 { malformed = 1; exit }
    {
      separator = 0
      for (position = length($0) - 1; position >= 1; position--) {
        if (substr($0, position, 2) == ") ") {
          separator = position
          break
        }
      }
      if (!separator) {
        malformed = 1
        exit
      }
      stat_tail = substr($0, separator + 2)
      if (stat_tail !~ /^[A-Za-z] /) {
        malformed = 1
        exit
      }
      field_count = split(stat_tail, stat_fields, /[[:space:]]+/)
      if (field_count < 20 || stat_fields[20] !~ /^[0-9]+$/) {
        malformed = 1
        exit
      }
      print stat_fields[20]
    }
    END { if (NR != 1 || malformed) exit 2 }
  ' "$stat_file"
}

process_start_time() {
  pid="${1:-}"
  is_valid_pid "$pid" || return 2
  if [ ! -r "/proc/$pid/stat" ]; then
    [ ! -e "/proc/$pid" ] && [ ! -L "/proc/$pid" ] && return 1
    return 2
  fi
  if start_marker="$(process_start_time_from_stat "/proc/$pid/stat")"; then
    :
  else
    [ ! -e "/proc/$pid" ] && [ ! -L "/proc/$pid" ] && return 1
    return 2
  fi
  is_valid_process_start_marker "$start_marker" || return 2
  printf '%s\n' "$start_marker"
}

process_state() {
  pid="${1:-}"
  is_valid_pid "$pid" || return 2
  stat_file="/proc/$pid/stat"
  if [ ! -r "$stat_file" ]; then
    [ ! -e "/proc/$pid" ] && [ ! -L "/proc/$pid" ] && return 1
    return 2
  fi
  if process_state_value="$(awk '
    NR != 1 { malformed = 1; exit }
    {
      separator = 0
      for (position = length($0) - 1; position >= 1; position--) {
        if (substr($0, position, 2) == ") ") { separator = position; break }
      }
      if (!separator) { malformed = 1; exit }
      stat_tail = substr($0, separator + 2)
      if (stat_tail !~ /^[A-Za-z] /) { malformed = 1; exit }
      print substr(stat_tail, 1, 1)
    }
    END { exit malformed ? 2 : (NR == 1 ? 0 : 2) }
  ' "$stat_file" 2>/dev/null)"; then
    :
  else
    [ ! -e "/proc/$pid" ] && [ ! -L "/proc/$pid" ] && return 1
    return 2
  fi
  [ "${#process_state_value}" -eq 1 ] || return 2
  printf '%s\n' "$process_state_value"
}

process_group_id() {
  pid="${1:-}"
  is_valid_pid "$pid" || return 2
  process_group_listing="$(ps -o pgid= -p "$pid" 2>/dev/null)" || return 2
  set -- $process_group_listing
  [ "$#" -eq 0 ] && return 1
  [ "$#" -eq 1 ] || return 2
  is_valid_pid "$1" || return 2
  printf '%s\n' "$1"
}

process_session_id() {
  pid="${1:-}"
  is_valid_pid "$pid" || return 2
  process_session_listing="$(ps -o sid= -p "$pid" 2>/dev/null)" || return 2
  set -- $process_session_listing
  [ "$#" -eq 0 ] && return 1
  [ "$#" -eq 1 ] || return 2
  is_valid_pid "$1" || return 2
  printf '%s\n' "$1"
}

is_valid_ps_identifier() {
  case "$1" in
    0) return 0 ;;
    *) is_valid_pid "$1" ;;
  esac
}

classify_linux_ps_stat() {
  linux_ps_stat="${1:-}"
  [ -n "$linux_ps_stat" ] || return 2
  linux_ps_state="${linux_ps_stat%"${linux_ps_stat#?}"}"
  linux_ps_modifiers="${linux_ps_stat#?}"
  case "$linux_ps_state" in
    D|I|P|R|S|T|t|W|X|Z) ;;
    *) return 2 ;;
  esac

  # Linux ps documents modifiers in this order after the state letter:
  # < or N, L, s, l, then +.  Reject unknown, repeated, and reordered values.
  linux_ps_modifier_stage=0
  while [ -n "$linux_ps_modifiers" ]; do
    linux_ps_modifier="${linux_ps_modifiers%"${linux_ps_modifiers#?}"}"
    linux_ps_modifiers="${linux_ps_modifiers#?}"
    case "$linux_ps_modifier" in
      '<'|N)
        [ "$linux_ps_modifier_stage" -eq 0 ] || return 2
        linux_ps_modifier_stage=1
        ;;
      L)
        [ "$linux_ps_modifier_stage" -le 1 ] || return 2
        linux_ps_modifier_stage=2
        ;;
      s)
        [ "$linux_ps_modifier_stage" -le 2 ] || return 2
        linux_ps_modifier_stage=3
        ;;
      l)
        [ "$linux_ps_modifier_stage" -le 3 ] || return 2
        linux_ps_modifier_stage=4
        ;;
      +)
        [ "$linux_ps_modifier_stage" -le 4 ] || return 2
        linux_ps_modifier_stage=5
        ;;
      *) return 2 ;;
    esac
  done

  [ "$linux_ps_state" = Z ] && return 0
  return 1
}

known_process_group_exists() {
  known_pid="$(validate_pid "${1:-}")"
  known_pgid="$(validate_pid "${2:-}")"
  [ "$known_pid" = "$known_pgid" ] || return 1
  known_group_listing="$(ps -e -o pgid= -o sid= -o stat= 2>/dev/null)" || return 2
  [ -n "$known_group_listing" ] || return 2
  known_group_saw_data=0
  known_group_found=0
  while IFS= read -r known_group_line || [ -n "$known_group_line" ]; do
    IFS=' 	' read -r listed_pgid listed_sid listed_stat listed_extra <<EOF
$known_group_line
EOF
    [ -n "${listed_pgid:-}" ] && [ -n "${listed_sid:-}" ] && [ -n "${listed_stat:-}" ] &&
      [ -z "${listed_extra:-}" ] || return 2
    # Kernel threads legitimately appear as unrelated PGID/SID 0 rows.
    is_valid_ps_identifier "$listed_pgid" && is_valid_ps_identifier "$listed_sid" || return 2
    if classify_linux_ps_stat "$listed_stat"; then
      listed_stat_class=0
    else
      listed_stat_class="$?"
    fi
    [ "$listed_stat_class" -le 1 ] || return 2
    known_group_saw_data=1
    if [ "$listed_pgid" = "$known_pgid" ] && [ "$listed_sid" = "$known_pid" ] &&
      [ "$listed_stat_class" -ne 0 ]; then
      known_group_found=1
    fi
  done <<EOF
$known_group_listing
EOF
  [ "$known_group_saw_data" = 1 ] || return 2
  [ "$known_group_found" = 1 ]
}

known_process_group_is_empty() {
  if known_process_group_exists "$@"; then
    return 1
  else
    known_group_status="$?"
  fi
  [ "$known_group_status" -eq 1 ] || return 2
}

managed_process_matches() {
  # 0 is a confirmed identity match; 1 is confirmed stale/reused/gone;
  # 2 means the identity cannot be safely inspected.
  known_pid="${1:-}"
  known_pgid="${2:-}"
  known_marker="${3:-}"
  is_valid_pid "$known_pid" && is_valid_pid "$known_pgid" &&
    is_valid_process_start_marker "$known_marker" || return 2
  [ "$known_pid" = "$known_pgid" ] || return 2
  if observed_marker="$(process_start_time "$known_pid")"; then
    :
  else
    process_status="$?"
    [ "$process_status" -eq 1 ] && return 1
    return 2
  fi
  [ "$observed_marker" = "$known_marker" ] || return 1
  if observed_pgid="$(process_group_id "$known_pid")"; then
    :
  else
    process_status="$?"
    [ "$process_status" -eq 1 ] && return 1
    return 2
  fi
  [ "$observed_pgid" = "$known_pgid" ] || return 1
  if observed_sid="$(process_session_id "$known_pid")"; then
    :
  else
    process_status="$?"
    [ "$process_status" -eq 1 ] && return 1
    return 2
  fi
  [ "$observed_sid" = "$known_pid" ] || return 1
}

managed_process_group_snapshot() {
  known_pid="${1:-}"
  known_pgid="${2:-}"
  is_valid_pid "$known_pid" && is_valid_pid "$known_pgid" && [ "$known_pid" = "$known_pgid" ] || return 2
  known_snapshot_listing="$(ps -e -o pid= -o pgid= -o sid= 2>/dev/null)" || return 2
  [ -n "$known_snapshot_listing" ] || return 2
  while IFS= read -r known_snapshot_line || [ -n "$known_snapshot_line" ]; do
    IFS=' 	' read -r listed_pid listed_pgid listed_sid listed_extra <<EOF
$known_snapshot_line
EOF
    [ -n "${listed_pid:-}" ] && [ -n "${listed_pgid:-}" ] && [ -n "${listed_sid:-}" ] &&
      [ -z "${listed_extra:-}" ] || return 2
    is_valid_pid "$listed_pid" && is_valid_ps_identifier "$listed_pgid" && is_valid_ps_identifier "$listed_sid" || return 2
    [ "$listed_pgid" = "$known_pgid" ] && [ "$listed_sid" = "$known_pid" ] || continue
    if known_member_marker="$(process_start_time "$listed_pid")"; then
      printf '%s %s\n' "$listed_pid" "$known_member_marker"
    else
      process_status="$?"
      [ "$process_status" -eq 1 ] || return 2
    fi
  done <<EOF
$known_snapshot_listing
EOF
}

snapshot_member_matches() {
  known_pid="${1:-}"
  known_pgid="${2:-}"
  known_member_pid="${3:-}"
  known_member_marker="${4:-}"
  is_valid_pid "$known_pid" && is_valid_pid "$known_pgid" && is_valid_pid "$known_member_pid" &&
    is_valid_process_start_marker "$known_member_marker" && [ "$known_pid" = "$known_pgid" ] || return 2
  if observed_marker="$(process_start_time "$known_member_pid")"; then
    :
  else
    process_status="$?"
    [ "$process_status" -eq 1 ] && return 1
    return 2
  fi
  [ "$observed_marker" = "$known_member_marker" ] || return 1
  if observed_pgid="$(process_group_id "$known_member_pid")"; then
    :
  else
    process_status="$?"
    [ "$process_status" -eq 1 ] && return 1
    return 2
  fi
  [ "$observed_pgid" = "$known_pgid" ] || return 1
  if observed_sid="$(process_session_id "$known_member_pid")"; then
    :
  else
    process_status="$?"
    [ "$process_status" -eq 1 ] && return 1
    return 2
  fi
  [ "$observed_sid" = "$known_pid" ] || return 1
}

snapshot_has_live_member() {
  known_pid="${1:-}"
  known_pgid="${2:-}"
  known_snapshot="${3:-}"
  is_valid_pid "$known_pid" && is_valid_pid "$known_pgid" && [ "$known_pid" = "$known_pgid" ] || return 2
  while IFS=' ' read -r known_member_pid known_member_marker known_extra; do
    [ -n "$known_member_pid" ] || continue
    [ -z "${known_extra:-}" ] || return 2
    if snapshot_member_matches "$known_pid" "$known_pgid" "$known_member_pid" "$known_member_marker"; then
      return 0
    else
      snapshot_status="$?"
      [ "$snapshot_status" -eq 1 ] || return 2
    fi
  done <<EOF
$known_snapshot
EOF
  return 1
}

managed_process_is_gone() {
  known_pid="${1:-}"
  is_valid_pid "$known_pid" || return 2
  if observed_state="$(process_state "$known_pid")"; then
    # The caller has already waited for this direct child.  A remaining
    # zombie is not a runnable member of an owned process group; treating it
    # as gone permits removal of its pre-record lifecycle lock.  Every other
    # observed state remains non-gone, and an unreadable/malformed identity
    # remains indeterminate/fail-closed.
    [ "$observed_state" = Z ] && return 0
    return 1
  else
    state_status="$?"
    [ "$state_status" -eq 1 ] && return 0
    return 2
  fi
}

signal_known_process_group() {
  known_signal="$1"
  known_pgid="$2"
  for known_kill_binary in /bin/kill /usr/bin/kill; do
    [ -x "$known_kill_binary" ] || continue
    "$known_kill_binary" "-$known_signal" -- "-$known_pgid"
    return "$?"
  done
  return 2
}

terminate_known_process_group() {
  known_pid="${1:-}"
  known_pgid="${2:-}"
  known_marker="${3:-}"
  known_signal="$(validate_signal "${4:-}")"
  is_valid_pid "$known_pid" && is_valid_pid "$known_pgid" && [ "$known_pid" = "$known_pgid" ] || return 2
  is_valid_process_start_marker "$known_marker" || return 2
  known_waited=0
  if managed_process_matches "$known_pid" "$known_pgid" "$known_marker"; then
    :
  else
    return "$?"
  fi
  if [ "$known_signal" = KILL ]; then
    if managed_process_matches "$known_pid" "$known_pgid" "$known_marker"; then
      :
    else
      return "$?"
    fi
    signal_known_process_group KILL "$known_pgid" 2>/dev/null || {
      [ "$?" -eq 2 ] && return 2
    }
  else
    if known_snapshot="$(managed_process_group_snapshot "$known_pid" "$known_pgid")"; then
      :
    else
      return "$?"
    fi
    if managed_process_matches "$known_pid" "$known_pgid" "$known_marker"; then
      :
    else
      return "$?"
    fi
    signal_known_process_group "$known_signal" "$known_pgid" 2>/dev/null || {
      [ "$?" -eq 2 ] && return 2
    }
    while :; do
      if known_process_group_exists "$known_pid" "$known_pgid"; then
        [ "$known_waited" -ge 2 ] && break
        sleep 1
        known_waited=$((known_waited + 1))
      else
        known_group_status="$?"
        [ "$known_group_status" -eq 1 ] && return 0
        return 2
      fi
    done
    if ! known_process_group_exists "$known_pid" "$known_pgid"; then
      known_group_status="$?"
      [ "$known_group_status" -eq 1 ] && return 0
      return 2
    fi
    if managed_process_matches "$known_pid" "$known_pgid" "$known_marker"; then
      :
    else
      managed_status="$?"
      if [ "$managed_status" -eq 1 ] && managed_process_is_gone "$known_pid" &&
        snapshot_has_live_member "$known_pid" "$known_pgid" "$known_snapshot"; then
        :
      else
        process_status="$?"
        [ "$managed_status" -eq 1 ] && [ "$process_status" -eq 1 ] && return 1
        return 2
      fi
    fi
    signal_known_process_group KILL "$known_pgid" 2>/dev/null || {
      [ "$?" -eq 2 ] && return 2
    }
  fi
  known_waited=0
  while :; do
    if known_process_group_exists "$known_pid" "$known_pgid"; then
      [ "$known_waited" -ge 2 ] && return 2
      sleep 1
      known_waited=$((known_waited + 1))
    else
      known_group_status="$?"
      [ "$known_group_status" -eq 1 ] && return 0
      return 2
    fi
  done
}

registered_process_matches() {
  run_id="$(validate_run_id "${1:-}")"
  record="$(registry_record_path "$run_id")"
  if [ ! -e "$record" ] && [ ! -L "$record" ]; then
    return 1
  fi
  [ -f "$record" ] && [ ! -L "$record" ] || return 2
  if record_target="$(read_recovery_run_record "$record")"; then
    :
  else
    return 2
  fi
  pid="${record_target%% *}"
  record_target="${record_target#* }"
  pgid="${record_target%% *}"
  marker="${record_target#* }"
  if managed_process_matches "$pid" "$pgid" "$marker"; then
    :
  else
    return "$?"
  fi
  printf '%s %s %s\n' "$pid" "$pgid" "$marker"
}

signal_registered_process_run() {
  [ "$#" -eq 2 ] || fail "signal-run requires run id and signal"
  run_id="$(validate_run_id "$1")"
  signal="$(validate_signal "$2")"
  lock_dir="$(recovery_lock_path "$run_id")"
  terminal_dir="$(recovery_terminal_path "$run_id")"
  if [ -e "$lock_dir" ] || [ -L "$lock_dir" ]; then
    [ ! -e "$terminal_dir" ] && [ ! -L "$terminal_dir" ] || fail "run lifecycle is ambiguous: $run_id"
    recovery_validate_run_lock "$run_id" || fail "run lock is unsafe: $run_id"
    with_run_lifecycle_gate "$lock_dir/signal-gate" signal_registered_process_run_locked "$run_id" "$signal" || {
      signal_status="$?"
      [ "$signal_status" -eq 2 ] && fail "run lock is unsafe: $run_id"
      [ "$signal_status" -eq 3 ] && fail "registered process cannot be safely inspected"
      fail "registered process is stale or invalid"
    }
    return 0
  fi
  if [ -e "$terminal_dir" ] || [ -L "$terminal_dir" ]; then
    recovery_validate_run_terminal "$run_id" || fail "run terminal is unsafe: $run_id"
    with_run_lifecycle_gate "$terminal_dir/signal-gate" signal_registered_terminal_is_complete "$run_id" || {
      terminal_status="$?"
      [ "$terminal_status" -eq 2 ] && fail "run terminal cannot be safely inspected: $run_id"
      fail "run terminal is not complete: $run_id"
    }
    return 0
  fi
  fail "run lock is unsafe: $run_id"
}

revalidate_signal_run_lifecycle_after_gate() {
  run_id="$(validate_run_id "${1:-}")"
  lock_dir="$(recovery_lock_path "$run_id")"
  status_file="$lock_dir/target-status"
  # The caller holds signal-gate.  Repeat the complete lifecycle validation
  # here, rather than relying on its pre-gate snapshot: status publication may
  # have changed while signal-run was waiting for that gate.  No signal,
  # receipt, or record mutation is allowed before this succeeds.
  recovery_validate_run_lock "$run_id" || return 2
  if [ -e "$status_file" ] || [ -L "$status_file" ]; then
    read_owned_run_status "$status_file" >/dev/null || return 2
  fi
}

signal_registered_process_run_locked() {
  run_id="$(validate_run_id "$1")"
  signal="$(validate_signal "$2")"
  record="$(registry_record_path "$run_id")"
  lock_dir="$(recovery_lock_path "$run_id")"
  ready_file="$lock_dir/supervisor-ready"
  go_file="$lock_dir/supervisor-go"
  signal_file="$lock_dir/signal-delivery"
  revalidate_signal_run_lifecycle_after_gate "$run_id" || return 2
  if [ ! -e "$go_file" ] && [ ! -L "$go_file" ]; then
    if read_owned_run_ready "$ready_file" >/dev/null || read_owned_run_attestation "$ready_file" >/dev/null; then
      fail "root run attestation is pending: $run_id"
    fi
    fail "run lock attestation is invalid: $run_id"
  fi
  target="$(read_owned_run_attestation "$ready_file")" || fail "run lock attestation is invalid: $run_id"
  pid="${target%% *}"
  target_rest="${target#* }"
  pgid="${target_rest%% *}"
  marker="${target_rest#* }"
  if [ -e "$signal_file" ] || [ -L "$signal_file" ]; then
    delivered="$(read_owned_run_signal_delivery "$signal_file")" || return 2
    delivered_signal="${delivered%% *}"
    delivered_rest="${delivered#* }"
    delivered_pid="${delivered_rest%% *}"
    delivered_rest="${delivered_rest#* }"
    delivered_pgid="${delivered_rest%% *}"
    delivered_marker="${delivered_rest#* }"
    [ "$delivered_pid" = "$pid" ] && [ "$delivered_pgid" = "$pgid" ] && [ "$delivered_marker" = "$marker" ] || return 2
    [ "$delivered_signal" = "$signal" ] && return 0
  fi
  if [ -e "$record" ] || [ -L "$record" ]; then
    recovery_validate_record "$run_id" || return 2
    [ "$(read_recovery_run_record "$record")" = "$pid $pgid $marker" ] ||
      return 2
  fi
  if terminate_known_process_group "$pid" "$pgid" "$marker" "$signal"; then
    write_owned_run_signal_delivery "$signal_file" "$signal" "$pid" "$pgid" "$marker" || return 2
    if [ -e "$record" ] || [ -L "$record" ]; then
      recovery_validate_record "$run_id" || return 2
      [ "$(read_recovery_run_record "$record")" = "$pid $pgid $marker" ] ||
        return 2
      rm -f -- "$record"
    fi
  else
    termination_status="$?"
    [ "$termination_status" -eq 1 ] && return 1
    return 3
  fi
}

signal_registered_terminal_is_complete() {
  run_id="$(validate_run_id "$1")"
  terminal_dir="$(recovery_terminal_path "$run_id")"
  ready_file="$terminal_dir/supervisor-ready"
  recovery_validate_run_terminal "$run_id" || return 2
  target="$(read_owned_run_attestation "$ready_file")" || return 2
  pid="${target%% *}"
  target_rest="${target#* }"
  pgid="${target_rest%% *}"
  marker="${target_rest#* }"
  if known_process_group_is_empty "$pid" "$pgid"; then
    :
  else
    return "$?"
  fi
  managed_process_is_gone "$pid"
}

acknowledge_registered_process_run_terminal() {
  [ "$#" -eq 1 ] || fail "ack-run-terminal requires one run id"
  run_id="$(validate_run_id "$1")"
  terminal_dir="$(recovery_terminal_path "$run_id")"
  if [ ! -e "$terminal_dir" ] && [ ! -L "$terminal_dir" ]; then
    lock_dir="$(recovery_lock_path "$run_id")"
    record="$(registry_record_path "$run_id")"
    [ ! -e "$lock_dir" ] && [ ! -L "$lock_dir" ] && [ ! -e "$record" ] && [ ! -L "$record" ] ||
      fail "run lifecycle is ambiguous: $run_id"
    return 0
  fi
  [ -d "$terminal_dir" ] && [ ! -L "$terminal_dir" ] || fail "run terminal is unsafe: $run_id"
  with_run_lifecycle_gate "$terminal_dir/signal-gate" acknowledge_registered_process_run_terminal_locked "$run_id" || {
    ack_status="$?"
    [ "$ack_status" -eq 2 ] && fail "run terminal cannot be safely inspected: $run_id"
    fail "run terminal is not complete: $run_id"
  }
}

acknowledge_registered_process_run_terminal_locked() {
  run_id="$(validate_run_id "$1")"
  terminal_dir="$(recovery_terminal_path "$run_id")"
  record="$(registry_record_path "$run_id")"
  signal_registered_terminal_is_complete "$run_id" || return "$?"
  if [ -e "$record" ] || [ -L "$record" ]; then
    return 2
  fi
  rm -f -- "$terminal_dir/supervisor-ready" "$terminal_dir/supervisor-go" "$terminal_dir/target-status" \
    "$terminal_dir/signal-delivery" "$terminal_dir/signal-gate" || return 2
  rmdir -- "$terminal_dir" 2>/dev/null || return 2
}

ensure_recovery_run_registry() {
  [ ! -L "$RUN_REGISTRY_DIR" ] || fail "run registry must not be a symlink: $RUN_REGISTRY_DIR"
  if [ ! -e "$RUN_REGISTRY_DIR" ]; then
    install -d -m 0750 -o root -g root "$RUN_REGISTRY_DIR" || fail "cannot create run registry: $RUN_REGISTRY_DIR"
  fi
  [ -d "$RUN_REGISTRY_DIR" ] && [ ! -L "$RUN_REGISTRY_DIR" ] || fail "run registry is unsafe: $RUN_REGISTRY_DIR"
  [ "$(stat -c '%u:%g:%a' "$RUN_REGISTRY_DIR" 2>/dev/null || true)" = '0:0:750' ] ||
    fail "run registry must be root:root mode 0750: $RUN_REGISTRY_DIR"
}

recovery_lifecycle_file_is_safe() {
  lifecycle_file="$1"
  [ -f "$lifecycle_file" ] && [ ! -L "$lifecycle_file" ] || return 1
  [ "$(stat -c '%u:%g:%a' "$lifecycle_file" 2>/dev/null || true)" = '0:0:600' ] || return 1
}

read_owned_run_go() {
  go_file="$1"
  [ -e "$go_file" ] || return 1
  go_contents="$(cat "$go_file")" || return 2
  case "$go_contents" in
    'helper-go-v1 '*) ;;
    *) return 2 ;;
  esac
  go_pid="${go_contents#helper-go-v1 }"
  [ "$go_contents" = "helper-go-v1 $go_pid" ] || return 2
  is_valid_pid "$go_pid" || return 2
  printf '%s\n' "$go_pid"
}

read_recovery_run_record() {
  record="$1"
  record_contents="$(cat "$record")" || return 1
  IFS=' ' read -r version pid pgid marker extra <<EOF
$record_contents
EOF
  [ "$version" = helper-v1 ] || return 1
  [ -n "${pid:-}" ] && [ -n "${pgid:-}" ] && is_valid_process_start_marker "$marker" || return 1
  [ -z "${extra:-}" ] || return 1
  [ "$record_contents" = "helper-v1 $pid $pgid $marker" ] || return 1
  is_valid_pid "$pid" && is_valid_pid "$pgid" || return 1
  [ "$pid" = "$pgid" ] || return 1
  printf '%s %s %s\n' "$pid" "$pgid" "$marker"
}

recovery_lock_path() {
  run_id="$(validate_run_id "${1:-}")"
  printf '%s/.%s.lock\n' "$RUN_REGISTRY_DIR" "$run_id"
}

recovery_terminal_path() {
  run_id="$(validate_run_id "${1:-}")"
  printf '%s/.%s.terminal\n' "$RUN_REGISTRY_DIR" "$run_id"
}

read_owned_run_signal_delivery() {
  signal_file="$1"
  [ -e "$signal_file" ] || return 1
  [ -f "$signal_file" ] && [ ! -L "$signal_file" ] || return 2
  [ "$(stat -c '%u:%g:%a' "$signal_file" 2>/dev/null || true)" = '0:0:600' ] || return 2
  signal_contents="$(cat "$signal_file")" || return 2
  IFS=' ' read -r signal_version delivered_signal delivered_pid delivered_pgid delivered_marker signal_extra <<EOF
$signal_contents
EOF
  [ "$signal_version" = helper-signal-v1 ] || return 2
  [ -n "${delivered_signal:-}" ] && [ -n "${delivered_pid:-}" ] && [ -n "${delivered_pgid:-}" ] && [ -n "${delivered_marker:-}" ] || return 2
  [ -z "${signal_extra:-}" ] || return 2
  [ "$signal_contents" = "helper-signal-v1 $delivered_signal $delivered_pid $delivered_pgid $delivered_marker" ] || return 2
  validate_signal "$delivered_signal" >/dev/null 2>&1 || return 2
  is_valid_pid "$delivered_pid" && is_valid_pid "$delivered_pgid" && is_valid_process_start_marker "$delivered_marker" || return 2
  [ "$delivered_pid" = "$delivered_pgid" ] || return 2
  printf '%s %s %s %s\n' "$delivered_signal" "$delivered_pid" "$delivered_pgid" "$delivered_marker"
}

recovery_validate_run_lifecycle_dir() {
  lifecycle_dir="$1"
  ready_file="$lifecycle_dir/supervisor-ready"
  go_file="$lifecycle_dir/supervisor-go"
  status_file="$lifecycle_dir/target-status"
  lifecycle_gate="$lifecycle_dir/signal-gate"
  signal_file="$lifecycle_dir/signal-delivery"
  ready_present=0
  go_present=0
  status_present=0
  gate_present=0
  signal_present=0

  [ -d "$lifecycle_dir" ] && [ ! -L "$lifecycle_dir" ] || return 1
  [ "$(stat -c '%u:%g:%a' "$lifecycle_dir" 2>/dev/null || true)" = '0:0:700' ] || return 1
  for lifecycle_file in "$lifecycle_dir"/* "$lifecycle_dir"/.[!.]* "$lifecycle_dir"/..?*; do
    [ -e "$lifecycle_file" ] || [ -L "$lifecycle_file" ] || continue
    lifecycle_name="${lifecycle_file##*/}"
    case "$lifecycle_name" in
      supervisor-ready)
        [ "$ready_present" = 0 ] && recovery_lifecycle_file_is_safe "$lifecycle_file" || return 1
        ready_present=1
        ;;
      supervisor-go)
        [ "$go_present" = 0 ] && recovery_lifecycle_file_is_safe "$lifecycle_file" || return 1
        go_present=1
        ;;
      target-status)
        [ "$status_present" = 0 ] && recovery_lifecycle_file_is_safe "$lifecycle_file" || return 1
        status_present=1
        ;;
      signal-gate)
        [ "$gate_present" = 0 ] && recovery_lifecycle_file_is_safe "$lifecycle_file" || return 1
        gate_present=1
        ;;
      signal-delivery)
        [ "$signal_present" = 0 ] && recovery_lifecycle_file_is_safe "$lifecycle_file" || return 1
        signal_present=1
        ;;
      *) return 1 ;;
    esac
  done

  [ "$gate_present" = 1 ] || return 1
  if [ "$ready_present" = 0 ]; then
    [ "$go_present" = 0 ] && [ "$status_present" = 0 ] && [ "$signal_present" = 0 ] || return 1
    return 0
  fi
  if [ "$go_present" = 0 ]; then
    if read_owned_run_ready "$ready_file" >/dev/null || read_owned_run_attestation "$ready_file" >/dev/null; then
      :
    else
      return 1
    fi
    [ "$status_present" = 0 ] && [ "$signal_present" = 0 ] || return 1
    return 0
  fi
  ready_target="$(read_owned_run_attestation "$ready_file")" || return 1
  ready_pid="${ready_target%% *}"
  ready_rest="${ready_target#* }"
  ready_pgid="${ready_rest%% *}"
  ready_marker="${ready_rest#* }"
  go_pid="$(read_owned_run_go "$go_file")" || return 1
  [ "$go_pid" = "$ready_pid" ] || return 1
  if [ "$status_present" = 1 ]; then
    read_owned_run_status "$status_file" >/dev/null || return 1
  fi
  if [ "$signal_present" = 1 ]; then
    signal_target="$(read_owned_run_signal_delivery "$signal_file")" || return 1
    signal_rest="${signal_target#* }"
    signal_pid="${signal_rest%% *}"
    signal_rest="${signal_rest#* }"
    signal_pgid="${signal_rest%% *}"
    signal_marker="${signal_rest#* }"
    [ "$signal_pid" = "$ready_pid" ] && [ "$signal_pgid" = "$ready_pgid" ] && [ "$signal_marker" = "$ready_marker" ] || return 1
  fi
}

# An invalid status is never a target outcome.  The writer leaves this marker
# only after the root-owned v2 lifecycle was attested and the supervisor group
# was stopped.  Recovery intentionally does not read, stat, or follow the
# invalid target-status entry; it only removes that fixed directory entry after
# re-checking the paired record and that the attested group is gone.
recovery_invalid_status_marker_is_safe() {
  quarantine_file="$1"
  [ -f "$quarantine_file" ] && [ ! -L "$quarantine_file" ] || return 1
  [ "$(stat -c '%u:%g:%a' "$quarantine_file" 2>/dev/null || true)" = '0:0:600' ] || return 1
  [ "$(cat "$quarantine_file" 2>/dev/null || true)" = 'helper-invalid-status-v1' ]
}

invalid_status_quarantine_marker_is_present() {
  run_id="$(validate_run_id "${1:-}")"
  quarantine_file="$(recovery_lock_path "$run_id")/target-status-invalid"
  [ -e "$quarantine_file" ] || [ -L "$quarantine_file" ]
}

invalid_status_quarantine_status_leaf_is_present() {
  lock_dir="$1"
  # `find -P` performs lstat-style directory enumeration and never follows a
  # symlink.  In particular, do not replace this with `[ -e "$status_file" ]`:
  # that shell builtin resolves a quarantined symlink to an existing target
  # before the marker path can reject it as untrusted.
  status_leaf="$(/usr/bin/find -P "$lock_dir" -maxdepth 1 -mindepth 1 -name target-status -printf '%f\n' 2>/dev/null)" || return 1
  [ "$status_leaf" = target-status ]
}

recovery_validate_invalid_status_quarantine_lock() {
  run_id="$(validate_run_id "${1:-}")"
  lock_dir="$(recovery_lock_path "$run_id")"
  ready_file="$lock_dir/supervisor-ready"
  go_file="$lock_dir/supervisor-go"
  status_file="$lock_dir/target-status"
  quarantine_file="$lock_dir/target-status-invalid"
  lifecycle_gate="$lock_dir/signal-gate"
  ready_present=0
  go_present=0
  status_present=0
  quarantine_present=0
  gate_present=0

  [ -d "$lock_dir" ] && [ ! -L "$lock_dir" ] || return 1
  [ "$(stat -c '%u:%g:%a' "$lock_dir" 2>/dev/null || true)" = '0:0:700' ] || return 1
  for lifecycle_file in "$lock_dir"/* "$lock_dir"/.[!.]* "$lock_dir"/..?*; do
    lifecycle_name="${lifecycle_file##*/}"
    case "$lifecycle_name" in
      target-status)
        # Dispatch this untrusted leaf before *any* generic presence test.
        # Its content, identity and symlink target are never a recovery input.
        [ "$status_present" = 0 ] || return 1
        invalid_status_quarantine_status_leaf_is_present "$lock_dir" || return 1
        status_present=1
        ;;
      *)
        [ -e "$lifecycle_file" ] || [ -L "$lifecycle_file" ] || continue
        case "$lifecycle_name" in
      supervisor-ready)
        [ "$ready_present" = 0 ] && recovery_lifecycle_file_is_safe "$lifecycle_file" || return 1
        ready_present=1
        ;;
      supervisor-go)
        [ "$go_present" = 0 ] && recovery_lifecycle_file_is_safe "$lifecycle_file" || return 1
        go_present=1
        ;;
      target-status-invalid)
        [ "$quarantine_present" = 0 ] && recovery_invalid_status_marker_is_safe "$lifecycle_file" || return 1
        quarantine_present=1
        ;;
      signal-gate)
        [ "$gate_present" = 0 ] && recovery_lifecycle_file_is_safe "$lifecycle_file" || return 1
        gate_present=1
        ;;
      *) return 1 ;;
        esac
        ;;
    esac
  done
  [ "$ready_present" = 1 ] && [ "$go_present" = 1 ] && [ "$status_present" = 1 ] &&
    [ "$quarantine_present" = 1 ] && [ "$gate_present" = 1 ] || return 1
  ready_target="$(read_owned_run_attestation "$ready_file")" || return 1
  ready_pid="${ready_target%% *}"
  go_pid="$(read_owned_run_go "$go_file")" || return 1
  [ "$go_pid" = "$ready_pid" ]
}

recovery_validate_run_lock() {
  run_id="$(validate_run_id "${1:-}")"
  lock_dir="$(recovery_lock_path "$run_id")"
  recovery_validate_run_lifecycle_dir "$lock_dir"
}

recovery_validate_run_terminal() {
  run_id="$(validate_run_id "${1:-}")"
  terminal_dir="$(recovery_terminal_path "$run_id")"
  recovery_validate_run_lifecycle_dir "$terminal_dir" || return 1
  [ -f "$terminal_dir/signal-delivery" ] && [ ! -L "$terminal_dir/signal-delivery" ] || return 1
}

recovery_validate_record() {
  run_id="$(validate_run_id "${1:-}")"
  record="$(registry_record_path "$run_id")"
  [ -f "$record" ] && [ ! -L "$record" ] || return 1
  [ "$(stat -c '%u:%g:%a' "$record" 2>/dev/null || true)" = '0:0:600' ] || return 1
  read_recovery_run_record "$record" >/dev/null
}

recovery_validate_registry_layout() {
  for artifact in "$RUN_REGISTRY_DIR"/* "$RUN_REGISTRY_DIR"/.[!.]* "$RUN_REGISTRY_DIR"/..?*; do
    [ -e "$artifact" ] || [ -L "$artifact" ] || continue
    artifact_name="${artifact##*/}"
    case "$artifact_name" in
      .*.lock)
        lock_run_id="${artifact_name#.}"
        lock_run_id="${lock_run_id%.lock}"
        [ ".${lock_run_id}.lock" = "$artifact_name" ] || return 1
        validate_run_id "$lock_run_id" >/dev/null 2>&1 || return 1
        if invalid_status_quarantine_marker_is_present "$lock_run_id"; then
          recovery_validate_invalid_status_quarantine_lock "$lock_run_id" || return 1
        else
          recovery_validate_run_lock "$lock_run_id" || return 1
        fi
        ;;
      .*.terminal)
        terminal_run_id="${artifact_name#.}"
        terminal_run_id="${terminal_run_id%.terminal}"
        [ ".${terminal_run_id}.terminal" = "$artifact_name" ] || return 1
        validate_run_id "$terminal_run_id" >/dev/null 2>&1 || return 1
        recovery_validate_run_terminal "$terminal_run_id" || return 1
        ;;
      *)
        validate_run_id "$artifact_name" >/dev/null 2>&1 || return 1
        recovery_validate_record "$artifact_name" || return 1
        ;;
    esac
  done
}

recover_terminal_run() {
  run_id="$(validate_run_id "${1:-}")"
  terminal_dir="$(recovery_terminal_path "$run_id")"
  record="$(registry_record_path "$run_id")"
  ready_file="$terminal_dir/supervisor-ready"
  signal_registered_terminal_is_complete "$run_id" || {
    terminal_status="$?"
    [ "$terminal_status" -eq 1 ] && fail "run terminal is not complete: $run_id"
    fail "run terminal cannot be safely inspected: $run_id"
  }
  if [ -e "$record" ] || [ -L "$record" ]; then
    recovery_validate_record "$run_id" || fail "run record is unsafe: $run_id"
    ready_target="$(read_owned_run_attestation "$ready_file")" || fail "run terminal is unsafe: $run_id"
    [ "$(read_recovery_run_record "$record")" = "$ready_target" ] ||
      fail "run record does not match terminal attestation: $run_id"
  fi
  rm -f -- "$terminal_dir/supervisor-ready" "$terminal_dir/supervisor-go" "$terminal_dir/target-status" \
    "$terminal_dir/signal-delivery" "$terminal_dir/signal-gate" || fail "cannot remove recovered run terminal: $run_id"
  rmdir -- "$terminal_dir" 2>/dev/null || fail "cannot remove recovered run terminal: $run_id"
  if [ -e "$record" ] || [ -L "$record" ]; then
    recovery_validate_record "$run_id" || fail "run record is unsafe: $run_id"
    rm -f -- "$record" || fail "cannot remove recovered run record: $run_id"
  fi
}

remove_recovery_run_artifacts() {
  run_id="$(validate_run_id "${1:-}")"
  expected_ready_pid="$(validate_pid "${2:-}")"
  expected_marker="${3:-}"
  expected_record_target="${4:-}"
  lock_dir="$(recovery_lock_path "$run_id")"
  record="$(registry_record_path "$run_id")"
  ready_file="$lock_dir/supervisor-ready"
  go_file="$lock_dir/supervisor-go"
  status_file="$lock_dir/target-status"
  lifecycle_gate="$lock_dir/signal-gate"
  signal_file="$lock_dir/signal-delivery"

  # Repeat every ownership and liveness check immediately before destructive cleanup.
  recovery_validate_run_lock "$run_id" || return 2
  if ready_target="$(read_owned_run_attestation "$ready_file")"; then
    ready_pid="${ready_target%% *}"
    ready_rest="${ready_target#* }"
    ready_pgid="${ready_rest%% *}"
    ready_marker="${ready_rest#* }"
    [ "$ready_pid" = "$expected_ready_pid" ] || return 2
    [ -n "$expected_marker" ] || return 2
    [ "$ready_pgid" = "$expected_ready_pid" ] && [ "$ready_marker" = "$expected_marker" ] || return 2
  else
    read_owned_run_ready "$ready_file" >/dev/null || return 2
    ready_pid="$(read_owned_run_ready "$ready_file")" || return 2
    [ "$ready_pid" = "$expected_ready_pid" ] || return 2
    [ -z "$expected_marker" ] || return 2
  fi
  if [ -n "$expected_record_target" ]; then
    [ -n "$expected_marker" ] || return 2
    [ "$expected_record_target" = "$expected_ready_pid $expected_ready_pid $expected_marker" ] || return 2
    recovery_validate_record "$run_id" || return 2
    [ "$(read_recovery_run_record "$record")" = "$expected_record_target" ] || return 2
  else
    [ ! -e "$record" ] && [ ! -L "$record" ] || return 2
  fi
  recovery_ready_pid_is_safe_to_forget "$ready_file" || return 2

  rm -f -- "$ready_file" "$go_file" "$status_file" "$lifecycle_gate" "$signal_file" || return 1
  rmdir -- "$lock_dir" 2>/dev/null || return 1
  if [ -n "$expected_record_target" ]; then
    recovery_validate_record "$run_id" || return 2
    [ "$(read_recovery_run_record "$record")" = "$expected_record_target" ] || return 2
    rm -f -- "$record" || return 1
  fi
}

recovery_ready_pid_is_safe_to_forget() {
  if ready_target="$(read_owned_run_attestation "$1")"; then
    ready_pid="${ready_target%% *}"
  else
    ready_pid="$(read_owned_run_ready "$1")" || return 1
  fi
  if known_process_group_exists "$ready_pid" "$ready_pid"; then
    return 1
  else
    recovery_group_status="$?"
  fi
  [ "$recovery_group_status" -eq 1 ] || return 2

  if ! kill -0 "$ready_pid" 2>/dev/null; then
    [ ! -e "/proc/$ready_pid" ] && [ ! -L "/proc/$ready_pid" ] || return 2
    return 0
  fi

  recovery_leader="$(ps -o pgid= -o sid= -o stat= -p "$ready_pid" 2>/dev/null)" || return 2
  set -- $recovery_leader
  [ "$#" -eq 3 ] || return 2
  is_valid_pid "$1" && is_valid_pid "$2" || return 2
  [ "$1" = "$ready_pid" ] && [ "$2" = "$ready_pid" ] || return 2
  classify_linux_ps_stat "$3"
}

recover_invalid_status_quarantine_locked() {
  run_id="$(validate_run_id "${1:-}")"
  record="$(registry_record_path "$run_id")"
  lock_dir="$(recovery_lock_path "$run_id")"
  ready_file="$lock_dir/supervisor-ready"
  go_file="$lock_dir/supervisor-go"
  status_file="$lock_dir/target-status"
  quarantine_file="$lock_dir/target-status-invalid"
  lifecycle_gate="$lock_dir/signal-gate"
  recovery_validate_invalid_status_quarantine_lock "$run_id" || return 2
  ready_target="$(read_owned_run_attestation "$ready_file")" || return 2
  # Invalid-status quarantine is created only by run-owned while its paired
  # record still exists.  signal-run instead moves the lifecycle to .terminal;
  # accepting a recordless lock here would authorize an unreachable shape.
  recovery_validate_record "$run_id" || return 2
  record_target="$(read_recovery_run_record "$record")" || return 2
  [ "$record_target" = "$ready_target" ] || return 2
  # No terminate_known_process_group here: a quarantine is removed only after
  # the already-attested group is independently confirmed absent.
  recovery_ready_pid_is_safe_to_forget "$ready_file" || return 2
  rm -f -- "$ready_file" "$go_file" "$status_file" "$quarantine_file" "$lifecycle_gate" || return 1
  rmdir -- "$lock_dir" 2>/dev/null || return 1
  recovery_validate_record "$run_id" || return 2
  [ "$(read_recovery_run_record "$record")" = "$record_target" ] || return 2
  rm -f -- "$record" || return 1
}

recover_invalid_status_quarantine() {
  run_id="$(validate_run_id "${1:-}")"
  lock_dir="$(recovery_lock_path "$run_id")"
  recovery_validate_invalid_status_quarantine_lock "$run_id" || fail "invalid-status quarantine is unsafe: $run_id"
  with_run_lifecycle_gate "$lock_dir/signal-gate" recover_invalid_status_quarantine_locked "$run_id" || {
    recovery_status="$?"
    [ "$recovery_status" -eq 2 ] && fail "invalid-status quarantine changed or remains live: $run_id"
    fail "cannot remove invalid-status quarantine: $run_id"
  }
}

recover_paired_run() {
  run_id="$(validate_run_id "${1:-}")"
  record="$(registry_record_path "$run_id")"
  lock_dir="$(recovery_lock_path "$run_id")"
  ready_file="$lock_dir/supervisor-ready"
  [ -d "$lock_dir" ] && [ ! -L "$lock_dir" ] || fail "run record has no paired lock: $run_id"
  recovery_validate_run_lock "$run_id" || fail "run lock is unsafe: $run_id"
  record_target="$(read_recovery_run_record "$record")" || fail "run record is unsafe: $run_id"
  pid="${record_target%% *}"
  record_target="${record_target#* }"
  pgid="${record_target%% *}"
  marker="${record_target#* }"
  ready_target="$(read_owned_run_attestation "$ready_file")" || fail "run lock attestation is invalid: $run_id"
  ready_pid="${ready_target%% *}"
  ready_rest="${ready_target#* }"
  ready_pgid="${ready_rest%% *}"
  ready_marker="${ready_rest#* }"
  [ "$ready_pid" = "$pid" ] && [ "$ready_pgid" = "$pgid" ] && [ "$ready_marker" = "$marker" ] ||
    fail "run record and lock attestation mismatch: $run_id"
  registered_stale=0

  if registered_target="$(registered_process_matches "$run_id")"; then
    if terminate_known_process_group "$pid" "$pgid" "$marker" TERM; then
      :
    else
      termination_status="$?"
      [ "$termination_status" -eq 1 ] || fail "registered process cannot be safely inspected: $run_id"
      registered_stale=1
    fi
  else
    registered_status="$?"
    [ "$registered_status" -eq 1 ] || fail "registered process cannot be safely inspected: $run_id"
    registered_stale=1
  fi

  if recovery_ready_pid_is_safe_to_forget "$ready_file"; then
    :
  else
    recovery_status="$?"
    [ "$registered_stale" -eq 1 ] && [ "$recovery_status" -eq 1 ] &&
      fail "registered process is stale or invalid: $run_id"
    [ "$recovery_status" -eq 1 ] && fail "run lock supervisor is still live: $run_id"
    fail "run lock supervisor cannot be safely inspected: $run_id"
  fi
  if remove_recovery_run_artifacts "$run_id" "$ready_pid" "$marker" "$pid $pgid $marker"; then
    :
  else
    removal_status="$?"
    [ "$removal_status" -eq 2 ] && fail "recovered run artifacts changed or cannot be safely inspected: $run_id"
    fail "cannot remove recovered run artifacts: $run_id"
  fi
}

recover_recordless_run_lock() {
  run_id="$(validate_run_id "${1:-}")"
  lock_dir="$(recovery_lock_path "$run_id")"
  ready_file="$lock_dir/supervisor-ready"
  recovery_validate_run_lock "$run_id" || fail "run lock is unsafe: $run_id"
  [ -e "$ready_file" ] || fail "recordless run lock is still starting: $run_id"
  if ready_target="$(read_owned_run_attestation "$ready_file")"; then
    ready_pid="${ready_target%% *}"
    ready_rest="${ready_target#* }"
    ready_pgid="${ready_rest%% *}"
    ready_marker="${ready_rest#* }"
    [ "$ready_pgid" = "$ready_pid" ] || fail "run lock attestation is invalid: $run_id"
  else
    ready_pid="$(read_owned_run_ready "$ready_file")" || fail "run lock is unsafe: $run_id"
    ready_marker=""
  fi
  if recovery_ready_pid_is_safe_to_forget "$ready_file"; then
    :
  else
    recovery_status="$?"
    [ "$recovery_status" -eq 1 ] && fail "run lock supervisor is still live: $run_id"
    fail "run lock supervisor cannot be safely inspected: $run_id"
  fi
  if remove_recovery_run_artifacts "$run_id" "$ready_pid" "$ready_marker" ""; then
    :
  else
    removal_status="$?"
    [ "$removal_status" -eq 2 ] && fail "recovered run lock changed or cannot be safely inspected: $run_id"
    fail "cannot remove recovered run lock: $run_id"
  fi
}

recover_registered_process_runs() {
  ensure_recovery_run_registry
  recovery_validate_registry_layout || fail "run registry contains unsafe recovery artifacts"
  for record in "$RUN_REGISTRY_DIR"/*; do
    [ -e "$record" ] || continue
    run_id="$(basename "$record")"
    terminal_dir="$(recovery_terminal_path "$run_id")"
    if [ -e "$terminal_dir" ] || [ -L "$terminal_dir" ]; then
      recover_terminal_run "$run_id"
    elif invalid_status_quarantine_marker_is_present "$run_id"; then
      recover_invalid_status_quarantine "$run_id"
    else
      recover_paired_run "$run_id"
    fi
  done
  for lock_dir in "$RUN_REGISTRY_DIR"/.[!.]*.lock "$RUN_REGISTRY_DIR"/..?*.lock; do
    [ -e "$lock_dir" ] || [ -L "$lock_dir" ] || continue
    lock_name="${lock_dir##*/}"
    run_id="${lock_name#.}"
    run_id="${run_id%.lock}"
    record="$(registry_record_path "$run_id")"
    [ ! -e "$record" ] && [ ! -L "$record" ] || continue
    recover_recordless_run_lock "$run_id"
  done
  for terminal_dir in "$RUN_REGISTRY_DIR"/.[!.]*.terminal "$RUN_REGISTRY_DIR"/..?*.terminal; do
    [ -e "$terminal_dir" ] || [ -L "$terminal_dir" ] || continue
    terminal_name="${terminal_dir##*/}"
    run_id="${terminal_name#.}"
    run_id="${run_id%.terminal}"
    record="$(registry_record_path "$run_id")"
    [ ! -e "$record" ] && [ ! -L "$record" ] || continue
    recover_terminal_run "$run_id"
  done
}

recover_quarantined_process_run() {
  [ "$#" -eq 1 ] || fail "recover-run requires one run id"
  run_id="$(validate_run_id "$1")"
  record="$(registry_record_path "$run_id")"
  lock_dir="$(recovery_lock_path "$run_id")"
  terminal_dir="$(recovery_terminal_path "$run_id")"
  # A quarantined Core state is released only after this exact run has a
  # root-owned paired record and v2 lock attestation, or the terminal proof
  # created after the attested launcher has been reaped. Missing artifacts are
  # not proof of termination: an untrusted launcher may still be alive.
  ensure_recovery_run_registry
  marker_present=0
  if invalid_status_quarantine_marker_is_present "$run_id"; then
    marker_present=1
  fi
  recovery_validate_registry_layout || fail "run registry contains unsafe recovery artifacts"
  if [ -e "$terminal_dir" ] || [ -L "$terminal_dir" ]; then
    recovery_validate_run_terminal "$run_id" || fail "quarantined run terminal is unsafe: $run_id"
    recover_terminal_run "$run_id"
  else
    [ -f "$record" ] && [ ! -L "$record" ] || fail "quarantined run recovery artifacts are missing: $run_id"
    [ -d "$lock_dir" ] && [ ! -L "$lock_dir" ] || fail "quarantined run recovery artifacts are missing: $run_id"
    if [ "$marker_present" = 1 ]; then
      recover_invalid_status_quarantine "$run_id"
    else
      recover_paired_run "$run_id"
    fi
  fi
  printf 'recovered-run-v1 %s\n' "$run_id"
}

require_root

command="${1:-}"
[ -n "$command" ] || fail "command is required"
shift

case "$command" in
  core-update)
    [ "$#" -eq 2 ] || fail "core-update requires update ID and version"
    case "$1" in ''|*[!a-f0-9-]*) fail "invalid update UUID" ;; esac
    case "$2" in ''|*[!a-z0-9.]*) fail "invalid update version" ;; esac
    printf '%s\n' "$1" | grep -Eq '^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$' || fail "invalid update UUID"
    [ "${#2}" -le 64 ] &&
      printf '%s\n' "$2" | grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)((a|b|rc)(0|[1-9][0-9]*))?(\.post(0|[1-9][0-9]*))?$' ||
      fail "invalid update version"
    installer=/usr/local/libexec/gp-control-plane/install-linux.sh
    [ -f "$installer" ] && [ ! -L "$installer" ] && [ "$(readlink -f -- "$installer")" = "$installer" ] &&
      [ "$(stat -c '%u:%g:%a' "$installer")" = '0:0:755' ] || fail "trusted Core installer is unavailable"
    # Leaving the cgroup is insufficient if sudo is still relaying SIGTERM
    # from the Core service to its child. Fork before creating the fixed scope.
    /usr/bin/setsid --fork /usr/bin/systemd-run --quiet --scope --unit=gp-control-plane-core-update \
      /usr/bin/systemd-cat --identifier=gp-core-update \
      /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin /bin/bash "$installer" --prepared-update "$1" "$2" \
      </dev/null >/dev/null 2>&1 || fail "could not fork the Core installer"
    attempts=0
    while [ "$attempts" -lt 40 ]; do
      if [ "$(/usr/bin/systemctl show --property=ActiveState --value gp-control-plane-core-update.scope)" = active ]; then
        exit 0
      fi
      attempts=$((attempts + 1))
      sleep 0.1
    done
    fail "Core installer scope did not become active"
    ;;
  check)
    ensure_run_registry
    exit 0
    ;;
  signal-run)
    with_discovery_gate signal_registered_process_run "$@"
    ;;
  ack-run-terminal)
    with_discovery_gate acknowledge_registered_process_run_terminal "$@"
    ;;
  recover-runs)
    [ "$#" -eq 0 ] || fail "recover-runs accepts no arguments"
    with_recovery_gate recover_registered_process_runs
    ;;
  recover-run)
    with_recovery_gate recover_quarantined_process_run "$@"
    ;;
  run)
    with_discovery_gate run_target "$@"
    ;;
  run-owned)
    with_discovery_gate run_owned_target "$@"
    ;;
  run-multidomain)
    with_discovery_gate run_multidomain_target "$@"
    ;;
  run-multidomain-owned)
    with_discovery_gate run_owned_multidomain_target "$@"
    ;;
  run-env)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--" ]; then
        shift
        break
      fi
      validate_env_assignment "$1"
      export "$1"
      shift
    done
    with_discovery_gate run_target "$@"
    ;;
  run-owned-env)
    run_id="$(validate_run_id "${1:-}")"
    shift
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--" ]; then
        shift
        break
      fi
      validate_env_assignment "$1"
      export "$1"
      shift
    done
    with_discovery_gate run_owned_target "$run_id" "$@"
    ;;
  run-multidomain-env)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--" ]; then
        shift
        break
      fi
      validate_env_assignment "$1"
      export "$1"
      shift
    done
    with_discovery_gate run_multidomain_target "$@"
    ;;
  run-multidomain-owned-env)
    run_id="$(validate_run_id "${1:-}")"
    shift
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--" ]; then
        shift
        break
      fi
      validate_env_assignment "$1"
      export "$1"
      shift
    done
    with_discovery_gate run_owned_multidomain_target "$run_id" "$@"
    ;;
  nft-list-tables)
    exec nft list tables
    ;;
  nft-delete-blockcheck-table)
    family="${1:-}"
    table="${2:-}"
    case "$family" in
      ip|ip6|inet|arp|bridge|netdev) ;;
      *) fail "unsupported nft family: $family" ;;
    esac
    case "$table" in
      blockcheck*) table_digits="${table#blockcheck}" ;;
      *) fail "unsupported nft table: $table" ;;
    esac
    case "$table_digits" in
      ''|*[!0-9]*) fail "unsupported nft table: $table" ;;
    esac
    exec nft delete table "$family" "$table"
    ;;
  *)
    fail "unsupported command: $command"
    ;;
esac
