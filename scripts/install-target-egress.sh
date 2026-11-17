#!/bin/bash
# Installs or upgrades the egress proxy on the remote Docker target in ~/.ensemble/docker/target.json, from this Mac:
# egress-proxy's egress-install fetches the image here and loads it on the target, which needs no bash, gh or login.
#   bash <ensemble plugin directory>/scripts/install-target-egress.sh [--version X.Y.Z --digest sha256:<digest>]
#   curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install-target-egress.sh | bash
# ENSEMBLE_INSTALL_ROOT prefixes its system paths and the account home it reads the target from; only tests set it.
set -euo pipefail

# The egress-proxy release this installs on the Docker target, and the windroc-labs/egress-proxy commit whose
# egress-install runs: that release's commit, never a tag, which could move. Version and digest stay empty until a
# release with remote install exists; --version and --digest override them.
egress_version=
egress_digest=
egress_ref=e466ea01a03bf50d854b2b928cbd8838b5b5e20a

me=install-target-egress.sh
root=${ENSEMBLE_INSTALL_ROOT:-}
ssh_dir=$root/usr/local/libexec/ensemble/ssh
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

Installs or upgrades the egress proxy on the remote Docker target in ~/.ensemble/docker/target.json, through the
broker's ssh wrapper. Its install directory on this Mac is ~/.egress-target: edit config.local.yaml there, then
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

# The target's ssh:// host, by the broker's rule: the file holds exactly {"host": "ssh://[user@]host[:port]"}.
# Exit 3: no file.
target_host() {
  python3 -I -c '
import json, os, pwd, re, sys
path = sys.argv[1] + pwd.getpwuid(os.getuid()).pw_dir + "/.ensemble/docker/target.json"
pattern = re.compile(r"ssh://([A-Za-z0-9._-]{1,64}@)?[A-Za-z0-9][A-Za-z0-9.-]{0,252}(:[0-9]{1,5})?")
try:
    with open(path, "rb") as f:
        data = f.read((64 << 10) + 1)
except FileNotFoundError:
    sys.exit(3)
except OSError as error:
    sys.exit("%s: %s" % (path, error.strerror))
try:
    found = json.loads(data) if len(data) <= 64 << 10 else None
except ValueError:
    found = None
host = found.get("host") if isinstance(found, dict) and set(found) == {"host"} else None
match = pattern.fullmatch(host) if isinstance(host, str) else None
if match is None or match[2] is not None and not 1 <= int(match[2][1:]) <= 65535:
    sys.exit("%s: want {\"host\": \"ssh://[user@]host[:port]\"}" % path)
print(host)
' "$root"
}

main() {
  local host code=0
  parse_args "$@"
  host=$(target_host) || code=$?
  case $code in
    0) ;;
    3) die 2 "no remote Docker target is set (~/.ensemble/docker/target.json); scripts/install.sh --docker installs this Mac's egress proxy" ;;
    *) die 2 "invalid Docker target setting; fix it, then rerun" ;;
  esac
  [ -x "$ssh_dir/ssh" ] || die 1 "no ssh wrapper at $ssh_dir/ssh; reinstall the broker with scripts/install.sh --docker"
  command -v gh >/dev/null || die 1 "needs gh on PATH to fetch windroc-labs/egress-proxy's egress-install"
  command -v docker >/dev/null || die 1 "needs docker on PATH (Docker Desktop's CLI)"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  gh api -H 'Accept: application/vnd.github.raw' "repos/windroc-labs/egress-proxy/contents/egress-install?ref=$egress_ref" \
    </dev/null >"$tmp/egress-install" && [ -s "$tmp/egress-install" ] \
    || die 1 "cannot fetch windroc-labs/egress-proxy's egress-install at $egress_ref; check gh auth status and repository access, then rerun"
  say "==> egress proxy on $host: egress-proxy $egress_version"
  code=0
  env -u DOCKER_HOST -u DOCKER_CONTEXT PATH="$ssh_dir:$PATH" bash "$tmp/egress-install" --version "$egress_version" \
    --digest "$egress_digest" --dir "$target_dir" --docker-host "$host" </dev/null || code=$?
  [ "$code" -eq 0 ] || die "$code" "egress-install exited $code; see its message above"
  say "egress proxy installed on $host; install directory $target_dir"
  say "apply a config change: PATH=$ssh_dir:\$PATH $target_dir/start.sh"
  say "logs: PATH=$ssh_dir:\$PATH docker --host $host logs egress-proxy"
}

main "$@"
