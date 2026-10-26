#!/bin/sh
# Builds ensemble-broker at <output>, signs it ad hoc with the hardened runtime and checks the signature.
# The one home of the build and signing identity: install.sh --build, CI and scripts/release-broker.sh call it.
set -eu
[ $# -eq 1 ] || { echo 'build.sh: usage: sh build.sh <output>' >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
case "$1" in /*) out=$1 ;; *) out="$PWD/$1" ;; esac
(cd "$here" && CGO_ENABLED=1 sh ./with-go-state.sh go build -trimpath -o "$out" .)
codesign --force --sign - --options runtime --identifier com.github.windroc.ensemble-broker "$out"
codesign --verify --strict "$out"
if ! codesign -dv "$out" 2>&1 | grep -q '^CodeDirectory.*flags=.*runtime'; then
  echo "build.sh: $out lacks the hardened runtime flag" >&2
  exit 1
fi
if codesign -d --entitlements - "$out" 2>&1 | grep -q get-task-allow; then
  echo "build.sh: $out has the get-task-allow entitlement" >&2
  exit 1
fi
