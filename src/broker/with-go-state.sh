#!/bin/sh
# Runs a Go tool with all its local state in <repo>/.cache/go (gitignored), never the user's home.
# HOME and XDG_CONFIG_HOME too: Go's telemetry dir is os.UserConfigDir()/go/telemetry and no setting moves or disables it.
set -eu
state="$(cd "$(dirname "$0")/../.." && pwd)/.cache/go"
mkdir -p "$state/tmp" "$state/home"
export GOCACHE="$state/build" GOMODCACHE="$state/mod" GOPATH="$state/path" GOENV="$state/env" TMPDIR="$state/tmp" HOME="$state/home" XDG_CONFIG_HOME="$state/home/.config"
# A writable module cache, so rm -rf and git clean can delete it.
export GOFLAGS="${GOFLAGS:+$GOFLAGS }-modcacherw"
exec "$@"
