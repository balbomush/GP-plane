#!/usr/bin/env bash
# This script is the sole elevated process of a clean installation.
set -Eeuo pipefail
PATH='/usr/sbin:/usr/bin:/sbin:/bin'
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
phase=validation
trap 'printf "ERROR: clean installation failed in phase=%s; before activation previous GP is preserved; after activation use the preserved vault for recovery\n" "$phase" >&2' ERR
usage() { printf '%s\n' 'usage: install-linux.sh --source-dir DIR --install-user USER (--tag vX.Y.Z|vX.Y.Z-alpha.N | --candidate-sha SHA) --web on|off --initial-install on|off' >&2; exit 64; }
[ "$(id -u)" -eq 0 ] || fail 'must be run by the bootstrap sudo process'
SOURCE_DIR= INSTALL_USER= TAG= CANDIDATE_SHA= INSTALL_WEB= INITIAL_INSTALL=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-dir|--install-user|--tag|--candidate-sha|--web|--initial-install)
      [ "$#" -ge 2 ] || usage; value="$2"; case "$1" in --source-dir) SOURCE_DIR="$value";; --install-user) INSTALL_USER="$value";; --tag) TAG="$value";; --candidate-sha) CANDIDATE_SHA="$value";; --web) INSTALL_WEB="$value";; --initial-install) INITIAL_INSTALL="$value";; esac; shift 2 ;;
    *) usage ;;
  esac
done
case "$INSTALL_USER" in ''|root|*[!A-Za-z0-9_-]*) fail 'install user is invalid';; esac
case "$INSTALL_WEB" in on|off) ;; *) fail 'web mode must be on or off';; esac
[ "$INSTALL_WEB" = off ] || fail 'Core-only installation; install GP Plane Web separately'
case "$INITIAL_INSTALL" in on|off) ;; *) fail 'initial-install must be on or off';; esac
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
elif [ "$INITIAL_INSTALL" != on ]; then
  fail 'managed GP root is unavailable for a vault restore'
fi
if [ -e "$install_dir" ] || [ -L "$install_dir" ]; then
  [ -d "$install_dir" ] && [ ! -L "$install_dir" ] && [ "$install_dir" = "$(readlink -f -- "$install_dir")" ] || fail 'managed GP install directory is not canonical'
fi
vault_tool="$SOURCE_DIR/scripts/clean-install-vault.py"
[ -f "$vault_tool" ] && [ ! -L "$vault_tool" ] || fail 'exact tag lacks the vault tool'
if [ "$INITIAL_INSTALL" = on ]; then
  for existing_state in "$state_parent" "$install_dir/build/state"; do
    [ ! -e "$existing_state" ] && [ ! -L "$existing_state" ] || fail 'initial install cannot discard existing data; prepare a vault and use the restore path'
  done
fi
# This executes as the install user; root neither reads nor deletes the vault/handoff.
if [ "$INITIAL_INSTALL" = off ]; then
  runuser -u "$INSTALL_USER" -- python3 "$vault_tool" --verify --home "$target_home" >/dev/null || fail 'vault is absent or corrupt; nothing was removed'
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
runuser -u "$INSTALL_USER" -- python3 -m venv "$prepare_dir/python/venv"
runuser -u "$INSTALL_USER" -- "$prepare_dir/python/venv/bin/python" -m pip download --only-binary=:all: --require-hashes --dest "$prepare_dir/python/wheels" -r "$prepare_dir/python/runtime.lock"
runuser -u "$INSTALL_USER" -- "$prepare_dir/python/venv/bin/python" -m pip wheel --no-deps --wheel-dir "$prepare_dir/python/wheels" "$SOURCE_DIR"
runuser -u "$INSTALL_USER" -- "$prepare_dir/python/venv/bin/python" -m pip install --no-index --find-links "$prepare_dir/python/wheels" --require-hashes -r "$prepare_dir/python/runtime.lock"
runuser -u "$INSTALL_USER" -- "$prepare_dir/python/venv/bin/python" -m pip install --no-index --no-deps --find-links "$prepare_dir/python/wheels" gp-access-control-plane
runuser -u "$INSTALL_USER" -- "$prepare_dir/python/venv/bin/python" -m pip check
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
# The independent Web is never stopped or removed by Core. Reject an unsupported
# installed consumer before activation, running its code only as its own user.
web_root="$gp_root/GP-plane-web"
if [ -e "$web_root" ] || [ -L "$web_root" ]; then
  [ -d "$web_root" ] && [ ! -L "$web_root" ] && [ "$(readlink -f -- "$web_root")" = "$web_root" ] || fail 'unsafe independent Web root; nothing stopped'
  [ -f "$web_root/.gp-web-owned" ] && [ ! -L "$web_root/.gp-web-owned" ] && [ "$(cat "$web_root/.gp-web-owned")" = 'GP Plane Web' ] || fail 'foreign independent Web root; nothing stopped'
  runuser -u "$INSTALL_USER" -- "$prepare_dir/python/venv/bin/python" -I -c 'from gp_control_plane.http.docs import openapi_json_bytes; print(openapi_json_bytes().decode("utf-8"))' |
    runuser -u "$INSTALL_USER" -- "$web_root/.venv/bin/python" -I -c 'import json,sys; from gp_control_plane_web.compatibility import validate_core_contract; validate_core_contract(json.load(sys.stdin))' || fail 'installed Web does not support prepared Core; nothing stopped'
fi
phase=activation
stop_unit() {
  local state
  state=$(systemctl show --property=LoadState --value "$1") || fail 'could not identify legacy GP service'
  if [ "$state" != not-found ]; then
    systemctl disable --now "$1" || fail 'legacy GP service did not stop; files were preserved'
    if systemctl is-active --quiet "$1"; then fail 'legacy GP service is still active; files were preserved'; fi
  fi
}
stop_unit gp-control-plane-web.service; stop_unit gp-control-plane-core.service
rm -f -- /etc/systemd/system/gp-control-plane-core.service /etc/systemd/system/gp-control-plane-web.service
rm -f -- /etc/default/gp-control-plane-install-profile /etc/default/gp-control-plane-core /etc/default/gp-control-plane-web /etc/default/gp-control-plane-root-helper /etc/sudoers.d/gp-control-plane-root-helper
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
runuser -u "$INSTALL_USER" -- "$install_dir/.venv/bin/python" -m pip install --no-index --find-links "$prepare_dir/python/wheels" --require-hashes -r "$prepare_dir/python/runtime.lock"
runuser -u "$INSTALL_USER" -- "$install_dir/.venv/bin/python" -m pip install --no-index --no-deps --find-links "$prepare_dir/python/wheels" gp-access-control-plane
runuser -u "$INSTALL_USER" -- "$install_dir/.venv/bin/python" -m pip check
# The application validates the pending vault ID and performs the semantic and
# SQLite checks itself. Root only invokes that user-owned operation.
if [ "$INITIAL_INSTALL" = off ]; then
  runuser -u "$INSTALL_USER" -- "$install_dir/.venv/bin/python" "$vault_tool" --restore --target-state-dir "$state_dir" --home "$target_home" \
    || fail 'clean-install vault restore failed; vault was preserved and services were not started'
fi
# The v2fly cache is disposable service data.  A network failure must not undo a
# successful clean install: the authenticated Web action can retry it later.
if ! runuser -u "$INSTALL_USER" -- env GP_STATE_DIR="$state_dir" "$install_dir/.venv/bin/gp-control-plane" domain-sources prepare-v2fly; then
  printf '%s\n' 'WARNING: v2fly catalog was not prepared; start the service and retry from the Web interface.' >&2
fi
install -d -m 0755 /usr/local/libexec/gp-control-plane
install -m 0755 "$install_dir/scripts/gp-root-helper.sh" /usr/local/libexec/gp-control-plane/gp-root-helper
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
