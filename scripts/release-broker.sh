#!/bin/sh
# Builds ensemble-broker from release tag v<X.Y.Z>'s commit on this Mac and uploads ensemble-broker-darwin-arm64.tar.gz
# and its sha256 file to that published Release, for src/broker/install.sh (AGENTS.md › Releasing).
set -eu
repo=windroc-labs/ensemble
asset_name=ensemble-broker-darwin-arm64.tar.gz
root=$(cd "$(dirname "$0")/.." && pwd)
fail() {
  echo "release-broker.sh: $*" >&2
  exit 1
}
case "$#:${1-}" in
  1:v[0-9]*.[0-9]*.[0-9]*) tag=$1 ;;
  *)
    echo "release-broker.sh: usage: sh release-broker.sh v<X.Y.Z>" >&2
    exit 2
    ;;
esac
platform=$(uname -sm)
[ "$platform" = "Darwin arm64" ] || fail "the broker asset is built on Darwin arm64, not $platform"
for tool in git gh go shasum; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool is required"
done
git -C "$root" fetch --quiet --no-tags "https://github.com/$repo.git" "refs/tags/$tag" \
  || fail "could not fetch tag $tag from $repo"
commit=$(git -C "$root" rev-parse --verify "FETCH_HEAD^{commit}")
version=$(git -C "$root" show "$commit:.claude-plugin/plugin.json" \
  | sed -n 's/^[[:space:]]*"version":[[:space:]]*"\([^"]*\)".*/\1/p' | sed -n 1p)
[ "$tag" = "v$version" ] || fail "tag $tag is not v$version from its .claude-plugin/plugin.json"
draft=$(gh release view "$tag" --repo "$repo" --json isDraft --jq .isDraft) \
  || fail "no published Release $tag in $repo: publish it first"
[ "$draft" = false ] || fail "Release $tag is a draft: publish it first"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/source" "$work/out"
git -C "$root" archive --output "$work/source.tar" "$commit"
tar -xf "$work/source.tar" -C "$work/source"
GOOS=darwin GOARCH=arm64 sh "$work/source/src/broker/build.sh" "$work/out/ensemble-broker"
tar -czf "$work/$asset_name" -C "$work/out" ensemble-broker
sum=$(shasum -a 256 <"$work/$asset_name")
printf '%s  %s\n' "${sum%% *}" "$asset_name" >"$work/$asset_name.sha256"
gh release upload "$tag" "$work/$asset_name" "$work/$asset_name.sha256" --repo "$repo" --clobber
echo "Uploaded $asset_name (sha256 ${sum%% *}) and $asset_name.sha256 to $tag, built from $commit"
