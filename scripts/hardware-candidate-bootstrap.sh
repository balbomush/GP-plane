#!/usr/bin/env bash
# Internal Pi validation transport only. Public installation uses bootstrap-linux.sh and an annotated tag.
set -Eeuo pipefail
REPO_URL='https://github.com/balbomush/GP-access-control-plane.git'
CANDIDATE_SHA=
INSTALL_WEB="${GP_INSTALL_WEB:-off}"
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { printf '%s\n' 'usage: hardware-candidate-bootstrap.sh --candidate-sha <40-lowercase-hex>' >&2; exit 64; }
need() { command -v "$1" >/dev/null 2>&1 || fail "required command is unavailable: $1"; }
[ "$(id -u)" -ne 0 ] || fail 'run the internal hardware bootstrap as the GP install user, not root'
while [ "$#" -gt 0 ]; do
  case "$1" in
    --candidate-sha) [ "$#" -ge 2 ] || usage; CANDIDATE_SHA="$2"; shift 2 ;;
    *) usage ;;
  esac
done
printf '%s\n' "$CANDIDATE_SHA" | grep -Eq '^[0-9a-f]{40}$' || fail 'candidate SHA must be a full lowercase commit SHA'
case "$INSTALL_WEB" in on|off) ;; *) fail 'GP_INSTALL_WEB must be on or off' ;; esac
[ "$INSTALL_WEB" = off ] || fail 'Core-only installation: install GP Plane Web separately; GP_INSTALL_WEB=on is no longer supported'
need git; need python3; need sudo
# v0.4 devices may have used either supported state location.  A clean-install
# handoff is safe only when exactly one of them is present.
v040_checkout_state="$HOME/gp/GP-access-control-plane/build/state"
v040_data_state="$HOME/gp/.GP-access-control-plane.data/state"
v040_state=
verify_vault() {
  python3 "$source_dir/scripts/clean-install-vault.py" --verify --state-dir "$1" --home "$HOME"
}
source_dir="$(mktemp -d "${TMPDIR:-/tmp}/gp-hardware-candidate.XXXXXX")"
cleanup() { rm -rf -- "$source_dir"; }
trap cleanup EXIT
git clone --no-checkout --depth=1 --branch dev "$REPO_URL" "$source_dir"
git -C "$source_dir" checkout --detach "$CANDIDATE_SHA"
[ "$(git -C "$source_dir" rev-parse HEAD)" = "$CANDIDATE_SHA" ] || fail 'checkout does not match the exact candidate SHA'
[ "$(git -C "$source_dir" rev-parse refs/remotes/origin/dev)" = "$CANDIDATE_SHA" ] || fail 'candidate SHA is not the frozen origin/dev tip'
[ -z "$(git -C "$source_dir" status --porcelain)" ] || fail 'exact candidate source tree is not clean'
initial_install=off
if { [ -e "$v040_checkout_state" ] || [ -L "$v040_checkout_state" ]; } \
  && { [ -e "$v040_data_state" ] || [ -L "$v040_data_state" ]; }; then
  fail "both supported v0.4 state sources exist; remove neither source before resolving: $v040_checkout_state and $v040_data_state"
elif [ -e "$v040_checkout_state" ] || [ -L "$v040_checkout_state" ]; then
  v040_state="$v040_checkout_state"
elif [ -e "$v040_data_state" ] || [ -L "$v040_data_state" ]; then
  v040_state="$v040_data_state"
fi
if [ -n "$v040_state" ]; then
  [ -d "$v040_state" ] && [ ! -L "$v040_state" ] \
    || fail "canonical v0.4 state is not a non-symlink directory: $v040_state"
  v040_state_canonical="$(readlink -f -- "$v040_state" 2>/dev/null || true)"
  [ "$v040_state_canonical" = "$v040_state" ] \
    || fail "canonical v0.4 state path is unsafe: $v040_state"
  v040_strategy_dir="$v040_state/strategy-finder"
  [ -d "$v040_strategy_dir" ] && [ ! -L "$v040_strategy_dir" ] \
    || fail "canonical v0.4 strategy-finder is not a non-symlink directory: $v040_strategy_dir"
  v040_strategy_dir_canonical="$(readlink -f -- "$v040_strategy_dir" 2>/dev/null || true)"
  [ "$v040_strategy_dir_canonical" = "$v040_state_canonical/strategy-finder" ] \
    || fail "canonical v0.4 strategy-finder path escapes state: $v040_strategy_dir"
  v040_sqlite="$v040_strategy_dir/state.sqlite3"
  [ -f "$v040_sqlite" ] && [ ! -L "$v040_sqlite" ] \
    || fail "canonical v0.4 state has an invalid layout: $v040_state"
  v040_sqlite_canonical="$(readlink -f -- "$v040_sqlite" 2>/dev/null || true)"
  [ "$v040_sqlite_canonical" = "$v040_strategy_dir_canonical/state.sqlite3" ] \
    || fail "canonical v0.4 state database path escapes state: $v040_sqlite"
  # A pending vault cannot be reused while its source is still live: a failed
  # pre-sudo attempt may have left newer source changes behind.
  if verify_vault "$v040_state" 2>/dev/null; then
    fail 'pending clean-install vault exists while canonical v0.4 state is still live; nothing was removed'
  fi
  python3 "$source_dir/scripts/clean-install-vault.py" --state-dir "$v040_state" --home "$HOME"
  # This mandatory verification is deliberately not a quiet existence probe.
  verify_vault "$v040_state"
# v0.4.0 requires --state-dir even for --verify.  Both supported source paths
# are absent here, so this is only an argparse-compatible placeholder; vault
# identity and verification remain device-local under --home.
elif verify_vault "$v040_data_state" 2>/dev/null; then
  :
else
  initial_install=on
fi
sudo -n -- "$source_dir/scripts/install-linux.sh" --source-dir "$source_dir" --install-user "$(id -un)" --candidate-sha "$CANDIDATE_SHA" --web "$INSTALL_WEB" --initial-install "$initial_install"
