#!/bin/bash
# Installs or upgrades the egress proxy on the remote Docker target `ensemble-broker target` names, from this Mac:
# egress-proxy's egress-install fetches the image here and loads it on the target, which needs no bash, gh or login.
#   bash <ensemble plugin directory>/scripts/install-target-egress.sh [--version X.Y.Z --digest sha256:<digest>]
#   curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install-target-egress.sh | bash
# ENSEMBLE_INSTALL_ROOT prefixes the broker's path; only tests set it.
set -euo pipefail

# The pin: the egress-proxy release this installs on the Docker target, and the windroc-labs/egress-proxy commit whose
# egress-install runs, a commit and never a tag, which could move. No release has remote install yet: version and
# digest stay empty, and the commit is the unreleased remote install's; set all three to that release's together.
# --version and --digest override the version and digest.
egress_version=
egress_digest=
egress_ref=e466ea01a03bf50d854b2b928cbd8838b5b5e20a

me=install-target-egress.sh
root=${ENSEMBLE_INSTALL_ROOT:-}
broker=$root/usr/local/libexec/ensemble/ensemble-broker
target_dir=$HOME/.egress-target
synopsis="$me [--version X.Y.Z --digest sha256:<digest>]"
# Global, as the EXIT trap that removes it runs after main has returned.
tmp=

say() { printf '%s\n' "$*" >&2; }
die() {
  say "$me: $2"
  exit "$1"
}

usage() {
  cat <<EOF
usage: $synopsis

  bash <ensemble plugin directory>/scripts/install-target-egress.sh [flags]
  curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install-target-egress.sh | bash -s -- [flags]

  --version X.Y.Z             the egress-proxy release to install (default: the pinned one)
  --digest sha256:<digest>    its image index digest, from the release; given with --version

Installs or upgrades the egress proxy on the remote Docker target the broker names (ensemble-broker target),
through the broker's ssh wrapper. Its install directory on this Mac is ~/.egress-target: edit config.local.yaml there, then
run ~/.egress-target/start.sh. scripts/install.sh --docker installs this Mac's own proxy.
EOF
}

usage_error() {
  say "$me: $1"
  say "$me: usage: $synopsis"
  exit 2
}

parse_args() {
  local version= digest=
  while [ $# -gt 0 ]; do
    case $1 in
      -h | --help)
        usage
        exit 0
        ;;
      --version)
        [ $# -ge 2 ] || usage_error "--version needs a value"
        version=$2
        shift
        ;;
      --digest)
        [ $# -ge 2 ] || usage_error "--digest needs a value"
        digest=$2
        shift
        ;;
      *) usage_error "unknown argument: $1" ;;
    esac
    shift
  done
  if [ -n "$version$digest" ]; then
    [ -n "$version" ] && [ -n "$digest" ] || usage_error "--version and --digest go together"
    [[ $version =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || usage_error "--version $version is not X.Y.Z"
    [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || usage_error "--digest $digest is not sha256:<64 hex>"
    egress_version=$version
    egress_digest=$digest
  fi
  [ -n "$egress_version" ] \
    || die 2 "no egress-proxy release with remote install is pinned yet; pass --version X.Y.Z --digest sha256:<digest>"
}

# The broker's remote daemon: its endpoint and PATH on two lines. Exit 3: no remote daemon; 4: unreachable, endpoint and
# reason printed; 5: an invalid target setting, the broker's message printed; 6: the broker failed, why printed.
remote_target() {
  python3 -I -c '
import json, subprocess, sys
try:
    done = subprocess.run([sys.argv[1], "target"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
except (OSError, subprocess.SubprocessError) as error:
    print(error)
    sys.exit(6)
def last_object(text):
    lines = text.strip().splitlines()
    try:
        found = json.loads(lines[-1]) if lines else None
    except ValueError:
        found = None
    return found if isinstance(found, dict) else {}
if done.returncode:
    error = last_object(done.stderr).get("error")
    error = error if isinstance(error, dict) else {}
    print(error.get("message") or "exit status %d" % done.returncode)
    sys.exit(5 if error.get("code") == "docker_unreachable" else 6)
daemons = last_object(done.stdout).get("daemons")
remote = [d for d in daemons if isinstance(d, dict) and d.get("name") == "remote"] if isinstance(daemons, list) else []
if not remote:
    sys.exit(3)
endpoint, path, why = remote[0].get("endpoint"), remote[0].get("path"), remote[0].get("unreachable")
if not isinstance(endpoint, str) or not isinstance(path, str):
    print("malformed target output")
    sys.exit(6)
print(endpoint)
if why is not None:
    print(why)
    sys.exit(4)
print(path)
' "$broker"
}

main() {
  local found host path code=0
  parse_args "$@"
  python3 -I -c '' 2>/dev/null || die 1 "needs a working python3 on PATH, for the target setting and egress-install"
  found=$(remote_target) || code=$?
  host=${found%%$'\n'*}
  path=${found#*$'\n'}
  case $code in
    0) ;;
    3) die 2 "no remote Docker target is set (~/.ensemble/docker/target.json); scripts/install.sh --docker installs this Mac's egress proxy" ;;
    4) die 1 "cannot reach the remote Docker target $host: $path" ;;
    5)
      say "$me: $found"
      die 2 "invalid Docker target setting; fix it, then rerun"
      ;;
    *) die 1 "ensemble-broker target failed: $found; install or upgrade the broker with scripts/install.sh --docker" ;;
  esac
  command -v gh >/dev/null || die 1 "needs gh on PATH to fetch windroc-labs/egress-proxy's egress-install"
  command -v docker >/dev/null || die 1 "needs docker on PATH (Docker Desktop's CLI)"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  gh api -H 'Accept: application/vnd.github.raw' "repos/windroc-labs/egress-proxy/contents/egress-install?ref=$egress_ref" \
    </dev/null >"$tmp/egress-install" && [ -s "$tmp/egress-install" ] \
    || die 1 "cannot fetch windroc-labs/egress-proxy's egress-install at $egress_ref; check gh auth status and repository access, then rerun"
  say "==> egress proxy on $host: egress-proxy $egress_version"
  code=0
  env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_CONFIG PATH="$path:$PATH" bash "$tmp/egress-install" --version "$egress_version" \
    --digest "$egress_digest" --dir "$target_dir" --docker-host "$host" </dev/null || code=$?
  [ "$code" -eq 0 ] || die "$code" "egress-install exited $code; see its message above"
  say "egress proxy installed on $host; install directory $target_dir"
  say "apply a config change: PATH=$path:\$PATH $target_dir/start.sh"
  say "logs: PATH=$path:\$PATH docker --host $host logs egress-proxy"
}

main "$@"
