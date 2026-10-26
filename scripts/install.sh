#!/bin/bash
# Installs ensemble on an Apple silicon Mac from nothing: dependencies, then your configuration
# (clients, Docker, OpenCode provider, the board) into ~/.ensemble/config.local.toml, then only what it needs.
# Asks sudo once, every login on the spot, and before writing your files. Rerun to finish or check.
#   curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install.sh | bash -s -- [flags]
# <tag>: the release the windroc marketplace pins for ensemble (README.md#install).
# ENSEMBLE_INSTALL_ROOT prefixes its system paths; only tests set it.
set -euo pipefail

me=install.sh
root=${ENSEMBLE_INSTALL_ROOT:-}
broker=$root/usr/local/libexec/ensemble/ensemble-broker
state_dir=$HOME/.ensemble/install
state_file=$state_dir/state
ledger=$state_dir/added
config=$HOME/.ensemble/config.local.toml
opencode_prefix=$HOME/.ensemble/opencode
opencode_executable=$opencode_prefix/bin/opencode
opencode_min=
ref=
# The broker's release (v<plugin version>) and whether install.sh downloads it or builds it: broker_plan.
release_tag=
broker_mode=
# Label contract v1; update only after checking the pinned repo's docs/label-contract.md.
egress_commit=122d7454d738db4a9b7e0c4a89d2cfa5ff564339
clients_flag=
clients=
docker_flag=
docker=
board_flag=
board=
board_tracker=
board_stages=
jobs_flag=
board_jobs_choice=
local_board_ready=
provider_flag=
url_flag=
model_flag=
context_flag=
provider=
url=
model=
context=
prev_provider=
prev_url=
prev_model=
prev_context=
key_stdin=
password_stdin=
sudo_mine=
keepalive=
feeder=
askpass_dir=
current=
answer=
token_screen=
app_pem=
app_id=
pkg=
user_path=$PATH
reload=
name_pattern='^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'
url_pattern='^https?://[A-Za-z0-9._~:/%+-]+/v1$'
model_pattern='^[A-Za-z0-9._:/-]{1,200}$'
context_pattern='^[1-9][0-9]{0,8}$'
synopsis='scripts/install.sh [--ref <ref>] [--clients <claude,codex,opencode>] [--docker | --no-docker] [--board <local|linear> | --no-board] [--launchd | --no-launchd] [--opencode-provider <name>] [--opencode-url <url>] [--opencode-model <model>] [--opencode-context <tokens>] [--opencode-key-stdin] [--password-stdin]'

say() { printf '%s\n' "$*" >&2; }
step() {
  current=$1
  say "==> $1"
}
skip() { say "    skip: $*"; }
die() {
  say "$me: $*"
  exit 1
}

usage() {
  cat <<EOF
usage: $synopsis

  curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install.sh | bash -s -- [flags]
  <tag>: the release the windroc marketplace pins for ensemble (README.md#install)

  --ref <ref>                 git branch or tag of windroc-labs/ensemble to install the plugin and broker from (default
                              <tag>); with claude or codex only <tag>, as they install from the marketplace; another
                              release tag downloads its broker, a branch builds it
  --clients <list>            comma-separated: claude (Claude Code), codex (Codex), opencode (OpenCode)
  --docker                    run workers in Docker: Docker Desktop, the broker and its logins
  --no-docker                 run workers on the local tmux Host; no Docker
  --board <local|linear>      set up a local or Linear board
  --no-board                 skip board setup (first-run default)
  --launchd, --no-launchd     choose router/promote jobs (docs/board.md)
  --opencode-provider <name>  the OpenAI-compatible provider OpenCode runs on, e.g. a LAN model server
  --opencode-url <url>        its base URL, ending /v1
  --opencode-model <model>    its model
  --opencode-context <tokens> its context window in tokens (default 65536)
  --opencode-key-stdin        read the provider's API key from stdin
  --password-stdin            read the sudo password from stdin (first line, before the key)

With --password-stdin or --opencode-key-stdin, stdin cannot carry the script:
  security find-generic-password -s <item> -w | bash -c "\$(curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install.sh)" scripts/install.sh --password-stdin

It shows a checklist only when clients or Docker has no flag or saved answer. Flags pre-tick their choices;
saved choices and existing config fill the other ticks. It installs the dependencies, asks what is still
unanswered, writes ~/.ensemble/config.local.toml,
then installs and logs in only what that needs. A rerun keeps the last answers unless a flag changes
them, and skips what is done.
EOF
}

usage_error() {
  say "install.sh: $1"
  say "install.sh: usage: $synopsis"
  exit 2
}

# The canonical form of a clients list (claude,codex,opencode order), or exit 1 when it is not one.
clients_of() {
  local item out=
  case $1 in '' | ,* | *, | *,,* | *[!a-z,]*) return 1 ;; esac
  for item in ${1//,/ }; do
    case $item in claude | codex | opencode) ;; *) return 1 ;; esac
  done
  for item in claude codex opencode; do
    case ",$1," in *",$item,"*) out=${out:+$out,}$item ;; esac
  done
  printf '%s' "$out"
}

valid_name() { [[ $1 =~ $name_pattern ]] && [ "$1" != ollama ]; }
valid_url() { [[ $1 =~ $url_pattern ]]; }
valid_model() { [[ $1 =~ $model_pattern ]]; }
valid_context() { [[ $1 =~ $context_pattern ]]; }

parse_args() {
  while [ $# -gt 0 ]; do
    case $1 in
      -h | --help)
        usage
        exit 0
        ;;
      --ref)
        [ $# -ge 2 ] || usage_error "--ref needs a value"
        case $2 in
          '' | -* | *[!A-Za-z0-9._/-]*) usage_error "--ref: not a git ref: $2" ;;
        esac
        ref=$2
        shift
        ;;
      --clients)
        [ $# -ge 2 ] || usage_error "--clients needs a value"
        clients_flag=$(clients_of "$2") || usage_error "--clients: want a comma-separated list of claude, codex, opencode, not $2"
        shift
        ;;
      --docker)
        [ "$docker_flag" != no ] || usage_error "--docker and --no-docker exclude each other"
        docker_flag=yes
        ;;
      --no-docker)
        [ "$docker_flag" != yes ] || usage_error "--docker and --no-docker exclude each other"
        docker_flag=no
        ;;
      --board)
        [ "$board_flag" != no ] || usage_error "--board and --no-board exclude each other"
        [ $# -ge 2 ] || usage_error "--board needs local or linear"
        case $2 in local | linear) ;; *) usage_error "--board needs local or linear" ;; esac
        board_flag=$2
        shift
        ;;
      --no-board)
        [ -z "$board_flag" ] || [ "$board_flag" = no ] || usage_error "--board and --no-board exclude each other"
        board_flag=no
        ;;
      --launchd)
        [ "$jobs_flag" != no ] || usage_error "--launchd and --no-launchd exclude each other"
        jobs_flag=yes
        ;;
      --no-launchd)
        [ "$jobs_flag" != yes ] || usage_error "--launchd and --no-launchd exclude each other"
        jobs_flag=no
        ;;
      --opencode-provider)
        [ $# -ge 2 ] && valid_name "$2" || usage_error "--opencode-provider: want 1-64 letters, digits, _ or -, not ollama"
        provider_flag=$2
        shift
        ;;
      --opencode-url)
        [ $# -ge 2 ] && valid_url "$2" || usage_error "--opencode-url: want http(s)://<host>[:<port>]/.../v1, no user info or quotes"
        url_flag=$2
        shift
        ;;
      --opencode-model)
        [ $# -ge 2 ] && valid_model "$2" || usage_error "--opencode-model: want 1-200 letters, digits, ., _, :, / or -"
        model_flag=$2
        shift
        ;;
      --opencode-context)
        [ $# -ge 2 ] && valid_context "$2" || usage_error "--opencode-context: want a token count, 1-999999999"
        context_flag=$2
        shift
        ;;
      --opencode-key-stdin) key_stdin=1 ;;
      --password-stdin) password_stdin=1 ;;
      *) usage_error "unknown argument: $1" ;;
    esac
    shift
  done
  if { [ -n "$password_stdin" ] || [ -n "$key_stdin" ]; } && [ -t 0 ]; then
    usage_error "--password-stdin and --opencode-key-stdin read a pipe, not a terminal"
  fi
}

opencode_flags() { [ -n "$provider_flag$url_flag$model_flag$context_flag$key_stdin" ]; }

# The flags' conflicts that flags and State already decide, before anything runs.
early_checks() {
  if opencode_flags && [ -n "$clients_flag" ]; then
    case ",$clients_flag," in *,opencode,*) ;; *) usage_error "--opencode-*: opencode is not among the clients" ;; esac
  fi
}

# A question on the terminal (fd 3); its answer lands in $answer.
ask() {
  printf '%s ' "$1" >&3
  IFS= read -r answer <&3 || die "no answer on the terminal"
}

ask_yn() {
  ask "$1 [y/N]"
  case $answer in
    [yY] | [yY][eE][sS]) return 0 ;;
  esac
  return 1
}

# Shows what would be written to $1 and asks before writing it.
consent() {
  printf 'install.sh will write to %s:\n%s\n' "$1" "$2" >&3
  ask_yn "Write it?"
}

# Exit 0 when $1 is a path strictly under $HOME, absolute, without `..` or a tab or newline.
under_home() {
  case $1 in
    "$HOME"/?*) ;;
    *) return 1 ;;
  esac
  case /$1/ in */../* | */./*) return 1 ;; esac
  case $1 in *$'\t'* | *$'\n'*) return 1 ;; esac
  return 0
}

# Inventory paths before dependencies exist; %q keeps each name on one line without evaluating it.
agent_tree_snapshot() {
  local path
  (
    umask 077
    agent_tree_find >"$state_dir/agent-tree.raw"
    while IFS= read -r -d '' path; do
      LC_ALL=C printf '%q\n' "$path"
    done <"$state_dir/agent-tree.raw" >"$state_dir/agent-tree.unsorted"
    LC_ALL=C sort "$state_dir/agent-tree.unsorted" >"$state_dir/agent-tree.paths.tmp"
  )
  mv "$state_dir/agent-tree.paths.tmp" "$state_dir/agent-tree.paths"
  rm -f "$state_dir/agent-tree.raw" "$state_dir/agent-tree.unsorted"
}

# Shared with install-cleanup.sh; find -path consumes a pattern even when the shell argument is quoted.
agent_tree_find() {
  local pattern=$HOME/.ensemble char
  for char in '\' '*' '?' '[' ']'; do
    pattern=${pattern//"$char"/\\$char}
  done
  find -H "$HOME/.ensemble" \( -path "$pattern/install" -o -path "$pattern/board" \) -prune -o -print0
}

# Exit 0 when the fields are a Ledger record the cleanup can act on.
ledger_valid() {
  local plain='^[A-Za-z0-9._@+][A-Za-z0-9._@+-]*$'
  case $1:$# in
    dir:2 | file:2 | profile-file:2 | board-dir:2) under_home "$2" ;;
    profile:3) under_home "$2" && case $3 in *$'\t'* | *$'\n'* | '') false ;; esac ;;
    docker-block:2) under_home "$2" && case ${2##*/} in .zshrc | .bashrc | .bash_profile | .zprofile) true ;; *) false ;; esac ;;
    homebrew:2) [ "$2" = "$root/opt/homebrew" ] ;;
    clt:1 | broker:1 | plugin-root:1) true ;;
    agent-tree:2) [ "$2" = "$HOME/.ensemble" ] ;;
    config:2) case $2 in created | backup) true ;; *) false ;; esac ;;
    formula:2) case $2 in gh | tmux | git | python | go | node) true ;; *) false ;; esac ;;
    cask:2) case $2 in iterm2 | claude-code | codex | docker-desktop) true ;; *) false ;; esac ;;
    claude-marketplace:2 | codex-marketplace:2) case $2 in windroc) true ;; *) false ;; esac ;;
    claude-plugin:2 | codex-plugin:2) case $2 in ensemble@windroc | updraft@windroc) true ;; *) false ;; esac ;;
    login:2) case $2 in gh | claude | codex) true ;; *) false ;; esac ;;
    launchd:2) case $2 in router | promote) true ;; *) false ;; esac ;;
    keychain:3) [[ $2 =~ $plain ]] && [[ $3 =~ $plain ]] ;;
    opencode-auth:3 | opencode-provider:3) under_home "$2" && [[ $3 =~ $name_pattern ]] ;;
    opencode-config:2) under_home "$2" && case ${2##*/} in opencode.json | opencode.jsonc | package.json | package-lock.json | .gitignore) true ;; *) false ;; esac ;;
    *) false ;;
  esac
}

# Records what the installer is about to create, once, for scripts/install-cleanup.sh.
ledger_add() {
  local line
  line=$(
    IFS=$'\t'
    printf '%s' "$*"
  )
  if ! ledger_valid "$@"; then
    say "    not recorded for cleanup: $*"
    return 0
  fi
  mkdir -p "$state_dir"
  if [ -f "$ledger" ] && grep -qxF -- "$line" "$ledger"; then return 0; fi
  printf '%s\n' "$line" >>"$ledger"
}

recorded() { [ -f "$ledger" ] && grep -qxF -- "$(
  IFS=$'\t'
  printf '%s' "$*"
)" "$ledger"; }

# Exit 0 when absolute path $1 is confirmed missing, 1 when it is there, 2 when it cannot tell (macOS may deny it, or
# $1 is relative): `test -e` is false on any lstat failure, so missing means its parent directory is missing too, or
# lists without it. Shell only: python3 may be the Command Line Tools' failing shim until homebrew installs them.
path_state() {
  local parent=${1%/*} listing rc=0
  if [ -e "$1" ] || [ -L "$1" ]; then return 1; fi
  case $1 in /*[!/]) ;; *) return 2 ;; esac
  [ -n "$parent" ] || parent=/
  if [ ! -e "$parent" ] && [ ! -L "$parent" ]; then
    path_state "$parent" || rc=$?
    return "$rc"
  fi
  [ -d "$parent" ] || return 2
  listing=$(ls -A -- "$parent" 2>/dev/null) || return 2
  grep -qxF -- "${1##*/}" <<<"$listing" || rc=$?
  [ "$rc" = 1 ] || return 2
}

# Exit 0 when path $1 is confirmed missing; a path macOS may deny counts as there (it may be the user's: never
# recorded, so never removed by the cleanup) and prints one line.
absent() {
  local rc=0
  path_state "$1" || rc=$?
  if [ "$rc" = 2 ]; then say "    not recorded for cleanup: $1: cannot tell whether it is there"; fi
  [ "$rc" = 0 ]
}

# Records directory $1 when it is confirmed missing.
record_dir() { if absent "$1"; then ledger_add dir "$1"; fi; }

# Records file $1 when it is confirmed missing.
record_file() { if absent "$1"; then ledger_add file "$1"; fi; }

# Per-user files under ~/Library that Docker Desktop and iTerm2 write once running, from their casks' zap stanzas:
# directories, then files (preferences plists, cookies).
docker_dirs=("Application Support/Docker Desktop" "Application Support/docker-secrets-engine"
  "Application Support/com.docker.docker" "Application Support/com.bugsnag.Bugsnag"
  "Application Support/com.bugsnag.Bugsnag/com.docker.docker" "Caches/com.docker.docker" "Caches/Docker Desktop"
  "Caches/docker-secrets-engine" "Caches/com.plausiblelabs.crashreporter.data"
  "Caches/com.plausiblelabs.crashreporter.data/com.docker.docker" "Caches/KSCrashReports" "Caches/KSCrashReports/Docker"
  "Containers/com.docker.helper" "Application Scripts/com.docker.helper" "Application Scripts/group.com.docker"
  "HTTPStorages/com.docker.docker" "Logs/Docker Desktop" "Saved Application State/com.electron.docker-frontend.savedState"
  "Saved Application State/com.electron.dockerdesktop.savedState")
docker_files=("HTTPStorages/com.docker.docker.binarycookies" "Preferences/com.docker.docker.plist"
  "Preferences/com.electron.docker-frontend.plist" "Preferences/com.electron.dockerdesktop.plist")
iterm_dirs=("Application Support/iTerm2" "Application Support/iTerm" "Caches/com.googlecode.iterm2"
  "HTTPStorages/com.googlecode.iterm2" "WebKit/com.googlecode.iterm2" "Saved Application State/com.googlecode.iterm2.savedState"
  "Containers/com.googlecode.iterm2.iTermFileProvider" "Containers/iTermAI"
  "Application Scripts/com.googlecode.iterm2.iTermFileProvider")
iterm_files=("Preferences/com.googlecode.iterm2.plist" "Preferences/com.googlecode.iterm2.private.plist"
  "Cookies/com.googlecode.iterm2.binarycookies" "HTTPStorages/com.googlecode.iterm2.binarycookies")

# Records each of the paths after $1 under ~/Library, $1 the kind: dir or file.
record_library() {
  local kind=$1 path
  shift
  for path in "$@"; do
    if [ "$kind" = dir ]; then record_dir "$HOME/Library/$path"; else record_file "$HOME/Library/$path"; fi
  done
}

# Records keychain item $1 / $2 when it does not exist yet.
record_item() {
  security find-generic-password -s "$1" -a "$2" </dev/null >/dev/null 2>&1 || ledger_add keychain "$1" "$2"
}

state_get() {
  local key value
  [ -f "$state_file" ] || return 0
  while IFS='=' read -r key value; do
    if [ "$key" = "$1" ]; then printf '%s' "$value"; fi
  done <"$state_file"
}

state_set() {
  local key value
  if [ -f "$state_file" ] && [ "$(state_get "$1")" = "$2" ]; then return 0; fi
  mkdir -p "$state_dir"
  {
    if [ -f "$state_file" ]; then
      while IFS='=' read -r key value; do
        if [ "$key" != "$1" ]; then printf '%s=%s\n' "$key" "$value"; fi
      done <"$state_file"
    fi
    printf '%s=%s\n' "$1" "$2"
  } >"$state_file.tmp"
  mv "$state_file.tmp" "$state_file"
}

on_exit() {
  local rc=$?
  token_screen_leave
  sudo_end
  if [ "$rc" -ne 0 ] && [ -n "$current" ]; then
    say "$me: $current failed; rerun the same command to continue"
  fi
  if [ -n "$reload" ]; then
    case $reload in "$HOME"/*) reload="~/${reload#"$HOME"/}" ;; esac
    say "$me: in this terminal, run source $reload first (or open a new one): its PATH lacks what install.sh added to $reload"
  fi
}

# Reads the sudo password once (stdin's first line, else the terminal) and checks it. A FIFO then
# hands it from memory to the askpass helper of every later sudo -A, Homebrew's included (it passes
# -A when SUDO_ASKPASS is set): macOS's per-terminal tickets miss some of those sudo calls.
sudo_once() {
  local password= tries=0
  step sudo
  sudo -k </dev/null >/dev/null 2>&1 || true
  sudo_mine=1
  if [ -n "$password_stdin" ]; then
    IFS= read -r password || true
    if ! printf '%s\n' "$password" | sudo -S -p '' -v; then
      unset password
      die "sudo rejected the password from stdin"
    fi
  else
    while :; do
      printf 'Password:' >&3
      IFS= read -rs password <&3 || die "no answer on the terminal"
      printf '\n' >&3
      if printf '%s\n' "$password" | sudo -S -p '' -v 2>/dev/null; then break; fi
      tries=$((tries + 1))
      [ "$tries" -lt 3 ] || die "sudo refused"
      printf 'Sorry, try again.\n' >&3
    done
  fi
  askpass_dir=$(mktemp -d "${TMPDIR:-/tmp}/ensemble-sudo.XXXXXX")
  case $askpass_dir in *"'"* | *$'\n'*) die "unusable TMPDIR: $askpass_dir" ;; esac
  mkfifo -m 600 "$askpass_dir/fifo"
  cat >"$askpass_dir/askpass" <<EOF
#!/bin/sh
IFS= read -r p <'$askpass_dir/fifo' && printf '%s\n' "\$p"
EOF
  chmod 700 "$askpass_dir/askpass"
  (
    trap '' PIPE
    while [ -p "$askpass_dir/fifo" ]; do
      printf '%s\n' "$password" 2>/dev/null >"$askpass_dir/fifo" || true
    done
  ) </dev/null >/dev/null 2>&1 &
  feeder=$!
  unset password
  export SUDO_ASKPASS=$askpass_dir/askpass
  (
    trap 'kill "$nap" 2>/dev/null; exit 0' TERM
    while kill -0 $$ 2>/dev/null; do
      sudo -n -v || true
      sleep 30 &
      nap=$!
      wait "$nap" || true
    done
    kill "$feeder" 2>/dev/null
    sleep 1
    rm -rf "$askpass_dir"
  ) </dev/null >/dev/null 2>&1 &
  keepalive=$!
}

# Stops the keep-alive and the askpass feeder, and drops the ticket sudo_once made.
sudo_end() {
  local pid
  for pid in $keepalive $feeder; do
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  if [ -n "$askpass_dir" ]; then rm -rf "$askpass_dir"; fi
  if [ -n "$sudo_mine" ]; then sudo -k </dev/null >/dev/null 2>&1 || true; fi
}

# Appends $1, which puts directory $2 on PATH, to the login shell's profile, with consent, unless
# it is there; then, when $2 is not on the PATH install.sh started with, on_exit says to source it.
profile_line() {
  local file=$HOME/.bash_profile
  case ${SHELL:-} in *zsh) file=$HOME/.zprofile ;; esac
  step "shell profile: $1"
  if [ -f "$file" ] && grep -qxF -- "$1" "$file"; then
    skip "in $file"
  elif consent "$file" "$1"; then
    if absent "$file"; then ledger_add profile-file "$file"; fi
    ledger_add profile "$file" "$1"
    if [ -s "$file" ] && [ -n "$(tail -c 1 "$file")" ]; then printf '\n' >>"$file"; fi
    printf '%s\n' "$1" >>"$file"
  else
    skip declined
    return
  fi
  case ":$user_path:" in *":$2:"*) ;; *) reload=$file ;; esac
}

homebrew() {
  local prefix=${HOMEBREW_PREFIX:-$root/opt/homebrew} brew installer
  step Homebrew
  if brew=$(command -v brew); then
    skip "installed"
  elif [ -x "$prefix/bin/brew" ]; then
    brew=$prefix/bin/brew
    skip "installed"
  else
    ledger_add homebrew "$prefix"
    if ! xcode-select -p </dev/null >/dev/null 2>&1; then ledger_add clt; fi
    installer=$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)
    NONINTERACTIVE=1 bash -c "$installer" </dev/null
    brew=$prefix/bin/brew
  fi
  eval "$("$brew" shellenv)"
  hash -r
  profile_line "eval \"\$($brew shellenv)\"" "${brew%/*}"
}

brew_install() {
  if [ "$1" = --cask ]; then ledger_add cask "$2"; else ledger_add formula "$1"; fi
  say "    brew install $*"
  brew install "$@" </dev/null
  hash -r
}

formula() {
  if brew list --formula "$1" </dev/null >/dev/null 2>&1; then
    skip "$1 installed"
  else
    brew_install "$1"
  fi
}

# cask <name> <command or app path that proves it installed>
cask() {
  if { [ -n "$2" ] && command -v "$2" >/dev/null; } || [ -e "${3:-/nonexistent}" ] \
    || brew list --cask "$1" </dev/null >/dev/null 2>&1; then
    skip "$1 installed"
  else
    brew_install --cask "$1"
  fi
}

python_ok() {
  command -v python3 >/dev/null && python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' </dev/null 2>/dev/null
}

has() { case ",$clients," in *",$1,"*) return 0 ;; esac; return 1; }

dependencies() {
  step dependencies
  formula gh
  formula tmux
  formula git
  if python_ok; then skip "python3 is 3.11+"; else brew_install python; fi
  record_dir "$HOME/.config"
  if [ ! -e "$root/Applications/iTerm.app" ]; then
    record_dir "$HOME/.config/iterm2"
    record_library dir "${iterm_dirs[@]}"
    record_library file "${iterm_files[@]}"
  fi
  cask iterm2 "" "$root/Applications/iTerm.app"
}

# What an existing config.local.toml says: "<[session.hosts] default>\t<[delegate] client>\t<[orchestrator] tracker>".
config_says() {
  [ -f "$config" ] || return 0
  python3 -I -c '
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as f:
        data = tomllib.load(f)
except (OSError, tomllib.TOMLDecodeError):
    sys.exit()
def get(*keys):
    node = data
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, str) else ""
tracker = get("orchestrator", "tracker")
print("\t".join((get("session", "hosts", "default"), get("delegate", "client"), tracker if tracker in ("local", "linear") else "")))' "$config"
}

# ask_valid <question> <check>: asks until <check> accepts the answer.
ask_valid() {
  while :; do
    ask "$1"
    if "$2" "$answer"; then return 0; fi
  done
}

# The checklist on the terminal: up/down (or k/j) move, space ticks, Enter confirms. labels[] are its items, ticks[]
# their 1/0, in and out.
checklist() {
  local n=${#labels[@]} at=0 i key rest mark drawn= saved
  saved=$(stty -g <&3)
  stty -echo <&3
  printf 'Choose what to install (up/down move, space ticks, Enter confirms):\n' >&3
  while :; do
    if [ -n "$drawn" ]; then printf '\033[%dA' "$n" >&3; fi
    drawn=1
    for ((i = 0; i < n; i++)); do
      mark=' '
      if [ "${ticks[i]}" = 1 ]; then mark=x; fi
      if [ "$i" = "$at" ]; then printf '\r\033[K> [%s] %s\n' "$mark" "${labels[i]}" >&3; else printf '\r\033[K  [%s] %s\n' "$mark" "${labels[i]}" >&3; fi
    done
    if ! IFS= read -rsn1 -d '' key <&3; then
      stty "$saved" <&3
      die "no answer on the terminal"
    fi
    case $key in
      $'\033')
        IFS= read -rsn2 rest <&3 || rest=
        case $rest in
          '[A' | OA) at=$(((at + n - 1) % n)) ;;
          '[B' | OB) at=$(((at + 1) % n)) ;;
        esac
        ;;
      k) at=$(((at + n - 1) % n)) ;;
      j) at=$(((at + 1) % n)) ;;
      ' ') ticks[at]=$((1 - ticks[at])) ;;
      $'\n' | $'\r')
        stty "$saved" <&3
        return 0
        ;;
    esac
  done
}

# The configuration: each answer from its flag, else the last answer (State), else the config, else the default;
# the checklist appears only when clients or Docker has no flag or saved answer.
answers() {
  local said default client item choose=
  local -a labels ticks names=(claude codex opencode)
  step configuration
  said=$(config_says)
  default=${said%%$'\t'*}
  client=${said#*$'\t'}
  client=${client%%$'\t'*}
  clients=${clients_flag:-$(state_get clients)}
  docker=${docker_flag:-$(state_get docker)}
  if [ -z "$clients" ] || [ -z "$docker" ]; then choose=yes; fi
  if [ -z "$clients" ]; then
    case $client in claude | codex | opencode) clients=$client ;; *) clients=claude,codex ;; esac
    if opencode_flags; then clients=$(clients_of "$clients,opencode"); fi
  fi
  if [ -z "$docker" ]; then
    if [ "$default" = docker ]; then docker=yes; else docker=no; fi
  fi
  board=$(state_get board)
  case $board_flag in local | linear) board=yes ;; no) board=no ;; esac
  board=${board:-no}
  if [ -n "$choose" ]; then
    labels=("Claude Code" "Codex" "OpenCode" "Docker workers (Docker Desktop and the ensemble broker)" "Board: local or Linear (launchd jobs chosen separately)")
    ticks=()
    for item in "${names[@]}"; do
      if has "$item"; then ticks+=(1); else ticks+=(0); fi
    done
    if [ "$docker" = yes ]; then ticks+=(1); else ticks+=(0); fi
    if [ "$board" = yes ]; then ticks+=(1); else ticks+=(0); fi
    while :; do
      checklist
      clients=
      for item in 0 1 2; do
        if [ "${ticks[item]}" = 1 ]; then clients=${clients:+$clients,}${names[item]}; fi
      done
      [ -z "$clients" ] || break
      printf 'Tick at least one client.\n' >&3
    done
    if [ "${ticks[3]}" = 1 ]; then docker=yes; else docker=no; fi
    if [ "${ticks[4]}" = 1 ]; then board=yes; else board=no; fi
  fi
  if [ "$board" != yes ] && [ -n "$jobs_flag" ]; then
    usage_error "--launchd/--no-launchd requires a board"
  fi
  board_tracker=
  case $board_flag in local | linear) board_tracker=$board_flag ;; esac
  board_tracker=${board_tracker:-$(state_get board_tracker)}
  board_tracker=${board_tracker:-$(configured_tracker "$said")}
  board_jobs_choice=${jobs_flag:-$(state_get board_jobs)}
  if [ "$board" = yes ]; then
    if [ -z "$board_tracker" ]; then
      while :; do
        ask "Board tracker [local/linear] [local]:"
        case ${answer:-local} in
          local | linear) board_tracker=${answer:-local}; break ;;
        esac
      done
    fi
    if [ -z "$board_jobs_choice" ]; then
      if [ -f "$HOME/Library/LaunchAgents/com.windroc.ensemble.router.plist" ] \
        || [ -f "$HOME/Library/LaunchAgents/com.windroc.ensemble.promote.plist" ]; then
        board_jobs_choice=yes
      elif ask_yn "Install board launchd jobs?"; then
        board_jobs_choice=yes
      else
        board_jobs_choice=no
      fi
    fi
    state_set board_tracker "$board_tracker"
    state_set board_jobs "$board_jobs_choice"
  fi
  board_tracker=${board_tracker:-local}
  if opencode_flags && ! has opencode; then usage_error "--opencode-*: opencode is not among the clients"; fi
  prev_provider=$(state_get opencode_provider)
  prev_url=$(state_get opencode_url)
  prev_model=$(state_get opencode_model)
  prev_context=$(state_get opencode_context)
  if has opencode; then
    provider=${provider_flag:-$prev_provider}
    url=${url_flag:-$prev_url}
    model=${model_flag:-$prev_model}
    context=${context_flag:-$prev_context}
    if [ -z "$provider" ]; then
      ask_valid "OpenCode provider name:" valid_name
      provider=$answer
    fi
    if [ -z "$url" ]; then
      ask_valid "Its base URL (ending /v1):" valid_url
      url=$answer
    fi
    if [ -z "$model" ]; then
      ask_valid "Its model:" valid_model
      model=$answer
    fi
    while [ -z "$context" ]; do
      ask "Its context window in tokens [65536]:"
      if valid_context "${answer:-65536}"; then context=${answer:-65536}; fi
    done
    state_set opencode_provider "$provider"
    state_set opencode_url "$url"
    state_set opencode_model "$model"
    state_set opencode_context "$context"
  fi
  state_set clients "$clients"
  state_set docker "$docker"
  state_set board "$board"
  say "    clients: $clients, Docker: $docker, board: $board ($board_tracker), launchd: ${board_jobs_choice:-no}${provider:+, OpenCode provider: $provider}"
}

# Plans the edits that give config.local.toml (or a new one) the wanted settings; writes the result to
# $1 and prints the changes (exit 3), prints only them when line edits cannot apply them (exit 4), or exit 0.
config_plan() {
  python3 -I -c '
import json, os, re, sys, tomllib
path, out, default, harness, client, exe, name, url, model, context, prev_name, prev_url, prev_model, prev_context, only, board = sys.argv[1:]
fresh = not os.path.exists(path)
text = ""
if not fresh:
    with open(path, encoding="utf-8") as f:
        text = f.read()
try:
    data = tomllib.loads(text)
except tomllib.TOMLDecodeError as error:
    sys.exit(f"{path}: {error}")
def get(data, table, key=None):
    node = data
    for part in table.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    if key is None:
        return node
    return node.get(key) if isinstance(node, dict) else None
wanted = []
def want(table, key, value):
    if get(data, table, key) != value:
        wanted.append((table, key, value))
only = only.split("\t") if only else []
scoped = bool(only or board)
if board:
    with open(board, encoding="utf-8") as f:
        wanted += [tuple(entry) for entry in json.load(f)]
elif only:
    want(*only)
elif fresh or (get(data, "session.hosts", "default") or "tmux") != default:
    wanted.append(("session.hosts", "default", default))
if not scoped and client and (get(data, "delegate", "client") or "claude") != client:
    wanted.append(("delegate", "client", client))
if not scoped and harness and get(data, "delegate.delegates", "harness_host") in (None, "", "docker"):
    wanted.append(("delegate.delegates", "harness_host", harness))
if name and not scoped:
    target, before = f"{name}/{model}", f"{prev_name}/{prev_model}" if prev_name else None
    if get(data, "session.clients.opencode", "executable") in (None, ""):
        want("session.clients.opencode", "executable", exe)
    def ours(value):
        return value is None or (isinstance(value, str) and (value.startswith("ollama/") or value == before))
    if ours(get(data, "session.clients.opencode", "model")):
        want("session.clients.opencode", "model", target)
    table = f"session.clients.opencode.providers.{name}"
    found = get(data, table)
    before_context = int(prev_context) if prev_context else None
    if found is None or (name == prev_name and found.get("base_url") == prev_url and found.get("models") == [prev_model]
                         and found.get("context") in (None, before_context)):
        want(table, "base_url", url)
        want(table, "models", [model])
        want(table, "context", int(context))
    for tier in "1234":
        if ours(get(data, "session.clients.opencode.tiers", tier)):
            want("session.clients.opencode.tiers", tier, target)
if not wanted:
    sys.exit(0)
def toml(value):
    if isinstance(value, list):
        return "[" + ", ".join(toml(item) for item in value) + "]"
    return str(value) if isinstance(value, int) else json.dumps(value, ensure_ascii=False)
def toml_key(key):
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)
def line_key(line):
    try:
        parsed = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return None
    return next(iter(parsed)) if len(parsed) == 1 else None
lines = text.splitlines(keepends=True)
if fresh:
    lines = ["# Written by install.sh. Keys and defaults: each layer\x27s packaged config.toml (its `# local:` lines);\n",
             "# the Linear board: docs/board.md#setup.\n"]
if lines and not lines[-1].endswith("\n"):
    lines[-1] += "\n"
added = set()
for table, key, value in wanted:
    new = f"{toml_key(key)} = {toml(value)}\n"
    print(f"[{table}] {new.strip()}")
    starts = [i for i, line in enumerate(lines) if line.strip() == f"[{table}]"]
    if not starts:
        lines += (["\n"] if lines and not (fresh and len(lines) == 2) else []) + [f"[{table}]\n", new]
        added.add(table)
        continue
    end = starts[0] + 1
    while end < len(lines) and not lines[end].lstrip().startswith("["):
        end += 1
    hits = [i for i in range(starts[0] + 1, end) if line_key(lines[i]) == key]
    if hits:
        lines[hits[0]] = new
    else:
        lines.insert(end if fresh or table in added else starts[0] + 1, new)
result = "".join(lines)
try:
    edited = tomllib.loads(result)
except tomllib.TOMLDecodeError:
    sys.exit(4)
if any(get(edited, table, key) != value for table, key, value in wanted):
    sys.exit(4)
with open(out, "w", encoding="utf-8") as f:
    f.write(result)
sys.exit(3)' "$config" "$1" "$wanted_default" "$wanted_harness" "$wanted_client" \
    "$opencode_executable" "$provider" "$url" "$model" "$context" \
    "$prev_provider" "$prev_url" "$prev_model" "$prev_context" "$only" "$board_file"
}

# Writes the configuration's settings into config.local.toml with consent; given <table> <key> <value>, only that;
# given board <file>, the settings board_plan wrote to <file>.
config_file() {
  local wanted_default=tmux wanted_harness= wanted_client= only= board_file= changes rc=0 plan
  if [ $# = 2 ] && [ "$1" = board ]; then
    board_file=$2
  elif [ $# = 3 ]; then
    only=$1$'\t'$2$'\t'$3
  fi
  step "config.local.toml${only:+: [$1] $2}${board_file:+: Linear board}"
  if [ "$docker" = yes ]; then
    wanted_default=docker
    wanted_harness=tmux
  fi
  if ! has claude; then
    wanted_client=opencode
    if has codex; then wanted_client=codex; fi
  fi
  mkdir -p "$state_dir"
  plan=$state_dir/config.local.toml.plan
  changes=$(config_plan "$plan") || rc=$?
  case $rc in
    0) skip "has the configuration" ;;
    3)
      if [ ! -f "$config" ]; then changes=$(cat "$plan"); fi
      if consent "$config" "$changes"; then
        if [ -f "$config" ]; then backup_config; else ledger_add config created; fi
        mv "$plan" "$config"
      else
        rm -f "$plan"
        skip declined
      fi
      ;;
    4)
      say "    set these by hand in $config:"
      say "$changes"
      skip "set it by hand"
      ;;
    *) die "cannot read $config" ;;
  esac
}

# Keeps the user's own config.local.toml once, before the installer's first edit (the cleanup restores it).
backup_config() {
  local backup=$state_dir/config.local.toml.orig
  if recorded config created || recorded config backup; then return 0; fi
  cp "$config" "$backup.tmp"
  mv "$backup.tmp" "$backup"
  ledger_add config backup
}

apps() {
  local rc
  step apps
  record_dir "$HOME/.config/gh"
  if has claude; then
    record_dir "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
    if [ -z "${CLAUDE_CONFIG_DIR:-}" ]; then record_file "$HOME/.claude.json"; fi
  fi
  if has codex; then record_dir "${CODEX_HOME:-$HOME/.codex}"; fi
  if [ "$docker" = yes ]; then
    record_dir "$HOME/.docker"
    record_dir "$HOME/Library/Containers/com.docker.docker"
    record_dir "$HOME/Library/Group Containers/group.com.docker"
    record_dir "$HOME/.config/cagent"
    record_library dir "${docker_dirs[@]}"
    record_library file "${docker_files[@]}"
    # Docker Desktop adds its CLI completion block to the shell's rc files, creating them if needed: the cleanup
    # removes the block where it was not before, and a file that was not there once empty.
    for rc in .zshrc .bashrc .bash_profile .zprofile; do
      if absent "$HOME/$rc"; then
        ledger_add profile-file "$HOME/$rc"
      elif ! grep -qF "# End of Docker Desktop section." "$HOME/$rc"; then
        ledger_add docker-block "$HOME/$rc"
      fi
    done
  fi
  if has claude; then cask claude-code claude; else skip "Claude Code: not chosen"; fi
  if has codex; then cask codex codex; else skip "Codex: not chosen"; fi
  if [ "$docker" = yes ]; then
    cask docker-desktop "" "$root/Applications/Docker.app"
    docker_build
    if [ "$broker_mode" = build ]; then formula go; else skip "go: the broker comes from release $release_tag"; fi
  else
    skip "Docker Desktop: Docker not chosen"
  fi
}

client_logins() {
  step "GitHub CLI login"
  if gh auth status </dev/null >/dev/null 2>&1; then
    skip "logged in"
  else
    ledger_add login gh
    gh auth login --hostname github.com --git-protocol https --web <&3 >&3 2>&3
  fi
  step "Claude Code login"
  if ! has claude; then
    skip "Claude Code not chosen"
  elif claude auth status </dev/null >/dev/null 2>&1; then
    skip "logged in"
  else
    ledger_add login claude
    claude auth login <&3 >&3 2>&3
  fi
  step "Codex login"
  if ! has codex; then
    skip "Codex not chosen"
  elif codex login status </dev/null >/dev/null 2>&1; then
    skip "logged in"
  else
    ledger_add login codex
    codex login <&3 >&3 2>&3
  fi
}

# Exit 0: marketplace $1 is $2; 1: absent; 2: another source.
claude_market() {
  claude plugin marketplace list --json </dev/null | python3 -I -c '
import json, sys
name, repo = sys.argv[1:]
for m in json.load(sys.stdin):
    if m.get("name") == name:
        sys.exit(0 if m.get("repo") == repo else 2)
sys.exit(1)' "$1" "$2"
}

# Exit 0: plugin $1 installed and enabled; 1: absent; 2: disabled.
claude_plugin() {
  claude plugin list --json </dev/null | python3 -I -c '
import json, sys
found = [p for p in json.load(sys.stdin) if p.get("id") == sys.argv[1]]
sys.exit(1 if not found else 0 if found[0].get("enabled") else 2)' "$1"
}

codex_market() {
  python3 -I -c '
import sys, tomllib
path, name, source = sys.argv[1:]
try:
    with open(path, "rb") as f:
        m = tomllib.load(f).get("marketplaces", {}).get(name)
except FileNotFoundError:
    m = None
if m is None:
    sys.exit(1)
sys.exit(0 if m.get("source") == source else 2)' \
    "${CODEX_HOME:-$HOME/.codex}/config.toml" "$1" "$2"
}

# Exit 0: plugin $1 installed and enabled; 1: absent; 2: disabled.
codex_plugin() {
  codex plugin list --json </dev/null | python3 -I -c '
import json, sys
found = [p for p in json.load(sys.stdin).get("installed", []) if p.get("pluginId") == sys.argv[1]]
sys.exit(1 if not found else 0 if found[0].get("enabled") else 2)' "$1"
}

# The ref the windroc marketplace serves ensemble at, from its manifest $1 on main: the ensemble entry's source.ref,
# else main (the default branch). A ref goes into URLs: --ref's characters and no .. segment.
marketplace_ref() {
  local text served
  text=$(curl -fsSL "https://raw.githubusercontent.com/windroc-labs/agent-plugins-marketplace/main/$1" </dev/null) \
    || die "cannot read ensemble's ref from the windroc marketplace ($1)"
  served=$(python3 -I -c '
import json, re, sys
try:
    plugins = json.loads(sys.stdin.read())["plugins"]
    source = next(p["source"] for p in plugins if p.get("name") == "ensemble")
    ref = None if "sha" in source else source.get("ref", "main")
except Exception:
    sys.exit("no ensemble entry")
if ref is None:
    sys.exit("pinned by sha, not a release tag")
if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", ref) or ref.startswith("-") \
        or ".." in ref.split("/"):
    sys.exit(f"not a git ref: {ref!r}")
print(ref)' 2>&1 <<<"$text") || die "cannot read ensemble's ref from the windroc marketplace ($1): $served"
  printf '%s' "$served"
}

# Claude Code and Codex install ensemble@windroc from the windroc marketplace, which serves one ref of ensemble: the
# install takes that ref, unless OpenCode alone names another.
ref_check() {
  local claude_ref= codex_ref= served_ref
  if [ -n "$ref" ] && ! has claude && ! has codex; then return; fi
  if has claude || ! has codex; then claude_ref=$(marketplace_ref .claude-plugin/marketplace.json); fi
  if has codex; then codex_ref=$(marketplace_ref .agents/plugins/marketplace.json); fi
  if [ -n "$claude_ref" ] && [ -n "$codex_ref" ] && [ "$claude_ref" != "$codex_ref" ]; then
    die "the windroc marketplace serves ensemble at $claude_ref to Claude Code and $codex_ref to Codex: they must match"
  fi
  served_ref=${claude_ref:-$codex_ref}
  if [ -z "$ref" ]; then
    ref=$served_ref
  elif [ "$ref" != "$served_ref" ]; then
    die "--ref $ref: Claude Code and Codex install ensemble@windroc from the windroc marketplace, which serves ensemble at $served_ref; use --ref $served_ref, or --clients opencode for another ref"
  fi
}

# File $2 of windroc-labs/ensemble at ref $1.
ensemble_file() {
  curl -fsSL "https://raw.githubusercontent.com/windroc-labs/ensemble/$1/$2" </dev/null \
    || die "cannot read $2 at ref $1 of windroc-labs/ensemble"
}

# File $2 of plugin source $1: an installed plugin's directory, else ensemble at $ref on GitHub.
plugin_file() {
  if [ -z "$1" ]; then
    ensemble_file "$ref" "$2"
  else
    cat -- "$1/$2" 2>/dev/null || die "cannot read $1/$2"
  fi
}

# Parsers of the files that hold the versions, for GitHub's copy and an installed one alike; empty when absent.
protocol_of() { sed -n 's/^PROTOCOL = "\([0-9][0-9]*\)"$/\1/p' | sed -n 1p; }
go_protocol_of() { sed -n 's/^const Version = "\([0-9][0-9]*\)"$/\1/p' | sed -n 1p; }
version_of() { sed -n 's/.*"version":[[:space:]]*"\([0-9][0-9]*\(\.[0-9][0-9]*\)*\)".*/\1/p' | sed -n 1p; }
spoken_protocol_of() { sed -n 's/.*"protocol_version": *"\([0-9][0-9]*\)".*/\1/p' | sed -n 1p; }

# The ensemble plugin Claude Code ($1 claude) or Codex (codex) has installed, else empty;
# Codex's version at the served ref, ignoring caches from other installs.
installed_plugin() {
  local version=${2:-}
  if [ "$1" = claude ]; then
    command -v claude >/dev/null || return 0
    claude plugin list --json </dev/null | python3 -I -c '
import json, sys
print(next((p.get("installPath", "") for p in json.load(sys.stdin) if p.get("id") == "ensemble@windroc"), ""))'
  else
    if [ -z "$version" ]; then version=$(ensemble_file "$ref" .claude-plugin/plugin.json | version_of); fi
    [ -n "$version" ] || die "Codex: no ensemble version at $ref"
    printf '%s/%s' "${CODEX_HOME:-$HOME/.codex}/plugins/cache/windroc/ensemble" "$version"
  fi
}

# With Docker: preflight uses the served ref before client setup; otherwise use refreshed installed sources.
# The broker comes from the release of the plugin plugin_dir will pick
# (Claude Code's, else Codex's, else OpenCode's), downloaded, or for a branch built from its sources, and must speak
# every chosen client's plugin protocol. Only advisory: src/broker/install.sh checks the download's sha256.
broker_plan() {
  local -a sources=()
  local client source label where version want got remedy installed broker_label commit opencode= seen= agreed=
  [ "$docker" = yes ] || return 0
  for client in claude codex opencode; do
    has "$client" || continue
    if [ "$client" != opencode ]; then
      if [ "${1:-}" = preflight ]; then source=; else source=$(installed_plugin "$client"); fi
    else
      # OpenCode's copy counts only while it is at the commit $ref names: opencode_plugin downloads it again otherwise.
      source=$(opencode_package "$(opencode_spec)" 2>/dev/null) || source=
      commit=$(state_get opencode_plugin_commit)
      if [ -z "$commit" ] || [ "$commit" != "$(ref_commit 2>/dev/null)" ]; then source=; fi
    fi
    [ -z "$source" ] || [ -d "$source" ] || source=
    if [ "$client" = opencode ]; then opencode=$source; fi
    sources+=("$source")
  done
  where=${sources[0]:-ref $ref}
  version=$(plugin_file "${sources[0]}" .claude-plugin/plugin.json | version_of)
  [ -n "$version" ] || die "no release version in .claude-plugin/plugin.json at $where"
  release_tag=v$version
  if [ "$ref" = main ] || [ "$ref" = "$release_tag" ] || { [ -n "${sources[0]}" ] && [ "${sources[0]}" != "$opencode" ]; }; then
    broker_mode=release
  else
    broker_mode=build
    return
  fi
  if [ -n "${sources[0]}" ] && installed=$(broker_current "${sources[0]}"); then
    got=${installed%%:*}
    broker_label="the installed broker"
  else
    got=$(ensemble_file "$release_tag" src/broker/internal/protocol/protocol.go | go_protocol_of)
    [ -n "$got" ] || die "no broker protocol in src/broker/internal/protocol/protocol.go at ref $release_tag"
    broker_label="release $release_tag"
  fi
  for source in "${sources[@]}"; do
    label=${source:-ensemble $ref}
    case $seen in *"<$label>"*) continue ;; esac
    seen+="<$label>"
    want=$(plugin_file "$source" src/ensemble/session/hosts/docker/broker.py | protocol_of)
    [ -n "$want" ] || die "no broker protocol in src/ensemble/session/hosts/docker/broker.py at ${source:-ref $ref}"
    if [ "$want" != "$got" ]; then
      # With Claude Code or Codex, ref_check takes only the served ref: OpenCode alone may change --ref.
      if ! has claude && ! has codex; then
        remedy="use --ref $release_tag or a branch other than main"
      elif [ -n "$source" ] || [ -n "${sources[0]}" ]; then
        remedy="update the plugin (Claude Code: claude plugin marketplace update windroc, then claude plugin update ensemble@windroc; Codex: codex plugin marketplace upgrade windroc), then rerun"
      else
        remedy="the windroc marketplace serves ensemble at $ref, ahead of release $release_tag: wait until it serves a release, or use --clients opencode --ref $release_tag"
      fi
      die "$label wants broker protocol $want, but its broker, $broker_label, speaks protocol $got: $remedy"
    fi
    agreed+="${agreed:+, }$label"
  done
  say "    broker protocol $got: $agreed and $broker_label agree"
}

# Check the source the client and launcher will use after refreshing it, before any consumer reads its files.
ensemble_plugin_check() {
  local client=$1 label=$2 source expected got want
  expected=$(ensemble_file "$ref" .claude-plugin/plugin.json | version_of) \
    || die "$label: cannot read ensemble's expected version at $ref"
  [ -n "$expected" ] || die "$label: no ensemble version at $ref"
  case $ref in
    v[0-9]*) [ "$ref" = "v$expected" ] || die "$label: ensemble at $ref reports version $expected" ;;
  esac
  source=$(installed_plugin "$client" "$expected") || die "$label: cannot locate ensemble after updating to $ref"
  [ -n "$source" ] && [ -d "$source" ] || die "$label: no installed ensemble directory after updating to $ref"
  got=$(version_of <"$source/.claude-plugin/plugin.json" 2>/dev/null) \
    || die "$label: cannot read ensemble version at $source after updating to $ref"
  [ "$got" = "$expected" ] \
    || die "$label: ensemble update to $ref expected version $expected, got ${got:-unknown} at $source"
  if [ "$docker" = yes ]; then
    want=$(protocol_of <"$source/src/ensemble/session/hosts/docker/broker.py" 2>/dev/null) \
      || die "$label: cannot read ensemble broker protocol at $source after updating to $ref"
    [ -n "$want" ] || die "$label: no ensemble broker protocol at $source after updating to $ref"
  fi
}

# Claude Code and Codex install ensemble, and updraft for host workers, from the windroc marketplace.
claude_plugins() {
  local rc id ids=(ensemble@windroc)
  step "Claude Code plugins"
  if ! has claude; then
    skip "Claude Code not chosen"
    return
  fi
  rc=0
  claude_market windroc windroc-labs/agent-plugins-marketplace || rc=$?
  if [ "$rc" = 1 ]; then
    ledger_add claude-marketplace windroc
    claude plugin marketplace add windroc-labs/agent-plugins-marketplace </dev/null >&2 \
      || die "Claude Code: cannot add marketplace windroc"
  else
    [ "$rc" = 0 ] || die "Claude Code: marketplace windroc has another source"
    claude plugin marketplace update windroc </dev/null >&2 || die "Claude Code: cannot refresh marketplace windroc"
  fi
  if [ "$docker" != yes ]; then ids+=(updraft@windroc); fi
  for id in "${ids[@]}"; do
    rc=0
    claude_plugin "$id" || rc=$?
    if [ "$id" = ensemble@windroc ] && [ "$rc" != 1 ]; then
      claude plugin update "$id" </dev/null >&2 || die "Claude Code: cannot update $id to $ref"
    fi
    case $rc in
      0) skip "$id installed" ;;
      2) claude plugin enable "$id" </dev/null >&2 || die "Claude Code: cannot enable $id" ;;
      *)
        ledger_add claude-plugin "$id"
        claude plugin install "$id" </dev/null >&2 || die "Claude Code: cannot install $id at $ref"
        ;;
    esac
  done
  ensemble_plugin_check claude "Claude Code"
}

codex_plugins() {
  local rc id ids=(ensemble@windroc)
  step "Codex plugins"
  if ! has codex; then
    skip "Codex not chosen"
    return
  fi
  rc=0
  codex_market windroc https://github.com/windroc-labs/agent-plugins-marketplace.git || rc=$?
  if [ "$rc" = 1 ]; then
    ledger_add codex-marketplace windroc
    codex plugin marketplace add windroc-labs/agent-plugins-marketplace </dev/null >&2 \
      || die "Codex: cannot add marketplace windroc"
  else
    [ "$rc" = 0 ] || die "Codex: marketplace windroc has another source"
    codex plugin marketplace upgrade windroc </dev/null >&2 || die "Codex: cannot refresh marketplace windroc"
  fi
  if [ "$docker" != yes ]; then ids+=(updraft@windroc); fi
  for id in "${ids[@]}"; do
    rc=0
    codex_plugin "$id" || rc=$?
    case $rc in
      0)
        if [ "$id" = ensemble@windroc ]; then
          codex plugin add "$id" </dev/null >&2 || die "Codex: cannot update $id to $ref"
        else
          skip "$id installed"
        fi
        ;;
      2) codex plugin add "$id" </dev/null >&2 || die "Codex: cannot enable/update $id to $ref" ;;
      *)
        ledger_add codex-plugin "$id"
        codex plugin add "$id" </dev/null >&2 || die "Codex: cannot install $id at $ref"
        ;;
    esac
  done
  ensemble_plugin_check codex "Codex"
}

# The installed plugin: the whole repo (at $ref for OpenCode), source of the broker, ctmux and jobs. Claude Code's
# install first, else Codex's served version, else OpenCode's. ctmux --install selects this source for launchers.
plugin_dir() {
  step "plugin directory"
  if ! has claude && ! has codex; then
    pkg=$(opencode_package "$(opencode_spec)")
  elif has claude; then
    pkg=$(installed_plugin claude)
  else
    pkg=$(installed_plugin codex)
  fi
  [ -n "$pkg" ] && [ -d "$pkg" ] || die "no installed ensemble plugin directory"
  say "    $pkg"
}

# go_const <file> <name>: the string value of `const <name> = "..."` in a Go file of the plugin.
go_const() {
  local value
  value=$(sed -n "s/^const $2 = \"\\([0-9.]*\\)\"\$/\\1/p" "$pkg/$1")
  [ -n "$value" ] || die "no $2 in $pkg/$1"
  printf '%s' "$value"
}

# The OpenCode version ensemble pins: from the plugin's broker sources, else (OpenCode only, no plugin yet) from --ref.
opencode_version() {
  local text value
  if [ -n "$pkg" ]; then
    go_const src/broker/internal/launcher/versions.go opencodeMinVersion
    return
  fi
  text=$(curl -fsSL "https://raw.githubusercontent.com/windroc-labs/ensemble/$ref/src/broker/internal/launcher/versions.go") \
    || die "cannot read OpenCode's version at ref $ref"
  value=$(sed -n 's/^const opencodeMinVersion = "\([0-9.]*\)"$/\1/p' <<<"$text")
  [ -n "$value" ] || die "no opencodeMinVersion at ref $ref"
  printf '%s' "$value"
}

# Only a stable, compatible 1.x version at or above $2 qualifies; failed --version calls never qualify.
opencode_usable() {
  local found major minor patch want_major want_minor want_patch
  local stable='^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$'
  found=$("$1" --version </dev/null 2>/dev/null) || return 1
  [[ $found =~ $stable ]] || return 1
  IFS=. read -r major minor patch <<<"$found"
  IFS=. read -r want_major want_minor want_patch <<<"$2"
  [ "$major" -eq 1 ] && [ "$major" -eq "$want_major" ] && {
    [ "$minor" -gt "$want_minor" ] || { [ "$minor" -eq "$want_minor" ] && [ "$patch" -ge "$want_patch" ]; }
  }
}

# Select before config_file so the config, plugin installer and login all use the same executable.
opencode_select() {
  local candidate
  has opencode || return 0
  opencode_min=$(opencode_version)
  record_dir "${XDG_DATA_HOME:-$HOME/.local/share}/opencode"
  record_dir "${XDG_DATA_HOME:-$HOME/.local/share}/opentui"
  record_dir "${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
  record_dir "${XDG_STATE_HOME:-$HOME/.local/state}/opencode"
  record_dir "${XDG_CACHE_HOME:-$HOME/.cache}/opencode"
  if candidate=$(type -P opencode) && opencode_usable "$candidate" "$opencode_min"; then
    case $candidate in /*) ;; *) candidate=$PWD/$candidate ;; esac
    opencode_executable=$candidate
  fi
}

opencode_install() {
  local version found=
  step "OpenCode install"
  if ! has opencode; then
    skip "OpenCode not chosen"
    return
  fi
  version=$opencode_min
  if [ "$opencode_executable" != "$opencode_prefix/bin/opencode" ]; then
    skip "OpenCode $version or newer at $opencode_executable"
  elif opencode_usable "$opencode_executable" "$version"; then
    skip "OpenCode $version in $opencode_prefix"
  else
    formula node
    record_dir "$opencode_prefix"
    npm install -g --prefix "$opencode_prefix" --cache "$opencode_prefix/.npm-cache" "opencode-ai@$version" </dev/null >&2
    # npm's allowScripts skips opencode-ai's postinstall, which puts the platform binary in place of a stub.
    node "$opencode_prefix/lib/node_modules/opencode-ai/postinstall.mjs" </dev/null >&2
    found=$("$opencode_prefix/bin/opencode" --version </dev/null 2>/dev/null || true)
    [ "$found" = "$version" ] || die "OpenCode $version in $opencode_prefix does not run: opencode --version gave '$found'"
  fi
  case $(python3 -I -c 'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.argv[1]).hostname or "")' "$url") in
    localhost | 127.0.0.1) ;;
    *) case $url in http://*) say "$url: http:// sends the API key in plaintext over the network (docs/docker-host.md › Risks)" ;; esac ;;
  esac
}

opencode_spec() { printf '%s' "ensemble@git+https://github.com/windroc-labs/ensemble.git#$ref"; }

# The directory OpenCode downloads spec $1 into, else exit 1 when it would not lie under OpenCode's packages
# (a --ref with .. segments).
opencode_cache_dir() {
  python3 -I -c '
import os, sys
packages = os.path.normpath(sys.argv[1])
path = os.path.normpath(os.path.join(packages, sys.argv[2]))
if not path.startswith(packages + os.sep):
    sys.exit(1)
print(path)' "${XDG_CACHE_HOME:-$HOME/.cache}/opencode/packages" "$1"
}

# Where OpenCode keeps the plugin it downloaded for spec $1: the whole repo at that ref.
opencode_package() {
  local dir
  dir=$(opencode_cache_dir "$1") || die "$1: not an OpenCode package dir"
  printf '%s' "$dir/node_modules/ensemble"
}

# The commit $ref names on GitHub now.
ref_commit() {
  local commit
  commit=$(curl -fsSL -H 'Accept: application/vnd.github.sha' "https://api.github.com/repos/windroc-labs/ensemble/commits/$ref" </dev/null) \
    || die "cannot resolve $ref on github.com/windroc-labs/ensemble"
  [[ $commit =~ ^[0-9a-f]{40}$ ]] || die "cannot resolve $ref on github.com/windroc-labs/ensemble"
  printf '%s' "$commit"
}

# ensemble's OpenCode plugin, the way OpenCode installs one: opencode plugin -g, which adds it to the global config
# and downloads it. The config there before is kept for the cleanup; the files OpenCode adds beside it are recorded.
# OpenCode never downloads a spec again, so when $ref has moved since the last download its copy is deleted first.
opencode_plugin() {
  local spec conf file name dir commit had
  step "OpenCode plugin"
  if ! has opencode; then
    skip "OpenCode not chosen"
    return
  fi
  spec=$(opencode_spec)
  conf=${XDG_CONFIG_HOME:-$HOME/.config}/opencode
  dir=$(opencode_cache_dir "$spec") && under_home "$dir" || die "$spec: not an OpenCode package dir"
  commit=$(ref_commit)
  had=$(state_get opencode_plugin_commit)
  if ! grep -qsF "\"$spec\"" "$conf/opencode.json" "$conf/opencode.jsonc"; then
    if ! consent "$conf/opencode.json(c)" "\"plugin\": [\"$spec\"]  (opencode plugin -g -f)"; then
      skip declined
      return
    fi
  elif [ -d "$dir/node_modules/ensemble" ] && [ "$had" = "$commit" ]; then
    skip "$spec at ${commit:0:12}"
    return
  else
    [ -d "$dir" ] && [ -n "$had" ] || had=unknown
    say "    $spec: $ref is at ${commit:0:12}, OpenCode's copy at ${had:0:12}: downloading it again"
  fi
  for file in "$conf/opencode.json" "$conf/opencode.jsonc" "$conf/package.json" "$conf/package-lock.json" "$conf/.gitignore"; do
    if [ -f "$file" ] && ! recorded file "$file" && ! recorded opencode-config "$file"; then
      cp "$file" "$state_dir/${file##*/}.orig"
      ledger_add opencode-config "$file"
    fi
  done
  for name in opencode.jsonc package.json package-lock.json .gitignore; do
    record_file "$conf/$name"
  done
  record_dir "$conf/node_modules"
  record_dir "${XDG_CACHE_HOME:-$HOME/.cache}/opencode/packages"
  record_dir "${XDG_CACHE_HOME:-$HOME/.cache}/opencode/packages/${spec%%//*}"
  rm -rf -- "$dir"
  "$opencode_executable" plugin -g -f "$spec" </dev/null >&2
  [ -d "$dir/node_modules/ensemble" ] || die "OpenCode did not download $spec"
  state_set opencode_plugin_commit "$commit"
}

# Shared with install-cleanup.sh; edits JSONC values without rewriting unrelated text.
opencode_config_edit() {
  python3 -I -c '
import json, os, re, sys
from pathlib import Path

strings = r"\"(?:[^\"\\]|\\.)*\""
comments = re.compile(strings + r"|//[^\r\n]*|/\*[\s\S]*?\*/")
commas = re.compile(strings + r"|,(?=\s*[}\]])")
decoder = json.JSONDecoder()

def masked(text):
    return comments.sub(lambda m: m[0] if m[0].startswith("\"") else re.sub(r"[^\r\n]", " ", m[0]), text)

def notes(text):
    return [m[0] for m in comments.finditer(text) if not m[0].startswith("\"")]

def parse(text):
    clean = commas.sub(lambda m: " " if m[0] == "," else m[0], masked(text))
    if clean.startswith("\ufeff"):
        clean = " " + clean[1:]
    data = json.loads(clean or "{}")
    if not isinstance(data, dict):
        raise ValueError()
    return data, clean

def load(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""

def write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)

def space(clean, index):
    while index < len(clean) and clean[index].isspace():
        index += 1
    return index

def members(clean, start):
    index = space(clean, start + 1)
    items = []
    while clean[index] != "}":
        key_start = index
        key, index = decoder.raw_decode(clean, index)
        index = space(clean, index)
        value_start = space(clean, index + 1)
        _, end = decoder.raw_decode(clean, value_start)
        items.append((key, key_start, value_start, end))
        index = space(clean, end)
        if clean[index] == ",":
            index = space(clean, index + 1)
    return items, index

def edit(text, keys, value, remove=False):
    _, clean = parse(text)
    start = space(clean, 0)
    for key in keys[:-1]:
        fields, _ = members(clean, start)
        start = next(field[2] for field in reversed(fields) if field[0] == key)
    fields, end = members(clean, start)
    found = next((field for field in reversed(fields) if field[0] == keys[-1]), None)
    if found:
        _, key_start, value_start, value_end = found
        if not remove:
            carry = "".join("\n" + note + "\n" for note in notes(text[value_start:value_end]))
            return text[:value_start] + carry + json.dumps(value, indent=2) + text[value_end:]
        carry = "".join("\n" + note + "\n" for note in notes(text[key_start:value_end]))
        raw = masked(text)
        after = space(raw, value_end)
        if raw[after:after + 1] == ",":
            return text[:key_start] + carry + text[value_end:after] + text[after + 1:]
        previous = next((f for f in reversed(fields) if f[3] < key_start), None)
        if previous:
            comma = raw.index(",", previous[3], key_start)
            return text[:comma] + text[comma + 1:key_start] + carry + text[value_end:]
        return text[:key_start] + carry + text[value_end:]
    prefix = "," if fields and "," not in masked(text)[fields[-1][3]:end] else ""
    addition = prefix + "\n  " + json.dumps(keys[-1]) + ": " + json.dumps(value, indent=2) + "\n"
    return text[:end] + addition + text[end:]

def run():
    action, raw_path, *args = sys.argv[1:]
    path = Path(raw_path)
    if action == "plan":
        state, name, url, model, window = args
        state = Path(state)
        plan = state / "opencode-provider.plan"
        plan.unlink(missing_ok=True)
        files = [path / "opencode.json", path / "opencode.jsonc"]
        loaded = [(file, *parse(load(file))) for file in files]
        for _, data, _ in loaded:
            providers = data.get("provider", {})
            if not isinstance(providers, dict):
                raise ValueError()
            if name in providers:
                return
        for _, data, _ in loaded:
            enabled = data.get("enabled_providers")
            disabled = data.get("disabled_providers", [])
            if (enabled is not None and (not isinstance(enabled, list) or name not in enabled)
                    or not isinstance(disabled, list) or name in disabled):
                raise ValueError()
        path = next((file for file in reversed(files) if file.exists()), files[-1])
        text = load(path) or "{}\n"
        data, _ = parse(text)
        window = int(window)
        output = min(32000, window // 4)
        value = {"npm": "@ai-sdk/openai-compatible", "name": name,
                 "options": {"baseURL": url}, "models": {model: {"name": model,
                 "limit": {"context": window, "input": window - min(output + 20000, window // 2), "output": output}}}}
        new = edit(text, ["provider", name], value) if "provider" in data else edit(text, ["provider"], {name: value})
        record = {"value": value, "container": "provider" in data, "created": not path.exists()}
        write(state / ("opencode-provider-" + name + ".json"), json.dumps(record))
        write(plan, new)
        print(path)
    elif action == "remove-provider":
        state, name = args
        if not path.exists():
            return
        expected = json.loads((Path(state) / ("opencode-provider-" + name + ".json")).read_text())
        text = load(path)
        data, _ = parse(text)
        providers = data.get("provider", {})
        if not isinstance(providers, dict) or name not in providers or providers[name] != expected["value"]:
            return
        keys = ["provider"] if len(providers) == 1 and not expected["container"] else ["provider", name]
        new = edit(text, keys, None, remove=True)
        remaining, _ = parse(new)
        if expected["created"] and not remaining and not notes(new) and not path.is_symlink() and path.parent != Path(state):
            path.unlink()
        else:
            write(path, new)
    else:
        raise ValueError()

try:
    run()
except (OSError, ValueError, KeyError, TypeError, StopIteration, RecursionError):
    sys.exit("install: OpenCode config edit failed for " + sys.argv[2] + "; check JSONC, provider filters and installer records")
' "$@"
}

opencode_provider() {
  local conf path
  step "OpenCode provider"
  if ! has opencode; then
    skip "OpenCode not chosen"
    return
  fi
  conf=${XDG_CONFIG_HOME:-$HOME/.config}/opencode
  path=$(opencode_config_edit plan "$conf" "$state_dir" "$provider" "$url" "$model" "$context")
  if [ -z "$path" ]; then
    skip "$provider in $conf/opencode.json(c)"
    return
  fi
  if ! consent "$path" "provider: $provider (URL, model, context; credentials stay in auth.json)"; then
    rm -f "$state_dir/opencode-provider.plan"
    skip declined
    return
  fi
  ledger_add opencode-provider "$path" "$provider"
  mkdir -p "$conf"
  cp "$state_dir/opencode-provider.plan" "$path"
  rm -f "$state_dir/opencode-provider.plan"
}

opencode_auth_file() { printf '%s' "${XDG_DATA_HOME:-$HOME/.local/share}/opencode/auth.json"; }

# Checks $1 in auth.json: $2=entry for ledger ownership, api for a usable key; no key is read out.
opencode_has_auth() {
  python3 -I -c '
import json, sys, unicodedata
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    if sys.argv[3] == "entry":
        sys.exit(0 if isinstance(data, dict) and sys.argv[2] in data else 1)
    entry = data.get(sys.argv[2]) if isinstance(data, dict) else None
    key = entry.get("key") if isinstance(entry, dict) and entry.get("type") == "api" else None
    valid = isinstance(key, str) and 0 < len(key.encode("utf-8")) <= 16384 and not any(
        c.isspace() or unicodedata.category(c) == "Cc" or c == "\ufffd" for c in key)
    sys.exit(0 if valid else 1)
except (OSError, ValueError):
    sys.exit(1)' "$(opencode_auth_file)" "$1" "$2"
}

# Adds {"<name>": {"type": "api", "key": <stdin line>}} to auth.json: 0600, atomic, others kept.
opencode_store_key() {
  python3 -I -c '
import json, os, sys, tempfile
path, name = sys.argv[1:]
key = sys.stdin.readline().rstrip("\n")
if not key:
    sys.exit("install.sh: no OpenCode key on stdin")
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}
except ValueError:
    sys.exit(f"install.sh: {path} is not JSON; nothing written")
data[name] = {"type": "api", "key": key}
os.makedirs(os.path.dirname(path), exist_ok=True)
fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".auth.json.")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, path)
except BaseException:
    os.unlink(temp)
    raise' "$(opencode_auth_file)" "$provider"
}

# Runs login command "$@" on the terminal until it succeeds (exit 0), or the user stops (exit 1).
key_login() {
  local rc
  until "$@" <&3 >&3 2>&3; do
    rc=$?
    # Broker exit 11 with an ownership diagnostic needs deletion, not another login attempt.
    [ "$rc" != 11 ] || return 1
    ask_yn "No $provider key stored. Try again?" || return 1
  done
}

# OpenCode's own login, which stores the key in auth.json; exit 1 when no $provider key landed there.
opencode_login() {
  "$opencode_executable" auth login && opencode_has_auth "$provider" api
}

opencode_key() {
  local sha later
  step "OpenCode key"
  if ! has opencode; then
    skip "OpenCode not chosen"
    return
  fi
  if opencode_has_auth "$provider" api; then
    skip "$provider in $(opencode_auth_file)"
  else
    opencode_has_auth "$provider" entry || ledger_add opencode-auth "$(opencode_auth_file)" "$provider"
    if [ -n "$key_stdin" ]; then
      opencode_store_key
    else
      say "In opencode auth login pick Other, enter $provider, then paste its API key"
      later="rerun install.sh, or run \"$opencode_executable\" auth login"
      if ! key_login opencode_login; then
        skip "no $provider key: $later"
        return
      fi
    fi
  fi
  if [ "$docker" = yes ]; then
    sha=$(shasum -a 256 "$broker" | cut -d' ' -f1)
    if [ "$(state_get login_opencode)" = "$sha $provider" ] \
      && security find-generic-password -s ensemble-opencode -a "$provider" </dev/null >/dev/null 2>&1; then
      skip "$provider logged in to this broker"
    else
      record_item ensemble-opencode "$provider"
      if key_login "$broker" login opencode "$provider" --auth-file "$(opencode_auth_file)"; then
        state_set login_opencode "$sha $provider"
      else
        skip "no $provider key: rerun install.sh, or run $broker login opencode $provider --auth-file $(opencode_auth_file)"
      fi
      case $(python3 -I -c 'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.argv[1]).hostname or "")' "$url") in
        localhost | 127.0.0.1) ;;
        *) say "The first worker that reaches $url makes macOS ask \"Allow Docker to find devices on local networks?\": click Allow (else System Settings › Privacy & Security › Local Network › Docker), or $provider stays unreachable" ;;
      esac
    fi
  fi
}

# Installer-owned adapters also support refs whose launchers predate source selection.
command_source() {
  if has opencode; then
    printf 'opencode\n%s/opencode/packages\n' "${XDG_CACHE_HOME:-$HOME/.cache}"
  elif has claude; then
    printf 'claude\n%s/plugins/installed_plugins.json\n' "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  else
    printf 'codex\n%s/plugins/cache/windroc/ensemble\n' "${CODEX_HOME:-$HOME/.codex}"
  fi
}

command_selection() {
  local client source
  { read -r client; read -r source; } < <(command_source)
  python3 -I -c 'import json, sys; print(json.dumps(dict(root=sys.argv[1], client=sys.argv[2], source=sys.argv[3])))' "$pkg" "$client" "$source"
}

command_launcher() {
  local client source
  { read -r client; read -r source; } < <(command_source)
  printf '#!/bin/bash\ntarget=$(python3 -I - %q %q %q "${0##*/}" <<\x27PY\x27\n' "$pkg" "$client" "$source"
  cat <<'EOF'
import glob, json, os, sys, tempfile

root, client, source, name = sys.argv[1:]
selection = os.path.expanduser("~/.ensemble/plugin-root")
rel = "skills/manage/scripts/ctmux" if name == "ctmux" else "bin/" + name
try:
    with open(selection, encoding="utf-8") as f:
        text = f.read().rstrip("\n")
    try:
        data = json.loads(text)
    except ValueError:
        data = {"root": text, "client": client, "source": source}
    root, client, source = data["root"], data["client"], data["source"]
except (OSError, ValueError, TypeError, KeyError) as e:
    sys.exit(f"{name}: cannot read {selection}: {e}; rerun install.sh or the plugin's ctmux --install")
path = os.path.join(root, rel)
if os.path.isabs(root) and "\n" not in root and os.access(path, os.X_OK):
    print(path)
    sys.exit()

def version(text):
    return [int(part) if part.isdigit() else -1 for part in text.split(".")]

def package_version(path):
    manifest = ".claude-plugin/plugin.json" if client == "claude" else "package.json"
    try:
        with open(os.path.join(path, manifest), encoding="utf-8") as f:
            return version(str(json.load(f).get("version")))
    except (OSError, ValueError, AttributeError):
        return version(os.path.basename(path)) if client == "claude" else []

roots = []
if os.path.isabs(root) and "\n" not in root and not os.path.exists(root):
    if client == "claude":
        try:
            with open(source, encoding="utf-8") as f:
                entries = json.load(f)["plugins"]["ensemble@windroc"]
            roots = [e.get("installPath") for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
        except (OSError, ValueError, LookupError, TypeError):
            pass
    elif client == "codex":
        roots = glob.glob(os.path.join(glob.escape(source), "*"))
    elif client == "opencode":
        for top, dirs, _ in os.walk(source):
            if "node_modules" in dirs:
                dirs.remove("node_modules")
                roots.append(os.path.join(top, "node_modules", "ensemble"))
    roots = sorted(p for p in roots if isinstance(p, str) and os.path.isabs(p) and "\n" not in p)
    key = (lambda p: version(os.path.basename(p))) if client == "codex" else package_version
    for candidate in sorted(roots, key=key, reverse=True):
        if os.access(path := os.path.join(candidate, rel), os.X_OK):
            fd, temp = tempfile.mkstemp(prefix=".plugin-root.", dir=os.path.dirname(selection))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(dict(root=candidate, client=client, source=source), f)
                    f.write("\n")
                os.replace(temp, selection)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            print(f"{name}: re-selected plugin {candidate}", file=sys.stderr)
            print(path)
            sys.exit()
sys.exit(f"{name}: selected plugin {root!r} has no executable {rel}; rerun install.sh or the plugin's ctmux --install")
PY
) || exit
exec "$target" "$@"
EOF
}

ctmux_install() {
  local name missing=
  step "ctmux --install"
  [ "$(cat "$HOME/.ensemble/plugin-root" 2>/dev/null)" = "$(command_selection)" ] || missing=1
  for name in ctmux ensemble-crew ensemble-session ensemble-delegate ensemble-orchestrator; do
    [ -x "$HOME/.ensemble/bin/$name" ] && cmp -s <(command_launcher "$name") "$HOME/.ensemble/bin/$name" || missing=1
  done
  if [ -n "$missing" ]; then
    if ! recorded file "$HOME/.ensemble/plugin-root" && ! recorded plugin-root; then
      if absent "$HOME/.ensemble/plugin-root"; then
        record_file "$HOME/.ensemble/plugin-root"
      else
        cp -P "$HOME/.ensemble/plugin-root" "$state_dir/plugin-root.orig.tmp"
        mv -f "$state_dir/plugin-root.orig.tmp" "$state_dir/plugin-root.orig"
        ledger_add plugin-root
      fi
    fi
    if [ -d "$HOME/.ensemble/bin" ]; then
      for name in ctmux ensemble-crew ensemble-session ensemble-delegate ensemble-orchestrator; do
        record_file "$HOME/.ensemble/bin/$name"
      done
    else
      record_dir "$HOME/.ensemble/bin"
    fi
    "$pkg/skills/manage/scripts/ctmux" --install </dev/null >&2
    for name in ctmux ensemble-crew ensemble-session ensemble-delegate ensemble-orchestrator; do
      command_launcher "$name" > "$state_dir/launcher.tmp"
      install -m 755 "$state_dir/launcher.tmp" "$HOME/.ensemble/bin/$name"
    done
    rm -f "$state_dir/launcher.tmp"
    command_selection > "$state_dir/plugin-root.tmp"
    mv -f "$state_dir/plugin-root.tmp" "$HOME/.ensemble/plugin-root"
  else
    skip "launchers in ~/.ensemble/bin"
  fi
  profile_line 'export PATH="$HOME/.ensemble/bin:$PATH"' "$HOME/.ensemble/bin"
  if has opencode && [ "$opencode_executable" = "$opencode_prefix/bin/opencode" ]; then
    profile_line 'export PATH="$HOME/.ensemble/opencode/bin:$PATH"' "$opencode_prefix/bin"
  fi
}

# sha256 of the plugin's broker sources: a change means the broker must be reinstalled.
broker_source() {
  (
    cd "$1"
    find src/broker -type f | LC_ALL=C sort | while IFS= read -r file; do shasum -a 256 "$file"; done
    shasum -a 256 .claude-plugin/plugin.json
  ) | shasum -a 256 | cut -d' ' -f1
}

# Stops unless Docker.app is built for this Mac's CPU and macOS: the cask's download is picked by CPU only.
docker_build() {
  local app=$root/Applications/Docker.app info archs cpu os
  info=$(python3 -I -c '
import plistlib, sys
with open(sys.argv[1], "rb") as f:
    info = plistlib.load(f)
print(info["CFBundleExecutable"], info.get("LSMinimumSystemVersion", "0"), info.get("CFBundleShortVersionString", "?"))' \
    "$app/Contents/Info.plist") || die "cannot read $app/Contents/Info.plist"
  set -- $info
  archs=$(lipo -archs "$app/Contents/MacOS/$1" </dev/null) || die "cannot read the CPU types of $app"
  cpu=$(uname -m)
  case " $archs " in
    *" $cpu "*) ;;
    *) die "Docker Desktop $3 is built for $archs, not this Mac's $cpu: brew reinstall --cask docker-desktop" ;;
  esac
  os=$(sw_vers -productVersion </dev/null)
  python3 -I -c '
import sys
def v(text):
    return [int(p) for p in text.split(".") if p.isdigit()]
sys.exit(v(sys.argv[1]) < v(sys.argv[2]))' "$os" "$2" || die "Docker Desktop $3 needs macOS $2 or later, not $os"
  say "    Docker Desktop $3 for $archs, macOS $2+: matches $cpu, macOS $os"
}

# Presets Docker Desktop's settings before its first start, keeping any it has: Rosetta off (the worker image is
# arm64; install.sh never installs Rosetta) and its onboarding marked done. Best effort: macOS 27 may deny the shell
# Docker's Group Container; any failure prints one line, keeps the file as it was, and the install goes on.
docker_settings() {
  python3 -I -c '
import contextlib, json, os, sys
path = sys.argv[1]
preset = {"UseVirtualizationFrameworkRosetta": False, "DisplayedOnboarding": True}
try:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"not a JSON object: {path}")
    new = {**preset, **data}
    if new != data:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(new, f, indent=2)
            os.replace(path + ".tmp", path)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(path + ".tmp")
            raise
except Exception as e:
    print(f"    Docker Desktop presets not written (Rosetta off, onboarding done): {e}; Docker Desktop may show its"
          " Welcome window", file=sys.stderr)' "$HOME/Library/Group Containers/group.com.docker/settings-store.json"
}

# Starts Docker Desktop and waits for its engine; the first time install.sh installed it, without its windows:
# license accepted and privileged setup done by its own install command, settings preset where macOS allows.
docker_running() {
  local app=$root/Applications/Docker.app tries=0
  step "Docker Desktop running"
  if docker info </dev/null >/dev/null 2>&1; then
    skip running
    return
  fi
  if recorded cask docker-desktop; then
    if [ "$(state_get docker_license)" != accepted ]; then
      sudo -A "$app/Contents/MacOS/install" --accept-license --user="$(id -un)" </dev/null >&2
      state_set docker_license accepted
    fi
    docker_settings
  fi
  open -a Docker </dev/null
  say "    waiting for Docker Desktop's engine (up to 5 minutes)"
  while ! docker info </dev/null >/dev/null 2>&1; do
    tries=$((tries + 1))
    [ "$tries" -le 100 ] || die "Docker Desktop did not start in 5 minutes: check its window, then rerun"
    sleep 3
  done
}

# Prints "<spoken>:<wanted>", whatever its exit status: the protocol the installed broker reports when it was
# installed from plugin $1's broker sources (else empty), and $1's plugin protocol. Exit 0 when both are set and
# equal: the broker is current and broker_install keeps it, so broker_plan may trust it.
broker_current() {
  local got= want=
  want=$(protocol_of <"$1/src/ensemble/session/hosts/docker/broker.py" 2>/dev/null) || want=
  if [ -x "$broker" ] && [ "$(state_get broker_source)" = "$(broker_source "$1")" ]; then
    got=$("$broker" version </dev/null 2>/dev/null | spoken_protocol_of) || got=
  fi
  printf '%s:%s\n' "$got" "$want"
  [ -n "$want" ] && [ "$got" = "$want" ]
}

# Installs the broker unless the one installed came from this plugin's sources and speaks its protocol.
broker_install() {
  local source installed got want
  step broker
  if installed=$(broker_current "$pkg"); then
    skip "installed from this plugin"
    return
  fi
  got=${installed%%:*}
  want=${installed#*:}
  if [ -n "$got" ]; then say "    installed broker speaks protocol $got, the plugin wants ${want:-unknown}: reinstalling"; fi
  source=$(broker_source "$pkg")
  [ -e "$root/usr/local/libexec/ensemble" ] || ledger_add broker
  record_dir "$HOME/.ensemble/docker"
  if [ "$broker_mode" = release ]; then
    sh "$pkg/src/broker/install.sh" </dev/null >&2
  else
    sh "$pkg/src/broker/install.sh" --build </dev/null >&2
  fi
  state_set broker_source "$source"
}

# Asks for the ensemble-bot App key's .pem and App ID into app_pem and app_id, for the broker to store; 1 when the
# user skips it.
store_app_key() {
  [ "$(state_get github_app_key)" != skipped ] || return 1
  ask "Path to your GitHub App's private key (.pem; docs/docker-host.md#create-the-github-app), empty to skip:"
  if [ -z "$answer" ]; then
    state_set github_app_key skipped
    return 1
  fi
  app_pem=$answer
  [ -f "$app_pem" ] || die "no file $app_pem"
  while :; do
    ask "Its App ID (digits):"
    if [[ $answer =~ ^[1-9][0-9]{0,19}$ ]]; then break; fi
  done
  app_id=$answer
  ledger_add keychain ensemble-bot "$app_id"
}

# Claude's default credential or pinned Codex: install, update, login, ready or busy.
broker_need() {
  "$broker" status --cleanup </dev/null | python3 -I -c '
import json, sys
data = json.load(sys.stdin)
if "audit_warning" in data:
    if data["audit_warning"] == "audit_write_failed":
        print("audit_write_failed: broker audit persistence failed; the operation result still applies. Do not repeat a successful start.", file=sys.stderr)
    else:
        print("ensemble-broker: invalid audit warning metadata; the operation result still applies.", file=sys.stderr)
if sys.argv[1] == "claude":
    print("ready" if (data.get("claude") or {}).get("default") is True else "login")
    sys.exit(0)
codex = data.get("codex") or {}
state = codex.get("state", "")
def version(text):
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return (0,)
if state == "busy":
    print("busy")
elif state == "installation_invalid":
    print("install" if "install-codex" in codex.get("repair", "") else "update")
elif version(codex.get("version", "")) < version(sys.argv[2]):
    print("update")
else:
    print("ready" if state == "ready" else "login")' "$@"
}

# Copies stdin to the terminal and to stdout.
to_terminal() {
  local line
  while IFS= read -r line; do
    printf '%s\n' "$line" >&3
    printf '%s\n' "$line"
  done
}

# The token screen: the terminal's alternate screen, which keeps no scrollback, cleared on entering and on leaving,
# for claude setup-token's token and its paste.
token_screen_enter() {
  printf '\033[?1049h\033[H\033[2J' >&3
  token_screen=1
}

token_screen_leave() {
  if [ -n "$token_screen" ]; then
    printf '\033[H\033[2J\033[?1049l' >&3
    token_screen=
  fi
}

broker_logins() {
  local sha min need said slug
  sha=$(shasum -a 256 "$broker" | cut -d' ' -f1)
  step "broker login: Claude"
  if ! has claude; then
    skip "Claude Code not chosen"
  elif [ "$(broker_need claude)" = ready ]; then
    skip "default Claude credential usable by this broker"
    if [ "$(state_get login_claude)" != "$sha" ]; then state_set login_claude "$sha"; fi
  else
    say "Create a token with claude setup-token, then paste it at the broker's prompt; both run on a screen cleared afterwards"
    record_item ensemble-claude default
    token_screen_enter
    if ! { claude setup-token <&3 >&3 2>&3 && "$broker" login claude <&3 >&3 2>&3; }; then
      ask "Press Enter to leave this screen:"
      exit 1
    fi
    token_screen_leave
    say "    setup-token saved in the broker; its screen is cleared"
    state_set login_claude "$sha"
  fi
  step "broker login: GitHub"
  if [ "$(state_get login_github)" = "$sha" ] && [ -n "$(state_get github_app)" ]; then
    skip "done for this broker"
  elif ! security find-generic-password -s ensemble-bot </dev/null >/dev/null 2>&1 && ! store_app_key; then
    skip "no GitHub App key: create the App by docs/docker-host.md#create-the-github-app, then rerun"
  else
    record_item ensemble-github-users ensemble-bot
    if [ -n "$app_id" ]; then
      said=$("$broker" login github ensemble-bot --key-file "$app_pem" --app-id "$app_id" <&3 2>&3 | to_terminal)
    else
      said=$("$broker" login github ensemble-bot <&3 2>&3 | to_terminal)
    fi
    slug=$(sed -n 's/^GitHub App \([A-Za-z0-9-]*\) bot user .*/\1/p' <<<"$said")
    [ -n "$slug" ] || die "broker login github named no GitHub App"
    state_set github_app "$slug"
    state_set login_github "$sha"
  fi
  if [ -n "$(state_get github_app)" ]; then config_file session.hosts.docker.github app "$(state_get github_app)"; fi
  step "broker Codex install"
  if ! has codex; then
    skip "Codex not chosen"
    step "broker login: Codex"
    skip "Codex not chosen"
  else
    min=$(go_const src/broker/internal/codex/versions.go MinVersion)
    need=$(broker_need codex "$min")
    case $need in
      busy) die "broker busy; rerun" ;;
      install | update) sudo -A "$broker" "$need-codex" "$min" </dev/null >&2 ;;
      *) skip "Codex $min or newer installed" ;;
    esac
    step "broker login: Codex"
    if [ "$(broker_need codex "$min")" = ready ]; then
      skip "logged in"
    else
      record_item ensemble-codex auth
      if ! "$broker" login codex <&3 >&3 2>&3; then
        if security find-generic-password -s ensemble-codex -a auth </dev/null >/dev/null 2>&1; then
          say "ensemble-codex/auth is unavailable to this broker. If another process created it, delete this item from the login keychain in Keychain Access, then rerun install.sh."
        fi
        return 1
      fi
    fi
  fi
}

# 0: ready for workers; 2: healthy contract-1 proxy whose config has no workers section; 1: not ready yet.
egress_ready() {
  local status running health contract name proxy networks
  status=$(docker inspect --format '{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{index .Config.Labels "windroc.egress.contract"}}|{{index .Config.Labels "windroc.egress.network.workers.name"}}|{{index .Config.Labels "windroc.egress.network.workers.proxy"}}|{{index .Config.Labels "windroc.egress.networks"}}' egress-proxy </dev/null 2>/dev/null) || return 1
  IFS='|' read -r running health contract name proxy networks <<<"$status"
  [ "$running|$health|$contract" = "true|healthy|1" ] || return 1
  case ",$networks," in *,workers,*) ;; *) return 2 ;; esac
  case $name in '' | '<no value>') return 1 ;; esac
  case $proxy in '' | '<no value>') return 1 ;; esac
}

# Download to a sibling directory so a failed fetch never leaves a usable partial version.
egress_download() (
  local versions=$HOME/.egress/versions tmp
  tmp=$(mktemp -d "$versions/.download.XXXXXX")
  trap 'rm -rf "$tmp"' EXIT
  gh api "repos/windroc/egress-proxy/tarball/$egress_commit" </dev/null >"$tmp/source.tar.gz" \
    || die "cannot fetch pinned windroc/egress-proxy $egress_commit; check gh auth status and repository access, then rerun"
  mkdir "$tmp/source"
  tar -xzf "$tmp/source.tar.gz" --strip-components=1 -C "$tmp/source" \
    || die "cannot unpack pinned windroc/egress-proxy $egress_commit"
  [ -x "$tmp/source/start.sh" ] || die "pinned egress version has no executable start.sh"
  printf '%s\n' "$egress_commit" >"$tmp/source/COMMIT"
  mv "$tmp/source" "$versions/$egress_commit"
)

egress_install() {
  local dir=$HOME/.egress version=$HOME/.egress/versions/$egress_commit tries=0 ready
  step "egress proxy"
  mkdir -p "$dir/versions"
  [ ! -L "$dir/config.local.yaml" ] \
    || die "$dir/config.local.yaml symlink is unsupported by the proxy's directory mount; existing path was kept"
  if [ ! -e "$dir/config.local.yaml" ]; then
    (umask 077; set -o noclobber; printf 'version: 1\nnetworks:\n  workers:\n    mode: denylist\n' >"$dir/config.local.yaml") \
      || die "cannot create $dir/config.local.yaml; existing config was kept"
  fi
  [ -f "$dir/config.local.yaml" ] && [ -r "$dir/config.local.yaml" ] \
    || die "$dir/config.local.yaml is not a readable file; existing path was kept"
  if [ ! -e "$version" ]; then egress_download; fi
  [ -x "$version/start.sh" ] && [ "$(cat "$version/COMMIT")" = "$egress_commit" ] \
    || die "incomplete egress checkout $version; keep your config and repair that version directory"
  # Always run start.sh: it applies a new or changed config and leaves an unchanged proxy running.
  EGRESS_CONFIG_DIR="$dir" bash "$version/start.sh" </dev/null >&2 \
    || die "egress-proxy start failed: run $version/start.sh; inspect docker logs egress-proxy"
  while :; do
    ready=0
    egress_ready || ready=$?
    [ "$ready" -ne 0 ] || break
    [ "$ready" -ne 2 ] \
      || die "$dir/config.local.yaml has no networks.workers section, so the healthy proxy serves no workers network; add one (see $version/config.example.yaml), then rerun scripts/install.sh --docker; your config was kept unchanged"
    [ "$tries" -lt 60 ] \
      || die "egress-proxy not healthy after 120 seconds: docker logs egress-proxy; rerun $version/start.sh"
    tries=$((tries + 1))
    sleep 2
  done
  if [ -e "$dir/current" ] || [ -L "$dir/current" ]; then
    [ -L "$dir/current" ] || die "$dir/current is not an installer symlink; kept it"
    rm "$dir/current"
  fi
  ln -s "$version" "$dir/current"
  state_set egress_commit "$egress_commit"
}

docker_steps() {
  local name
  if [ "$docker" != yes ]; then
    for name in "Docker Desktop running" "egress proxy" broker "broker login: Claude" "broker login: GitHub" \
      "broker Codex install" "broker login: Codex"; do
      step "$name"
      skip "Docker not chosen"
    done
    return
  fi
  docker_running
  egress_install
  broker_install
  docker_cli
  broker_logins
}

# Points [session.hosts.docker] docker at this Mac's docker CLI when the packaged /usr/local/bin/docker is not there.
docker_cli() {
  local cli
  if [ -x "$root/usr/local/bin/docker" ] || ! cli=$(command -v docker); then return 0; fi
  config_file session.hosts.docker docker "$cli"
}

# configured_tracker <config_says snapshot>: prints only a recognized tracker; absent stays empty.
configured_tracker() {
  local tracker=${1#*$'\t'}
  tracker=${tracker#*$'\t'}
  case $tracker in local | linear) printf '%s' "$tracker" ;; esac
}

board_wanted() {
  if [ "$board" != yes ]; then
    skip "board not chosen"
    return 1
  fi
}

linear_wanted() {
  board_wanted || return 1
  if [ "$board_tracker" != linear ]; then
    skip "local board chosen"
    return 1
  fi
}

# "<role>\t<input>" for each [[stage]] in the workflow the orchestrator loads: config.local.toml's [orchestrator]
# workflow, else the plugin's packaged one.
stage_roles() {
  python3 -I -c '
import os, re, sys, tomllib
config, packaged = sys.argv[1:]
path = ""
if os.path.exists(config):
    with open(config, "rb") as f:
        path = tomllib.load(f).get("orchestrator", {}).get("workflow", "")
path = os.path.expanduser(path) if isinstance(path, str) and path else packaged
try:
    with open(path, "rb") as f:
        stages = [(stage.get("role"), stage.get("input")) for stage in tomllib.load(f).get("stage", [])]
except (OSError, tomllib.TOMLDecodeError, AttributeError):
    stages = []
names = re.compile(r"[a-z][a-z0-9-]*")
if not stages or not all(isinstance(v, str) and names.fullmatch(v) for stage in stages for v in stage):
    sys.exit(f"install.sh: {path}: cannot read its [[stage]] roles")
print("\n".join(f"{role}\t{input_}" for role, input_ in stages))' "$config" "$pkg/src/ensemble/orchestrator/engine/flow/workflow.toml"
}

# linear_slots <stages>: slot, token service and secret service of each app: harness, manager, then the stage roles.
linear_slots() {
  python3 -I -c '
import os, re, sys, tomllib
config, roles = sys.argv[1], [line.split("\t")[0] for line in sys.argv[2].splitlines()]
# engine/config.py LINEAR_KEYS; tests/unit/install_board_test.py keeps them equal
KNOWN = {"team", "states", "human_members", "harness", "manager", "task_label_group", "task_labels",
         "agent_label_group", "agent_labels", "roles", "project_repos"}
UPGRADE = "; see docs/upgrading.md#linear-apps"
linear = {}
if os.path.exists(config):
    with open(config, "rb") as f:
        linear = tomllib.load(f).get("orchestrator", {}).get("linear", {})
def text(value):
    return value if isinstance(value, str) else ""
def refuse(table, extra):
    names = ", ".join(extra)
    sys.exit(f"install.sh: config.local.toml: unknown keys in [{table}]: {names}{UPGRADE}")
if not isinstance(linear, dict):
    sys.exit("install.sh: config.local.toml: [linear] must be a table")
if extra := sorted(set(linear) - KNOWN):
    refuse("linear", extra)
def table_of(table, found):
    if not isinstance(found, dict):
        sys.exit(f"install.sh: config.local.toml: [{table}] must be a table")
    if extra := sorted(set(found) - {"client_id", "secret_service", "token_service", "user_id", "renewal"}):
        refuse(table, extra)
    if any(not isinstance(value, str) for value in found.values()):
        sys.exit(f"install.sh: config.local.toml: [{table}] app fields must be strings")
    if found.get("renewal", "transport") not in ("transport", "broker"):
        sys.exit(f"install.sh: config.local.toml: [{table}] renewal must be transport or broker")
    return found
role_tables = linear.get("roles", {})
if not isinstance(role_tables, dict):
    sys.exit("install.sh: config.local.toml: [linear.roles] must be a table")
# every role table, a stage role or not, as the orchestrator checks them all
for name in sorted(role_tables):
    table_of(f"linear.roles.{name}", role_tables[name])
slots = [(slot, f"linear.{slot}", table_of(f"linear.{slot}", linear.get(slot, {}))) for slot in ("harness", "manager")]
slots += [(role, f"linear.roles.{role}", role_tables.get(role, {})) for role in roles]
used = {}
for slot, table, found in slots:
    services = [(field, text(found.get(field)) or f"linear-app-{slot}-{kind}")
                for field, kind in (("token_service", "token"), ("secret_service", "secret"))]
    for field, value in services:
        where = f"{table}.{field}"
        if not re.fullmatch(r"[A-Za-z0-9._@+][A-Za-z0-9._@+-]*", value):
            sys.exit(f"install.sh: config.local.toml: {where}: not a plain keychain name")
        if value in used:
            sys.exit(f"install.sh: config.local.toml: {where} is also {used[value]}")
        used[value] = where
    print(f"{slot}\t{services[0][1]}\t{services[1][1]}")' "$config" "$1"
}

# GraphQL reads app token records from Keychain; curl gets authorization on stdin (-K -).
linear_client='
import json, subprocess, sys
class LinearError(Exception):
    pass
def shown(text):
    return text if text.isprintable() else repr(text)
def linear(service, query, variables):
    found = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
    try:
        token = json.loads(found.stdout.strip())["access_token"]
    except (ValueError, KeyError, TypeError):
        raise LinearError(f"no usable app token in keychain item {service}") from None
    if found.returncode or not isinstance(token, str) or not token or not token.isprintable() or any(c.isspace() or c in "\"\\" for c in token):
        raise LinearError(f"no usable app token in keychain item {service}")
    authorization = f"Bearer {token}"
    body = json.dumps({"query": query, "variables": variables})
    sent = subprocess.run(["curl", "-q", "-fsS", "--connect-timeout", "10", "--max-time", "30", "-K", "-",
                           "--data-binary", body, "-H", "Content-Type: application/json",
                           "https://api.linear.app/graphql"],
                          input=f"header = \"Authorization: {authorization}\"\n", capture_output=True, text=True)
    if sent.returncode:
        raise LinearError("Linear request failed")
    try:
        answer = json.loads(sent.stdout)
    except json.JSONDecodeError:
        raise LinearError("not a JSON answer") from None
    errors = answer.get("errors") if isinstance(answer, dict) else None
    if errors or not isinstance(answer, dict) or not isinstance(answer.get("data"), dict):
        raise LinearError("Linear returned errors or no data")
    return answer["data"]
'

linear_apps() {
  local list=$1 slot token secret new_secret new_token writer
  step "Linear apps"
  linear_wanted || return 0
  mkdir -p "$state_dir"
  printf '{}\n' >"$state_dir/linear-apps.json"
  while IFS=$'\t' read -r slot token secret; do
    [ -n "$token" ] || continue
    writer=$(python3 -I "$pkg/scripts/install_linear_app.py" --writer "$config" "$slot") || exit 1
    if [ "$writer" = broker ]; then
      python3 -I "$pkg/scripts/install_linear_app.py" "$config" "$slot" "$token" "$secret" "$state_dir/linear-apps.json" || exit 1
      continue
    fi
    new_secret=no new_token=no
    if security find-generic-password -s "$secret" </dev/null >/dev/null 2>&1; then
      skip "$secret in the keychain"
    else
      ledger_add keychain "$secret" "$secret"
      say "Linear app $slot: paste its client secret at security's prompt (nothing echoes)"
      security add-generic-password -s "$secret" -a "$secret" -w <&3 >&3 2>&3
      new_secret=yes
    fi
    if ! security find-generic-password -s "$token" </dev/null >/dev/null 2>&1; then
      ledger_add keychain "$token" "$token"
      new_token=yes
    fi
    if ! python3 -I "$pkg/scripts/install_linear_app.py" "$config" "$slot" "$token" "$secret" "$state_dir/linear-apps.json"; then
      if [ "$new_secret" = yes ]; then security delete-generic-password -s "$secret" </dev/null >/dev/null 2>&1 || true; fi
      if [ "$new_token" = yes ]; then security delete-generic-password -s "$token" </dev/null >/dev/null 2>&1 || true; fi
      exit 1
    fi
  done <<<"$list"
}

# Exit 0 when config.local.toml has all the board config the orchestrator requires (engine/config.py's REQUIRED over
# the stage roles, and each document role's GitHub output), the manager app included, with tracker = "linear".
board_complete() {
  [ -f "$config" ] || return 1
  python3 -I -c '
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    data = tomllib.load(f)
orchestrator, delegate = data.get("orchestrator", {}), data.get("delegate", {})
linear = orchestrator.get("linear", {})
stages = [line.split("\t") for line in sys.argv[2].splitlines()]
def set_(node, *keys):
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return isinstance(node, str) and node != ""
app_keys = ("client_id", "secret_service", "token_service", "user_id")
required = [("team",), ("task_label_group",)]
required += [(app, key) for app in ("harness", "manager") for key in app_keys]
required += [("states", state) for state in ("backlog", "todo", "in_progress", "in_review", "handoff", "done", "canceled")]
required += [("roles", role, key) for role, _ in stages for key in app_keys]
def published(role):
    output = delegate.get("roles", {}).get(role, {}).get("output") or delegate.get("output", {})
    return output.get("type") == "github" and set_(output, "repo") and set_(output, "branch")
docs = all(published(role) for role, input_ in stages if input_ in ("research", "design"))
sys.exit(0 if orchestrator.get("tracker") == "linear" and docs and all(set_(linear, *keys) for keys in required) else 1)' \
    "$config" "$1"
}

# board_plan <out> <slots> <stages>: asks Linear (and the user, for the team and project repos) for the board config
# config.local.toml lacks; writes it to <out> as JSON [[table, key, value], ...]. Everything Linear says is checked
# before it is shown or planned.
board_plan() {
  python3 -I -c "$linear_client"'
import os, re, tomllib
config, out, slot_lines, stage_lines, charters, app_file = sys.argv[1:]
with open(app_file, encoding="utf-8") as f:
    apps = json.load(f)
slots = [line.split("\t") for line in slot_lines.splitlines()]
stages = [line.split("\t") for line in stage_lines.splitlines()]
service = {slot: name for slot, name, _ in slots}
roles = [slot for slot, _, _ in slots[2:]]
data = {}
if os.path.exists(config):
    with open(config, "rb") as f:
        data = tomllib.load(f)
def dig(node, *keys):
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node
def stop(text):
    sys.exit(f"install.sh: {text}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
def bad(field, ident=None):
    stop(f"Linear returned an invalid {field}" + (f" ({ident})" if ident else ""))
def uid(value, field):
    return value if isinstance(value, str) and UUID.fullmatch(value) else bad(field)
def name(value, field, ident):
    return value if isinstance(value, str) and value and value.isprintable() else bad(field, ident)
def email(value, field):
    ok = isinstance(value, str) and value.isprintable() and re.fullmatch(r"[^@\s]+@[^@\s]+", value)
    return value if ok else bad(field)
def nodes(node, *keys):
    found = dig(node, *keys, "nodes")
    return [n for n in found if isinstance(n, dict)] if isinstance(found, list) else []
def fetch(slot, query, variables=None):
    try:
        return linear(service[slot], query, variables or {})
    except LinearError as error:
        print(f"install.sh: Linear: {error}", file=sys.stderr)
        stop(f"Linear refused the app token in {service[slot]}, or cannot be reached: fix that keychain item, then rerun")
def ask(question):
    os.write(3, (question + " ").encode())
    line = b""
    while not line.endswith(b"\n"):
        chunk = os.read(3, 1)
        if not chunk:
            stop("no answer on the terminal")
        line += chunk
    return line.decode(errors="replace").strip()
linear_config = dig(data, "orchestrator", "linear") or {}
team = linear_config.get("team")
if team:
    if not isinstance(team, str) or not UUID.fullmatch(team):
        stop("config.local.toml: [orchestrator.linear] team must be a Linear team id (UUID)")
else:
    teams = []
    for found in nodes(fetch("harness", "{ teams(first: 250) { nodes { id key name } } }"), "teams"):
        ident = uid(found.get("id"), "team id")
        teams.append((ident, name(found.get("key"), "team key", ident)))
    if not teams:
        stop("the harness app sees no Linear team (docs/credentials.md#linear-presets)")
    team = teams[0][0]
    if len(teams) > 1:
        keys = {key: ident for ident, key in teams}
        listed = ", ".join(keys)
        while (answer := ask(f"Linear team for the board ({listed}):")) not in keys:
            pass
        team = keys[answer]
detail = dig(fetch("harness", "query($id: String!) { team(id: $id) { key states(first: 250) { nodes { id name } } "
                   "members(first: 250) { nodes { email active } } projects(first: 250) { nodes { id name } } } }",
                   {"id": team}), "team")
team_key = name(dig(detail, "key"), "team key", team)
states = {}
for found in nodes(detail, "states"):
    ident = uid(found.get("id"), "state id")
    states.setdefault(name(found.get("name"), "state name", ident), ident)
STATES = {"backlog": "Backlog", "todo": "Todo", "in_progress": "In Progress", "in_review": "In Review",
          "handoff": "Handoff", "done": "Done", "canceled": "Canceled"}
if missing := [title for title in STATES.values() if title not in states]:
    listed = ", ".join(missing)
    stop(f"Linear team {team_key} lacks states: {listed} (docs/credentials.md#linear-presets)")
labels = []
for found in nodes(fetch("harness", "query($id: ID) { issueLabels(first: 250, filter: { or: [{ team: { id: { eq: $id } } }, "
                         "{ team: { null: true } }] }) { nodes { id name isGroup parent { id } } } }", {"id": team}),
                   "issueLabels"):
    ident = uid(found.get("id"), "label id")
    parent = dig(found, "parent", "id")
    labels.append((ident, name(found.get("name"), "label name", ident), found.get("isGroup") is True,
                   uid(parent, "label parent id") if parent is not None else None))
def group(title):
    return next((ident for ident, label, is_group, _ in labels if is_group and label == title), None)
def children(parent):
    return {label: ident for ident, label, _, of in labels if of == parent}
tasks = linear_config.get("task_label_group") or group("Tasks")
if not tasks:
    stop(f"Linear team {team_key} has no Tasks label group (docs/credentials.md#linear-presets)")
humans = []
for found in nodes(detail, "members"):
    if found.get("active") is True:
        member = email(found.get("email"), "member email")
        humans.append(member)
plan = []
def fill(table, key, value):
    if dig(data, *table.split("."), key) in (None, "", [], {}):
        plan.append([table, key, value])
if dig(data, "orchestrator", "tracker") != "linear":
    plan.append(("orchestrator", "tracker", "linear"))
fill("orchestrator.linear", "team", team)
for slot, fields in apps.items():
    table = f"orchestrator.linear.{slot}" if slot in ("harness", "manager") else f"orchestrator.linear.roles.{slot}"
    for key, value in fields.items():
        fill(table, key, value)
fill("orchestrator.linear", "task_label_group", tasks)
if humans:
    fill("orchestrator.linear", "human_members", humans)
for key, title in STATES.items():
    fill("orchestrator.linear.states", key, states[title])
known = set()
team_dirs = dig(data, "delegate", "team_dirs") or []
charter_dirs = [charters, *(os.path.join(os.path.expanduser(d), "roles") for d in team_dirs)]
for role, _ in stages:
    paths = [os.path.join(d, f"{role}.md") for d in charter_dirs if os.path.isfile(os.path.join(d, f"{role}.md"))]
    if len(paths) > 1:
        stop(f"charter {role} is defined more than once; team_dirs files only add")
    if not paths:
        continue
    try:
        with open(paths[0], encoding="utf-8") as f:
            match = re.search(r"^## Tasks\n(.*?)(?=^#|\Z)", f.read(), re.M | re.S)
    except OSError:
        continue
    if match:
        known.update(re.findall(r"^- `([\w-]+)`(?: \(`[^`]*`\))?: .+$", match[1], re.M))
for task, ident in children(tasks).items():
    if task in known:
        fill("orchestrator.linear.task_labels", task, ident)
    else:
        print(f"    Tasks label {task}: not a task of the board\x27s roles; left out", file=sys.stderr)
agent = group("Agent")
if agent and children(agent) and not linear_config.get("agent_label_group") and not linear_config.get("agent_labels"):
    fill("orchestrator.linear", "agent_label_group", agent)
    for client, ident in children(agent).items():
        fill("orchestrator.linear.agent_labels", client, ident)
repos = linear_config.get("project_repos") or {}
slug = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._][A-Za-z0-9._-]{0,99}")
for found in nodes(detail, "projects"):
    ident = uid(found.get("id"), "project id")
    title = name(found.get("name"), "project name", ident)
    if ident in repos:
        continue
    while answer := ask(f"GitHub repo of Linear project {title} (<owner>/<name>, empty to skip):"):
        if slug.fullmatch(answer) and answer.split("/")[1] not in (".", ".."):
            plan.append(["orchestrator.linear.project_repos", ident, answer])
            break
delegate = data.get("delegate", {})
def output(role):
    found = dig(delegate, "roles", role, "output") or delegate.get("output") or {}
    return found if found.get("type") == "github" and found.get("repo") and found.get("branch") else None
documents = [(role, input_) for role, input_ in stages if input_ in ("research", "design")]
unpublished = [(role, input_) for role, input_ in documents if not output(role)]
if unpublished:
    known_output = next((output(role) for role, _ in documents if output(role)), None)
    if known_output:
        repo, branch = known_output["repo"], known_output["branch"]
    else:
        question = "GitHub repo for the board\x27s documents, research reports and PRDs (<owner>/<name>):"
        while not ((repo := ask(question)) and slug.fullmatch(repo) and repo.split("/")[1] not in (".", "..")):
            pass
        while not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch := ask("Its branch [main]:") or "main"):
            pass
    for role, input_ in unpublished:
        table = f"delegate.roles.{role}.output"
        for key, value in (("type", "github"), ("repo", repo), ("branch", branch),
                           ("dir", "Research/" if input_ == "research" else "Product Design/")):
            fill(table, key, value)
with open(out, "w", encoding="utf-8") as f:
    json.dump(plan, f)' "$config" "$1" "$2" "$3" "$pkg/src/ensemble/delegate/team/roles" "$state_dir/linear-apps.json"
}

local_board_setup() {
  local board_dir=$HOME/.ensemble/board wanted= directory
  directory=$board_dir/unassigned
  while [ ! -e "$directory" ]; do
    wanted="$directory${wanted:+
$wanted}"
    directory=${directory%/*}
    [ -n "$directory" ] || directory=/
  done
  if [ -n "$wanted" ]; then
    if ! consent "$board_dir" "Create directories:
$wanted"; then
      skip declined
      return 0
    fi
    while IFS= read -r directory; do
      if absent "$directory"; then ledger_add board-dir "$directory"; fi
    done <<<"$wanted"
  fi
  mkdir -p "$board_dir/unassigned"
  [ -n "$wanted" ] || skip "local board initialized"
  local_board_ready=yes
}

board_config() {
  local plan=$state_dir/board.json slots=
  if [ "$board" = yes ] && [ "$board_tracker" = linear ]; then
    board_stages=$(stage_roles) || exit 1
    slots=$(linear_slots "$board_stages") || exit 1
  fi
  linear_apps "$slots"
  step "$board_tracker board configuration"
  board_wanted || return 0
  if [ "$board_tracker" = local ]; then
    config_file orchestrator tracker local
    if [ "$(configured_tracker "$(config_says)")" = local ]; then
      local_board_setup
    else
      skip "local tracker not configured"
    fi
    return 0
  fi
  if board_complete "$board_stages"; then
    skip "has the Linear board configuration"
    return 0
  fi
  mkdir -p "$state_dir"
  board_plan "$plan" "$slots" "$board_stages"
  config_file board "$plan"
  rm -f "$plan"
}

# PATH for the board jobs: the directories of the tools install.sh found, then ~/.ensemble/bin and the system's.
launchd_path() {
  local dirs= name dir
  for name in python3 claude codex gh tmux git docker; do
    dir=$(command -v "$name") || continue
    case $dir in /*) ;; *) continue ;; esac
    dirs="$dirs:${dir%/*}"
  done
  for dir in $dirs "$HOME/.ensemble/bin" /usr/bin /bin /usr/sbin /sbin; do
    case ":$path:" in *":$dir:"*) ;; *) path=${path:+$path:}$dir ;; esac
  done
}

# Writes the board job $1's plist for this user to $2, with @HOME@ and @PATH@ filled in.
board_plist() {
  local path=
  IFS=: launchd_path
  python3 -I -c '
import sys
from xml.sax.saxutils import escape
source, out, home, path = sys.argv[1:]
with open(source, encoding="utf-8") as f:
    text = f.read()
if "@HOME@" not in text or "@PATH@" not in text:
    sys.exit(f"install.sh: {source} is not a template with @HOME@ and @PATH@")
text = text.replace("@HOME@", escape(home)).replace("@PATH@", escape(path))
with open(out, "w", encoding="utf-8") as f:
    f.write(text)' "$pkg/launchd/com.windroc.ensemble.$1.plist" "$2" "$HOME" "$path"
}

board_jobs() {
  local job target new offered= shown= uid
  step "board launchd jobs"
  board_wanted || return 0
  if [ "$board_jobs_choice" != yes ]; then
    skip "launchd jobs not chosen"
    return 0
  fi
  if [ "$(configured_tracker "$(config_says)")" != "$board_tracker" ]; then
    skip "board tracker not configured"
    return 0
  fi
  if [ "$board_tracker" = local ] && [ "$local_board_ready" != yes ]; then
    skip "local board not initialized"
    return 0
  fi
  if [ "$board_tracker" = linear ] && ! board_complete "$1"; then
    skip "no Linear board"
    return
  fi
  mkdir -p "$state_dir"
  for job in router promote; do
    target=$HOME/Library/LaunchAgents/com.windroc.ensemble.$job.plist
    new=$state_dir/com.windroc.ensemble.$job.plist
    board_plist "$job" "$new"
    if [ -f "$target" ] && cmp -s "$new" "$target"; then
      skip "$job installed"
    elif [ -e "$target" ]; then
      skip "$target exists; remove it to let install.sh install the job"
    else
      offered="$offered $job"
      shown="$shown$(printf '%s:\n%s' "$target" "$(cat "$new")")
"
    fi
  done
  [ -n "$offered" ] || return 0
  if ! consent "$HOME/Library/LaunchAgents" "$shown"; then
    skip declined
    return
  fi
  uid=$(id -u)
  record_dir "$HOME/.ensemble/logs"
  record_dir "$HOME/Library/LaunchAgents"
  mkdir -p "$HOME/.ensemble/logs" "$HOME/Library/LaunchAgents"
  for job in $offered; do
    ledger_add launchd "$job"
    target=$HOME/Library/LaunchAgents/com.windroc.ensemble.$job.plist
    cp "$state_dir/com.windroc.ensemble.$job.plist" "$target"
    launchctl bootout "gui/$uid/com.windroc.ensemble.$job" </dev/null >/dev/null 2>&1 || true
    launchctl enable "gui/$uid/com.windroc.ensemble.$job" </dev/null
    launchctl bootstrap "gui/$uid" "$target" </dev/null
  done
}

# The terminal's own device (/dev/ttys<n>), else /dev/tty: macOS's kqueue refuses /dev/tty, so
# Bun-built clients (Claude Code, OpenCode) die on it with EINVAL.
terminal() {
  local name
  name=$(ps -o tty= -p $$ </dev/null 2>/dev/null) || name=
  name=${name//[[:space:]]/}
  case $name in
    ttys[0-9]*)
      if [ -c "$root/dev/$name" ] && (: <>"$root/dev/$name") 2>/dev/null; then
        printf '%s' "$root/dev/$name"
        return
      fi
      ;;
  esac
  printf '%s' "$root/dev/tty"
}

# The banner a finished install ends with, a line at a time on a terminal.
celebrate() {
  local line pause=
  if [ -t 2 ]; then pause=0.08; fi
  while IFS= read -r line; do
    say "$line"
    if [ -n "$pause" ]; then sleep "$pause"; fi
  done <<'EOF'

 _____                          _     _
| ____|_ __  ___  ___ _ __ ___ | |__ | | ___
|  _| | '_ \/ __|/ _ \ '_ ` _ \| '_ \| |/ _ \
| |___| | | \__ \  __/ | | | | | |_) | |  __/
|_____|_| |_|___/\___|_| |_| |_|_.__/|_|\___|

EOF
  say "ensemble is installed (ref $ref, clients $clients, Docker $docker)."
  say "Welcome onboard, and congrats: your crew is ready. Open a commander with ctmux <name>."
}

main() {
  local platform answered=
  set +x
  parse_args "$@"
  early_checks
  platform=$(uname -sm)
  [ "$platform" = "Darwin arm64" ] || die "needs macOS on Apple silicon (Darwin arm64), not $platform"
  if [ ! -e "$root/dev/tty" ] || ! (: <>"$root/dev/tty") 2>/dev/null; then die "needs a terminal for its questions and logins"; fi
  exec 3<>"$(terminal)"
  trap on_exit EXIT
  [ -d "$HOME/.ensemble" ] || ledger_add dir "$HOME/.ensemble"
  if ! recorded agent-tree "$HOME/.ensemble" || [ ! -f "$state_dir/agent-tree.paths" ] \
    || [ -f "$state_dir/agent-tree.cleaned" ] || [ -f "$state_dir/agent-tree.pending" ]; then
    ledger_add agent-tree "$HOME/.ensemble"
    : >"$state_dir/agent-tree.pending"
    agent_tree_snapshot
    rm -f "$state_dir/agent-tree.pending" "$state_dir/agent-tree.cleaned"
  fi
  # Record tool state before Homebrew, logins or launcher installation can create it.
  record_dir "$HOME/.homebrew"
  record_file "$HOME/.homebrew/trust.json"
  record_dir "${XDG_CACHE_HOME:-$HOME/.cache}/gh"
  record_dir "${XDG_STATE_HOME:-$HOME/.local/state}/gh"
  record_dir "${UV_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/uv}"
  # An installed board already has Python; reject deselecting it with job flags before privileged work.
  if [ -n "$jobs_flag" ] && [ "$(state_get board)" = yes ]; then
    PATH="${HOMEBREW_PREFIX:-$root/opt/homebrew}/bin:$PATH" answers
    answered=yes
  fi
  sudo_once
  homebrew
  dependencies
  if [ -z "$answered" ]; then answers; fi
  ref_check
  broker_plan preflight
  opencode_select
  config_file
  apps
  client_logins
  claude_plugins
  codex_plugins
  if has claude || has codex; then broker_plan; fi
  if has claude || has codex; then plugin_dir; fi
  opencode_install
  opencode_plugin
  if ! has claude && ! has codex; then plugin_dir; fi
  opencode_provider
  docker_steps
  opencode_key
  ctmux_install
  board_config
  board_jobs "$board_stages"
  current=
  celebrate
}

main "$@"
