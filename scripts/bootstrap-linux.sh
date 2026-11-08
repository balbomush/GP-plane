#!/usr/bin/env bash
# Resolve the exact trusted release before invoking its Core installer.
set -Eeuo pipefail
REPO_URL="${GP_REPO_URL:-https://github.com/balbomush/GP-plane.git}"
TAG="${GP_BRANCH:-}"
INSTALL_MODE="${GP_INSTALL_MODE:-ordinary}"
INSTALL_WEB="${GP_INSTALL_WEB:-off}"
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || fail 'run as the GP install user'
printf '%s\n' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-alpha\.[1-9][0-9]*)?$' ||
  fail 'GP_BRANCH must name the exact annotated release tag'
case "$INSTALL_MODE" in ordinary|clean) ;; *) fail 'GP_INSTALL_MODE must be ordinary or clean';; esac
[ "$INSTALL_WEB" = off ] || fail 'Install GP Plane Web separately'
for tool in git sudo; do
  command -v "$tool" >/dev/null 2>&1 || fail "required command is unavailable: $tool"
done
source_dir="$(mktemp -d "${TMPDIR:-/tmp}/gp-core-install.XXXXXX")"
trap 'rm -rf -- "$source_dir"' EXIT
git clone --no-checkout --depth=1 --branch "$TAG" "$REPO_URL" "$source_dir"
[ "$(git -C "$source_dir" cat-file -t "refs/tags/$TAG" 2>/dev/null || true)" = tag ] || fail 'release tag must be annotated'
git -C "$source_dir" checkout --detach "$TAG"
[ "$(git -C "$source_dir" rev-parse HEAD)" = "$(git -C "$source_dir" rev-parse "refs/tags/$TAG^{commit}")" ] ||
  fail 'checkout does not match the annotated tag'
[ -z "$(git -C "$source_dir" status --porcelain)" ] || fail 'exact-tag source tree is not clean'
sudo -- "$source_dir/scripts/install-linux.sh" --source-dir "$source_dir" --install-user "$(id -un)" --tag "$TAG" --web off --mode "$INSTALL_MODE"
