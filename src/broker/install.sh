#!/bin/sh
# Installs ensemble-broker root-owned and seeds the worker image definition once. Rerun to upgrade.
# Default: this plugin version's release asset, installed only after gh attestation verify passes.
# --build: builds and signs it locally with Go (build.sh).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
target=/usr/local/libexec/ensemble
worker="$HOME/.ensemble/docker/worker"
repo=windroc-labs/ensemble
asset_name=ensemble-broker-darwin-arm64.tar.gz
fail() {
  echo "install.sh: $*" >&2
  exit 1
}
case "$#:${1-}" in
  0:) mode=release ;;
  1:--build) mode=build ;;
  *)
    echo "install.sh: usage: sh install.sh [--build]" >&2
    exit 2
    ;;
esac
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if [ "$mode" = build ]; then
  command -v go >/dev/null 2>&1 || fail "go is required to build ensemble-broker (brew install go)"
  sh "$here/build.sh" "$work/ensemble-broker"
else
  platform=$(uname -sm)
  [ "$platform" = "Darwin arm64" ] \
    || fail "release assets are built for Darwin arm64 only, not $platform; build it locally: sh $here/install.sh --build"
  manifest="$(dirname "$(dirname "$here")")/.claude-plugin/plugin.json"
  version=$(sed -n 's/^[[:space:]]*"version":[[:space:]]*"\([^"]*\)".*/\1/p' "$manifest" 2>/dev/null | sed -n 1p)
  [ -n "$version" ] || fail "no version in $manifest"
  tag="v$version"
  command -v gh >/dev/null 2>&1 \
    || fail "gh is required to download and verify ensemble-broker (brew install gh, then gh auth login); or build it locally: sh $here/install.sh --build"
  asset="$work/$asset_name"
  if [ -n "${ENSEMBLE_BROKER_ASSET:-}" ]; then
    cp "$ENSEMBLE_BROKER_ASSET" "$asset"
  elif ! gh release download "$tag" --repo "$repo" --pattern "$asset_name" --dir "$work"; then
    fail "could not download $asset_name from release $tag of $repo (its release workflow may still be running, or the download failed): retry in a few minutes, or build locally: sh $here/install.sh --build"
  fi
  gh attestation verify "$asset" --repo "$repo" --signer-workflow "$repo/.github/workflows/release.yml" \
    --source-ref "refs/tags/$tag" --deny-self-hosted-runners \
    || fail "${ENSEMBLE_BROKER_ASSET:-$asset_name} failed provenance verification for $tag; nothing installed"
  tar -xzf "$asset" -C "$work" ensemble-broker
fi
if [ ! -e "$worker" ]; then
  mkdir -p "$worker"
  cp "$here/worker/Dockerfile" "$worker/Dockerfile"
  echo "Seeded $worker/Dockerfile"
fi
sudo install -d -o root -g wheel -m 0755 "$target"
sudo install -o root -g wheel -m 0755 "$work/ensemble-broker" "$target/ensemble-broker"
echo "Installed $target/ensemble-broker"
echo "Upgrade: rerun sh $here/install.sh"
