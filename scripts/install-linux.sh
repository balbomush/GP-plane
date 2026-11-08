#!/usr/bin/env bash
# Core installer: ordinary replacement or explicit clean installation.
set -Eeuo pipefail
PATH='/usr/sbin:/usr/bin:/sbin:/bin'
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
phase=validation
trap 'printf "ERROR: Core installation failed in phase=%s\n" "$phase" >&2' ERR
usage() { printf '%s\n' 'usage: install-linux.sh --source-dir DIR --install-user USER (--tag vX.Y.Z|vX.Y.Z-alpha.N | --candidate-sha SHA) --web off --mode ordinary|clean' >&2; exit 64; }
[ "$(id -u)" -eq 0 ] || fail 'must be run by the bootstrap sudo process'
# Ordinary installation owns only the managed Core program and its venv.
CORE_UNIT=gp-control-plane-core.service
find_managed_core() {
  local service_user exec_start
  service_user="$(systemctl show -p User --value "$CORE_UNIT")"
  [[ "$service_user" =~ ^[a-zA-Z0-9_-]+$ && "$service_user" != root ]] || fail 'managed Core service user is unavailable'
  [ -z "${INSTALL_USER:-}" ] || [ "$INSTALL_USER" = "$service_user" ] || fail 'Core belongs to another install user'
  INSTALL_USER="$service_user"
  target_home="$(getent passwd "$INSTALL_USER" | cut -d: -f6)"
  [ -n "$target_home" ] && [ -d "$target_home" ] && [ "$(readlink -f -- "$target_home")" = "$target_home" ] || fail 'unsafe Core home'
  gp_root="$target_home/gp"
  install_dir="$gp_root/GP-access-control-plane"
  state_parent="$gp_root/.GP-access-control-plane.data"
  state_dir="$state_parent/state"
  for managed in "$gp_root" "$install_dir" "$state_parent" "$state_dir"; do
    [ -d "$managed" ] && [ ! -L "$managed" ] && [ "$(readlink -f -- "$managed")" = "$managed" ] || fail 'unsafe managed Core directory'
  done
  [ ! -e "$install_dir/build/state" ] && [ ! -L "$install_dir/build/state" ] || fail 'Core data is inside the old program directory'
  [ "$(systemctl show -p WorkingDirectory --value "$CORE_UNIT")" = "$install_dir" ] || fail 'Core service does not own this program'
  exec_start="$(systemctl show -p ExecStart --value "$CORE_UNIT")"
  [[ "$exec_start" == *"path=$install_dir/.venv/bin/gp-control-plane "* ]] || fail 'unexpected Core service command'
  [[ "$exec_start" =~ --port[[:space:]]+([0-9]+) ]] || fail 'managed Core port could not be determined'
  core_port="${BASH_REMATCH[1]}"
  [ "${#core_port}" -le 5 ] && [ "$((10#$core_port))" -gt 0 ] && [ "$((10#$core_port))" -le 65535 ] || fail 'invalid Core port'
  [[ "$exec_start" =~ --host[[:space:]]+([^[:space:]\;]+) ]] || fail 'managed Core host could not be determined'
  case "${BASH_REMATCH[1]}" in
    0.0.0.0|127.0.0.1) core_host=127.0.0.1 ;;
    ::|::1) core_host='[::1]' ;;
    *) fail 'Core listener is outside the managed loopback health probe' ;;
  esac
}

core_user() {
  runuser -u "$INSTALL_USER" -- env -i "PATH=$PATH" "HOME=$target_home" "USER=$INSTALL_USER" "$@"
}

update_report() {
  [ -n "${update_id:-}" ] || return 0
  core_user python3 -I - "$state_dir" "$update_id" "$expected_version" "$1" <<'PYTHON'
import json, os, pathlib, sys, tempfile
state, update_id, version, status = sys.argv[1:]
folder = pathlib.Path(state) / "strategy-finder"
target = folder / "core-update.json"
current = json.loads(target.read_text(encoding="utf8"))
if current["update_id"] != update_id or current["target_version"] != version:
    raise SystemExit("Core update report no longer owns this installation")
error = None if status != "failed" else {
    "code": "installation_failed", "message": "Core installer did not complete",
}
fd, stage = tempfile.mkstemp(prefix=".core-update-", dir=folder)
try:
    with os.fdopen(fd, "w", encoding="utf8") as stream:
        json.dump(dict(update_id=update_id, target_version=version, state=status, error=error), stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stage, target)
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
finally:
    pathlib.Path(stage).unlink(missing_ok=True)
PYTHON
}

prepare_ordinary() {
  local source="$1" wheel="$2" delivery="$3"
  core_user /bin/bash -s -- "$install_dir" "$stage" "$venv" "$source" "$wheel" "$delivery" "$expected_version" <<'USER_PREPARE'
set -Eeuo pipefail
umask 077
installed="$1"; stage="$2"; venv="$3"; source="$4"; wheel="$5"; delivery="$6"; version="$7"
mkdir -- "$stage"
venv_owned=0
trap 'rc=$?; if [ "$rc" -ne 0 ]; then rm -rf --one-file-system -- "$stage"; if [ "$venv_owned" = 1 ]; then rm -rf --one-file-system -- "$venv"; fi; fi' EXIT
if [ -n "$delivery" ]; then
  bundle="$stage/.delivery"
  mkdir -- "$bundle"
  cp -- "$delivery"/SHA256SUMS "$delivery"/gp_access_control_plane-"$version"*.whl \
     "$delivery"/gp_access_control_plane-"$version".tar.gz "$delivery"/core-*.tar.gz "$bundle"
  "$installed/.venv/bin/python" -I -c \
    'from pathlib import Path; import sys; from gp_control_plane.core_supply import _valid_size; _valid_size(Path(sys.argv[1]), sys.argv[2])' \
    "$bundle" "$version"
  archives=("$bundle"/core-*.tar.gz)
  [ "${#archives[@]}" -eq 1 ]
  tar --extract --gzip --no-same-owner --no-same-permissions --file "${archives[0]}" -C "$stage"
  wheel="$bundle/gp_access_control_plane-$version-py3-none-any.whl"
else
  tar -C "$source" --exclude=.git -cf - . | tar --no-same-owner --no-same-permissions -xf - -C "$stage"
fi
[ ! -e "$stage/.venv" ] && [ ! -L "$stage/.venv" ]
mkdir -- "$venv"
venv_owned=1
python3 -m venv "$venv"
export PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1
if [ -z "$wheel" ]; then
  mkdir -- "$stage/.delivery"
  "$venv/bin/python" -I -m pip wheel --no-deps --wheel-dir "$stage/.delivery" "$stage"
  wheel="$stage/.delivery/gp_access_control_plane-$version-py3-none-any.whl"
fi
"$venv/bin/python" -I -m pip install --only-binary=:all: --require-hashes -r "$stage/requirements/a5-transport.lock"
"$venv/bin/python" -I -m pip install --no-deps "$wheel"
"$venv/bin/python" -I -m pip check
"$venv/bin/python" -I -c 'import sys; from gp_control_plane import __version__; from gp_control_plane.http.api_v2.app import _contract; assert __version__ == sys.argv[1]; assert _contract()' "$version"
rm -rf -- "$stage/.delivery"
ln -s -- "$venv" "$stage/.venv"
USER_PREPARE
}

activate_ordinary() {
  systemctl stop "$CORE_UNIT"
  if systemctl is-active --quiet "$CORE_UNIT"; then fail 'Core did not stop'; fi
  core_user /bin/bash -s -- "$install_dir" "$stage" "$previous" <<'USER_SWITCH'
set -Eeuo pipefail
current="$1"; stage="$2"; previous="$3"
[ -d "$current" ] && [ ! -L "$current" ] && [ -d "$stage" ] && [ ! -L "$stage" ]
[ ! -e "$previous" ] && [ ! -L "$previous" ]
mv -T -- "$current" "$previous"
if ! mv -T -- "$stage" "$current"; then
  mv -T -- "$previous" "$current"
  exit 1
fi
USER_SWITCH
  if [ -n "${manual_helper_source:-}" ]; then
    install -m 0755 "$manual_helper_source/scripts/gp-root-helper.sh" /usr/local/libexec/gp-control-plane/gp-root-helper.new
    mv -T -- /usr/local/libexec/gp-control-plane/gp-root-helper.new /usr/local/libexec/gp-control-plane/gp-root-helper
    install -m 0755 "$manual_helper_source/scripts/install-linux.sh" /usr/local/libexec/gp-control-plane/install-linux.sh.new
    mv -T -- /usr/local/libexec/gp-control-plane/install-linux.sh.new /usr/local/libexec/gp-control-plane/install-linux.sh
  fi
  systemctl start "$CORE_UNIT"
  local attempt pid
  for attempt in {1..50}; do
    pid="$(systemctl show -p MainPID --value "$CORE_UNIT")"
    if [[ "$pid" =~ ^[1-9][0-9]*$ ]] &&
       core_user python3 -I - "$core_host" "$core_port" "$pid" "$expected_version" <<'HEALTH'
import json, sys, urllib.request
host, port, pid, version = sys.argv[1:]
request = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with request.open(f"http://{host}:{port}/api/health", timeout=1) as response:
    if (response.headers.get("X-GP-Core-Pid") != pid
            or response.headers.get("X-GP-Core-Version") != version
            or json.loads(response.read(1024)) != {"status": "ok"}):
        raise SystemExit(1)
HEALTH
    then
      update_report succeeded
      core_user /bin/bash -s -- "$gp_root" "$previous" <<'USER_CLEAN' || printf '%s\n' 'WARNING: previous program cleanup failed' >&2
set -Eeuo pipefail
gp_root="$1"; previous="$2"
old_venv="$(readlink -- "$previous/.venv" || true)"
rm -rf --one-file-system -- "$previous"
if [[ "$old_venv" == "$gp_root"/.gp-core-venv.* ]] &&
   [[ "${old_venv##*.}" =~ ^[a-f0-9]{32}$ ]] && [ -d "$old_venv" ] && [ ! -L "$old_venv" ]; then
  rm -rf --one-file-system -- "$old_venv"
fi
USER_CLEAN
      printf '%s\n' 'status=success phase=ordinary-install'
      return
    fi
    sleep 0.2
  done
  fail 'new Core version did not pass the managed listener check'
}

ordinary_install() {
  find_managed_core
  local gate_parent=/run/gp-control-plane gate=/run/gp-control-plane/gates
  for dir in "$gate_parent" "$gate"; do
    [ ! -L "$dir" ] || fail 'unsafe Core maintenance directory'
    [ -e "$dir" ] || mkdir -m 0700 -- "$dir"
    [ -d "$dir" ] && [ "$(stat -c %u -- "$dir")" = 0 ] || fail 'unsafe Core maintenance owner'
  done
  [ "$(stat -c '%u:%g:%a' "$gate")" = 0:0:700 ] || fail 'unsafe Core maintenance gate'
  [ ! -L "$gate/discovery-update.lock" ] || fail 'unsafe Core maintenance lock'
  [ -e "$gate/discovery-update.lock" ] || ( umask 077; set -C; : >"$gate/discovery-update.lock" )
  [ -f "$gate/discovery-update.lock" ] && [ "$(stat -c '%u:%g:%a' "$gate/discovery-update.lock")" = 0:0:600 ] || fail 'unsafe Core maintenance lock'
  exec 9<>"$gate/discovery-update.lock"
  flock -n -x 9 || fail 'Core discovery or maintenance is active'
  token="$(cat /proc/sys/kernel/random/uuid | tr -d '-')"
  [[ "$token" =~ ^[a-f0-9]{32}$ ]] || fail 'could not allocate an installer identity'
  stage="$gp_root/.gp-core-stage.$token"
  venv="$gp_root/.gp-core-venv.$token"
  previous="$gp_root/.gp-core-previous.$token"
  phase=ordinary-preparation
  prepare_ordinary "$@"
  phase=ordinary-activation
  activate_ordinary
}

if [ "${1:-}" = --prepared-update ]; then
  [ "$#" -eq 3 ] || usage
  update_id="$2"; expected_version="$3"
  [[ "$update_id" =~ ^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$ ]] || fail 'invalid update UUID'
  [[ "$expected_version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)((a|b|rc)(0|[1-9][0-9]*))?(\.post(0|[1-9][0-9]*))?$ ]] || fail 'invalid update version'
  [ "${#expected_version}" -le 64 ] || fail 'invalid update version length'
  grep -Eq '/gp-control-plane-core-update\.scope$' /proc/self/cgroup || fail 'installer must run in its managed scope'
  find_managed_core
  trap 'rc=$?; if [ "$rc" -ne 0 ]; then update_report failed || true; fi' EXIT
  core_user "$install_dir/.venv/bin/python" -I -c \
    'import sys; from gp_control_plane import __version__; from gp_control_plane.application.core_update import version_key; assert version_key(sys.argv[1]) >= version_key(__version__)' "$expected_version"
  ordinary_install '' '' "$state_dir/strategy-finder/downloads/$expected_version"
  exit
fi

SOURCE_DIR= INSTALL_USER= TAG= CANDIDATE_SHA= INSTALL_WEB= INSTALL_MODE=ordinary
while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-dir|--install-user|--tag|--candidate-sha|--web|--mode)
      [ "$#" -ge 2 ] || usage; value="$2"; case "$1" in --source-dir) SOURCE_DIR="$value";; --install-user) INSTALL_USER="$value";; --tag) TAG="$value";; --candidate-sha) CANDIDATE_SHA="$value";; --web) INSTALL_WEB="$value";; --mode) INSTALL_MODE="$value";; esac; shift 2 ;;
    *) usage ;;
  esac
done
case "$INSTALL_USER" in ''|root|*[!A-Za-z0-9_-]*) fail 'install user is invalid';; esac
case "$INSTALL_WEB" in on|off) ;; *) fail 'web mode must be on or off';; esac
[ "$INSTALL_WEB" = off ] || fail 'Core-only installation; install GP Plane Web separately'
case "$INSTALL_MODE" in ordinary|clean) ;; *) fail 'mode must be ordinary or clean';; esac
[ -d "$SOURCE_DIR/.git" ] && [ ! -L "$SOURCE_DIR" ] || fail 'source directory is unsafe'
[ -z "$TAG" ] || [ -z "$CANDIDATE_SHA" ] || usage
[ -n "$TAG" ] || [ -n "$CANDIDATE_SHA" ] || usage
SOURCE_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
printf '%s\n' "$SOURCE_COMMIT" | grep -Eq '^[0-9a-f]{40}$' || fail 'source checkout has an invalid HEAD commit'
if [ -n "$TAG" ]; then
  printf '%s\n' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-alpha\.[1-9][0-9]*)?$' || fail 'tag must be an exact stable or alpha release tag'
  [ "$(git -C "$SOURCE_DIR" cat-file -t "refs/tags/$TAG" 2>/dev/null || true)" = tag ] || fail 'source tag must be annotated'
  [ "$SOURCE_COMMIT" = "$(git -C "$SOURCE_DIR" rev-parse "refs/tags/$TAG^{commit}")" ] || fail 'source checkout does not match the exact tag'
  INSTALL_REF="$TAG"
else
  printf '%s\n' "$CANDIDATE_SHA" | grep -Eq '^[0-9a-f]{40}$' || fail 'candidate SHA must be a full lowercase commit SHA'
  [ "$SOURCE_COMMIT" = "$CANDIDATE_SHA" ] || fail 'source checkout does not match the exact candidate SHA'
  INSTALL_REF="candidate:$CANDIDATE_SHA"
fi
[ -z "$(git -C "$SOURCE_DIR" status --porcelain)" ] || fail 'exact-tag source tree is not clean'
INSTALL_COMMIT="$SOURCE_COMMIT"
target_home="$(getent passwd "$INSTALL_USER" | cut -d: -f6)"
[ -n "$target_home" ] && [ -d "$target_home" ] || fail 'install-user home is unavailable'
gp_root="$target_home/gp"; install_dir="$gp_root/GP-access-control-plane"; state_parent="$gp_root/.GP-access-control-plane.data"; state_dir="$state_parent/state"
[ "$target_home" = "$(readlink -f -- "$target_home")" ] && [ ! -L "$target_home" ] || fail 'install-user home is not canonical'
if [ -e "$gp_root" ] || [ -L "$gp_root" ]; then
  [ -d "$gp_root" ] && [ ! -L "$gp_root" ] && [ "$gp_root" = "$(readlink -f -- "$gp_root")" ] || fail 'managed GP root is not canonical'
else
  :
fi
if [ -e "$install_dir" ] || [ -L "$install_dir" ]; then
  [ -d "$install_dir" ] && [ ! -L "$install_dir" ] && [ "$install_dir" = "$(readlink -f -- "$install_dir")" ] || fail 'managed GP install directory is not canonical'
fi
if [ "$INSTALL_MODE" = ordinary ]; then
  if [ -e "$install_dir" ]; then
    expected_version="$(core_user python3 -I -c 'from pathlib import Path; import sys,tomllib; print(tomllib.loads((Path(sys.argv[1])/"pyproject.toml").read_text())["project"]["version"])' "$SOURCE_DIR")"
    manual_helper_source="$SOURCE_DIR"
    ordinary_install "$SOURCE_DIR" '' ''
    exit
  fi
  [ ! -e "$state_parent" ] && [ ! -L "$state_parent" ] || fail 'managed state exists without a program; explicit clean installation is required to discard it'
fi
# Finish dependency preparation while the old GP is still running. No network
# or upstream installation script is needed after the destructive boundary.
phase=preparation
case "$(uname -s):$(uname -m)" in
  Linux:x86_64|Linux:i686|Linux:i386|Linux:aarch64|Linux:armv7l|Linux:armv6l) ;;
  *) fail 'unsupported Linux architecture; previous GP was preserved';;
esac
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl dnsutils git iproute2 ipset iptables nftables python3 python3-pip python3-venv sudo
group="$(id -gn "$INSTALL_USER")"
prepare_dir="$(mktemp -d /tmp/gp-install-prepare.XXXXXX)"
publish_dir=
cleanup_preparation() {
  rm -rf --one-file-system -- "$prepare_dir"
  [ -z "$publish_dir" ] || rm -rf --one-file-system -- "$publish_dir"
}
trap cleanup_preparation EXIT
chmod 0755 "$prepare_dir"
bash "$SOURCE_DIR/scripts/install-zapret2.sh" --prepare-dir "$prepare_dir"
runuser -u "$INSTALL_USER" -- python3 "$SOURCE_DIR/scripts/prepare-zapret2.py" --probe --destination "$prepare_dir/runtime"
install -d -m 0700 -o "$INSTALL_USER" -g "$group" "$prepare_dir/python"
install -m 0644 "$SOURCE_DIR/requirements/a5-transport.lock" "$prepare_dir/python/runtime.lock"
cmp -s "$SOURCE_DIR/requirements/a5-transport.lock" "$prepare_dir/python/runtime.lock" || fail 'staged Core runtime lock mismatch; previous GP was preserved'
install -d -m 0700 -o "$INSTALL_USER" -g "$group" "$prepare_dir/python/pip-cache" "$prepare_dir/python/pip-tmp"
# pip must not inherit user/site/global config or redirecting PIP_* options.
# Preserve only explicit network/proxy trust settings; all writes stay in the
# selected venv or this user-owned preparation namespace (including build children).
pip_user() {
  local pip_python="$1"; shift
  local pip_proxy
  local -a pip_environment=("PATH=$PATH" "HOME=$target_home" "USER=$INSTALL_USER" "LOGNAME=$INSTALL_USER"
    "PIP_CONFIG_FILE=/dev/null" "PIP_CACHE_DIR=$prepare_dir/python/pip-cache"
    "TMPDIR=$prepare_dir/python/pip-tmp" "TMP=$prepare_dir/python/pip-tmp" "TEMP=$prepare_dir/python/pip-tmp"
    "PIP_NO_INPUT=1" "PIP_DISABLE_PIP_VERSION_CHECK=1")
  for pip_proxy in http_proxy https_proxy all_proxy no_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY SSL_CERT_FILE REQUESTS_CA_BUNDLE; do
    if [[ -v "$pip_proxy" ]]; then pip_environment+=("$pip_proxy=${!pip_proxy}"); fi
  done
  runuser -u "$INSTALL_USER" -- env -i "${pip_environment[@]}" "$pip_python" -I -m pip "$@"
}
runuser -u "$INSTALL_USER" -- python3 -m venv "$prepare_dir/python/venv"
pip_user "$prepare_dir/python/venv/bin/python" download --only-binary=:all: --require-hashes --dest "$prepare_dir/python/wheels" -r "$prepare_dir/python/runtime.lock"
pip_user "$prepare_dir/python/venv/bin/python" wheel --no-deps --wheel-dir "$prepare_dir/python/wheels" "$SOURCE_DIR"
pip_user "$prepare_dir/python/venv/bin/python" install --no-index --find-links "$prepare_dir/python/wheels" --require-hashes -r "$prepare_dir/python/runtime.lock"
pip_user "$prepare_dir/python/venv/bin/python" install --no-index --no-deps --find-links "$prepare_dir/python/wheels" gp-access-control-plane
pip_user "$prepare_dir/python/venv/bin/python" check
# Match the prepared package to the already validated source, including future
# releases. A stale or mismatched wheel must fail before stopping the old GP.
runuser -u "$INSTALL_USER" -- "$prepare_dir/python/venv/bin/python" -c 'import sys, tomllib, bottle, cheroot; from pathlib import Path; from importlib.metadata import version; from gp_control_plane import __version__; from gp_control_plane.web.docs import openapi_json_bytes; expected = tomllib.loads((Path(sys.argv[1]) / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]; (__version__ == expected == version("gp-access-control-plane")) or sys.exit("ERROR: prepared GP version does not match validated source"); openapi_json_bytes() or sys.exit("ERROR: prepared GP OpenAPI document is empty")' "$SOURCE_DIR"
# A GP-owned versioned runtime keeps arbitrary /opt/zapret2 git/release layouts
# and their settings intact. Existing files are checked, never overwritten.
zapret_parent=/opt/gp-zapret2
zapret_dir="$zapret_parent/v1.0.5.2-$(uname -m)"
if [ -e "$zapret_parent" ] || [ -L "$zapret_parent" ]; then
  [ -d "$zapret_parent" ] && [ ! -L "$zapret_parent" ] && [ "$(readlink -f -- "$zapret_parent")" = "$zapret_parent" ] && [ "$(stat -c '%u:%a' "$zapret_parent")" = 0:755 ] || fail 'managed zapret2 parent is unsafe; nothing was removed'
else
  install -d -m 0755 -o root -g root "$zapret_parent"
fi
if [ -e "$zapret_dir" ] || [ -L "$zapret_dir" ]; then
  python3 "$SOURCE_DIR/scripts/prepare-zapret2.py" --destination "$prepare_dir/runtime" --verify-installed "$zapret_dir"
else
  publish_dir="$(mktemp -d "$zapret_parent/.prepare.XXXXXX")"
  cp -a -- "$prepare_dir/runtime" "$publish_dir/runtime"
  mv -T -- "$publish_dir/runtime" "$zapret_dir"
fi
runuser -u "$INSTALL_USER" -- python3 "$SOURCE_DIR/scripts/prepare-zapret2.py" --probe --destination "$zapret_dir"
python3 "$SOURCE_DIR/scripts/check-core-listener.py" --install-dir "$install_dir" --install-user "$INSTALL_USER"
# Independent Web files and units are outside the Core installation.
phase=activation
stop_unit() {
  local state
  state=$(systemctl show --property=LoadState --value "$1") || fail 'could not identify legacy GP service'
  if [ "$state" != not-found ]; then
    systemctl disable --now "$1" || fail 'legacy GP service did not stop; files were preserved'
    if systemctl is-active --quiet "$1"; then fail 'legacy GP service is still active; files were preserved'; fi
  fi
}
stop_unit gp-control-plane-core.service
rm -f -- /etc/systemd/system/gp-control-plane-core.service
rm -f -- /etc/default/gp-control-plane-core /etc/default/gp-control-plane-root-helper /etc/sudoers.d/gp-control-plane-root-helper
rm -rf --one-file-system -- /usr/local/libexec/gp-control-plane /run/gp-control-plane "$install_dir" "$state_parent"
systemctl daemon-reload
install -d -o "$INSTALL_USER" -g "$group" "$gp_root" "$install_dir"
tar -C "$SOURCE_DIR" --exclude=.git -cf - . | tar -C "$install_dir" -xf -
chown -R "$INSTALL_USER:$group" "$install_dir"
install -d -m 0755 /usr/local/libexec/gp-control-plane
cat > /usr/local/libexec/gp-control-plane/nfqws2 <<EOF
#!/bin/sh
exec $zapret_dir/nfq2/nfqws2 "\$@"
EOF
cat > /usr/local/libexec/gp-control-plane/blockcheck2.sh <<EOF
#!/bin/sh
exec $zapret_dir/blockcheck2.sh "\$@"
EOF
chmod 0755 /usr/local/libexec/gp-control-plane/nfqws2 /usr/local/libexec/gp-control-plane/blockcheck2.sh
install -d -m 0700 -o "$INSTALL_USER" -g "$group" "$state_parent" "$state_dir"
runuser -u "$INSTALL_USER" -- python3 -m venv "$install_dir/.venv"
pip_user "$install_dir/.venv/bin/python" install --no-index --find-links "$prepare_dir/python/wheels" --require-hashes -r "$prepare_dir/python/runtime.lock"
pip_user "$install_dir/.venv/bin/python" install --no-index --no-deps --find-links "$prepare_dir/python/wheels" gp-access-control-plane
pip_user "$install_dir/.venv/bin/python" check
# The v2fly cache is disposable service data.  A network failure must not undo a
# successful clean install: the authenticated Web action can retry it later.
if ! runuser -u "$INSTALL_USER" -- env GP_STATE_DIR="$state_dir" "$install_dir/.venv/bin/gp-control-plane" domain-sources prepare-v2fly; then
  printf '%s\n' 'WARNING: v2fly catalog was not prepared; start the service and retry from the Web interface.' >&2
fi
install -d -m 0755 /usr/local/libexec/gp-control-plane
install -m 0755 "$SOURCE_DIR/scripts/gp-root-helper.sh" /usr/local/libexec/gp-control-plane/gp-root-helper
install -m 0755 "$SOURCE_DIR/scripts/install-linux.sh" /usr/local/libexec/gp-control-plane/install-linux.sh
cat > /etc/default/gp-control-plane-root-helper <<EOF
# Managed by the GP clean installer, consumed by the existing root helper.
ZAPRET_DIR='$zapret_dir'
EOF
chmod 0644 /etc/default/gp-control-plane-root-helper
cat > /etc/sudoers.d/gp-control-plane-root-helper <<EOF
# Managed by GP clean installer; runtime discovery only.
$INSTALL_USER ALL=(root) NOPASSWD: /usr/local/libexec/gp-control-plane/gp-root-helper *
EOF
visudo -cf /etc/sudoers.d/gp-control-plane-root-helper >/dev/null || fail 'runtime sudoers rule is invalid'
chmod 0440 /etc/sudoers.d/gp-control-plane-root-helper
cat > /etc/default/gp-control-plane-core <<EOF
GP_INSTALL_DIR='$install_dir'
GP_STATE_DIR='$state_dir'
GP_INSTALLED_REF='$INSTALL_REF'
GP_INSTALLED_COMMIT='$INSTALL_COMMIT'
GP_BLOCKCHECK2D='$zapret_dir/blockcheck2.d'
EOF
cat > /etc/systemd/system/gp-control-plane-core.service <<EOF
[Unit]
Description=GP Strategy Finder Core API
After=network-online.target
[Service]
User=$INSTALL_USER
WorkingDirectory=$install_dir
Environment=HOME=$target_home
Environment=PATH=/usr/local/libexec/gp-control-plane:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EnvironmentFile=/etc/default/gp-control-plane-core
Environment=GP_ROOT_HELPER=/usr/local/libexec/gp-control-plane/gp-root-helper
ExecStart=$install_dir/.venv/bin/gp-control-plane core --host 127.0.0.1 --port 8081
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload; systemctl enable --now gp-control-plane-core.service
printf '%s\n' 'status=success phase=fresh-install'
