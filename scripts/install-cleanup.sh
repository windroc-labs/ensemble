#!/bin/bash
# Internal, for test machines only (not user docs): removes what scripts/install.sh added, as recorded in
# ~/.ensemble/install/added, and keeps what was there before. Lists it and asks first; --yes skips that.
#   curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<ref>/scripts/install-cleanup.sh | bash
# Its shared helpers are scripts/install.sh's, copied (scripts/install.sh stays one file for curl | bash);
# tests/integration/install_integration_test.py checks they match. ENSEMBLE_INSTALL_ROOT: as scripts/install.sh's.
set -euo pipefail

me=install-cleanup.sh
root=${ENSEMBLE_INSTALL_ROOT:-}
state_dir=$HOME/.ensemble/install
ledger=$state_dir/added
config=$HOME/.ensemble/config.local.toml
name_pattern='^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'
yes=
password_stdin=
sudo_mine=
keepalive=
feeder=
askpass_dir=
current=
answer=

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
    clt:1 | broker:1) true ;;
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

on_exit() {
  local rc=$?
  sudo_end
  if [ "$rc" -ne 0 ] && [ -n "$current" ]; then
    say "$me: $current failed; rerun the same command to continue"
  fi
}

usage() {
  cat <<EOF
usage: install-cleanup.sh [--yes] [--password-stdin]

Internal, for test machines only: removes what install.sh recorded in ~/.ensemble/install/added,
so the machine is as before install.sh ran. Lists it and asks first; --yes skips the question
(automation only). An app or group container to remove (~/Library/Containers/<app>, ~/Library/Group
Containers/<group>) needs the terminal's Full Disk Access: without it the cleanup stops before removing
anything. Run by hand:
  curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<ref>/scripts/install-cleanup.sh | bash
With --password-stdin (sudo password on stdin), stdin cannot carry the script:
  security find-generic-password -s <item> -w | bash -c "\$(curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<ref>/scripts/install-cleanup.sh)" install-cleanup.sh --yes --password-stdin
EOF
}

usage_error() {
  say "$me: $1"
  say "$me: usage: install-cleanup.sh [--yes] [--password-stdin]"
  exit 2
}

parse_args() {
  while [ $# -gt 0 ]; do
    case $1 in
      -h | --help)
        usage
        exit 0
        ;;
      --yes) yes=1 ;;
      --password-stdin) password_stdin=1 ;;
      *) usage_error "unknown argument: $1" ;;
    esac
    shift
  done
  if [ -n "$password_stdin" ] && [ -t 0 ]; then
    usage_error "--password-stdin reads a pipe, not a terminal"
  fi
}

# Removes record $1 (a whole Ledger line) once its action has succeeded.
drop() {
  grep -vxF -- "$1" "$ledger" >"$ledger.tmp" || true
  mv "$ledger.tmp" "$ledger"
}

# The Ledger's records of kind $1, one line each, in Ledger order.
of_kind() { grep -E "^$1(\$|"$'\t'")" "$ledger" || true; }

validate() {
  local line
  local -a fields
  while IFS= read -r line; do
    IFS=$'\t' read -r -a fields <<<"$line"
    ledger_valid "${fields[@]}" || die "not a record install.sh writes; nothing removed: $line"
  done <"$ledger"
}

# Exit 0 when path $1 is confirmed missing (install.sh's path_state): `test -e` is false on any lstat failure, so its
# parent directory must be missing too, or list without it.
absent() {
  local parent=${1%/*} listing rc=0
  if [ -e "$1" ] || [ -L "$1" ]; then return 1; fi
  case $1 in /*[!/]) ;; *) return 1 ;; esac
  [ -n "$parent" ] || parent=/
  if [ ! -e "$parent" ] && [ ! -L "$parent" ]; then
    absent "$parent"
    return
  fi
  [ -d "$parent" ] || return 1
  listing=$(ls -A -- "$parent" 2>/dev/null) || return 1
  grep -qxF -- "${1##*/}" <<<"$listing" || rc=$?
  [ "$rc" = 1 ]
}

# Stops before anything is removed while a recorded app or group container may be there and the terminal lacks Full
# Disk Access: macOS lets only that remove ~/Library/Containers/<app> and ~/Library/Group Containers/<group>, and only
# that reads TCC.db.
protected_dirs() {
  local path found=
  while IFS= read -r path; do
    case $path in
      "$HOME/Library/Containers/"?* | "$HOME/Library/Group Containers/"?*)
        if ! absent "$path"; then found="$found $path"; fi
        ;;
    esac
  done <<<"$(of_kind dir | cut -f2)"
  if [ -z "$found" ] || head -c 1 "$HOME/Library/Application Support/com.apple.TCC/TCC.db" >/dev/null 2>&1; then return 0; fi
  die "macOS lets only Full Disk Access remove$found: give the terminal Full Disk Access (System Settings › Privacy & Security), reopen it, then rerun; nothing removed (take the access back once cleanup has finished)"
}

show() {
  local kind
  printf 'install-cleanup.sh will remove what install.sh added:\n' >&3
  for kind in launchd keychain broker codex-plugin codex-marketplace claude-plugin claude-marketplace \
    login opencode-auth opencode-provider opencode-config agent-tree docker-block profile profile-file config cask formula homebrew clt file board-dir dir; do
    of_kind "$kind" | tr '\t' ' ' | sed 's/^/  /' >&3
  done
}

# need <command>: fails the Step when the tool a record needs is not on PATH; the record stays.
need() { command -v "$1" >/dev/null || die "$1 not found on PATH; its records stay for a rerun"; }

launchd_jobs() {
  local line job uid
  step "launchd jobs"
  uid=$(id -u)
  while IFS=$'\t' read -r _ job; do
    [ -n "$job" ] || continue
    line=launchd$'\t'$job
    launchctl bootout "gui/$uid/com.windroc.ensemble.$job" </dev/null >/dev/null 2>&1 || true
    rm -f "$HOME/Library/LaunchAgents/com.windroc.ensemble.$job.plist"
    drop "$line"
  done <<<"$(of_kind launchd)"
}

# Deletes keychain item -s $1 [-a $2]; exit 0 when it is gone or was never there.
delete_item() {
  local rc=0
  security delete-generic-password -s "$1" ${2:+-a "$2"} </dev/null >/dev/null 2>&1 || rc=$?
  [ "$rc" = 0 ] || [ "$rc" = 44 ] || die "security delete-generic-password -s $1 failed ($rc)"
  return "$rc"
}

keychain_items() {
  local service account
  step "keychain items"
  while IFS=$'\t' read -r _ service account; do
    [ -n "$service" ] || continue
    delete_item "$service" "$account" || true
    drop "keychain"$'\t'"$service"$'\t'"$account"
  done <<<"$(of_kind keychain)"
}

broker_files() {
  local service count
  step broker
  if ! grep -qx broker "$ledger"; then
    skip "not installed by install.sh"
    return
  fi
  sudo -A rm -rf "$root/usr/local/libexec/ensemble"
  for service in ensemble-tokens ensemble-revocations; do
    count=0
    while delete_item "$service"; do
      count=$((count + 1))
      [ "$count" -lt 1000 ] || die "more than 1000 $service items; rerun"
    done
  done
  drop broker
}

# still <kind> <name>: after a failed remove, exit 0 when the client still lists it, 1 when it is gone;
# a list that cannot be read fails the Step, the record kept.
still() {
  local listing
  case $1 in
    codex-plugin) listing=$(codex plugin list --json </dev/null) ;;
    claude-plugin) listing=$(claude plugin list --json </dev/null) ;;
    claude-marketplace) listing=$(claude plugin marketplace list --json </dev/null) ;;
    codex-marketplace) listing=$(cat "${CODEX_HOME:-$HOME/.codex}/config.toml" 2>/dev/null || true) ;;
  esac || die "cannot list $1 to check $2"
  python3 -I -c '
import json, sys, tomllib
kind, name, text = sys.argv[1], sys.argv[2], sys.stdin.read()
try:
    if kind == "codex-marketplace":
        found = name in tomllib.loads(text).get("marketplaces", {})
    else:
        data = json.loads(text)
        items = data.get("installed", []) if kind == "codex-plugin" else data
        key = {"codex-plugin": "pluginId", "claude-plugin": "id", "claude-marketplace": "name"}[kind]
        found = any(item.get(key) == name for item in items)
except (ValueError, AttributeError, tomllib.TOMLDecodeError):
    sys.exit(2)
sys.exit(0 if found else 1)' "$1" "$2" <<<"$listing" || {
    [ $? = 1 ] && return 1
    die "cannot read the $1 list to check $2"
  }
}

plugins() {
  local kind name
  step plugins
  while IFS=$'\t' read -r kind name; do
    [ -n "$name" ] || continue
    case $kind in
      codex-plugin) need codex && codex plugin remove "$name" </dev/null >&2 ;;
      codex-marketplace) need codex && codex plugin marketplace remove "$name" </dev/null >&2 ;;
      claude-plugin) need claude && claude plugin uninstall "$name" </dev/null >&2 ;;
      claude-marketplace) need claude && claude plugin marketplace remove "$name" </dev/null >&2 ;;
    esac || ! still "$kind" "$name" || die "removing $kind $name failed; its record stays for a rerun"
    drop "$kind"$'\t'"$name"
  done <<<"$(of_kind codex-plugin; of_kind codex-marketplace; of_kind claude-plugin; of_kind claude-marketplace)"
}

logins() {
  local client
  step logins
  while IFS=$'\t' read -r _ client; do
    [ -n "$client" ] || continue
    need "$client"
    case $client in
      gh)
        if gh auth status </dev/null >/dev/null 2>&1; then gh auth logout --hostname github.com <&3 >&3 2>&3 || true; fi
        ! gh auth status </dev/null >/dev/null 2>&1 || die "gh is still logged in"
        ;;
      claude)
        if claude auth status </dev/null >/dev/null 2>&1; then claude auth logout <&3 >&3 2>&3 || true; fi
        ! claude auth status </dev/null >/dev/null 2>&1 || die "claude is still logged in"
        while delete_item "Claude Code-credentials"; do :; done
        ;;
      codex)
        if codex login status </dev/null >/dev/null 2>&1; then codex logout <&3 >&3 2>&3 || true; fi
        ! codex login status </dev/null >/dev/null 2>&1 || die "codex is still logged in"
        ;;
    esac
    drop "login"$'\t'"$client"
  done <<<"$(of_kind login)"
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

opencode_keys() {
  local path name
  step "OpenCode keys"
  while IFS=$'\t' read -r _ path name; do
    [ -n "$name" ] || continue
    python3 -I -c '
import json, os, sys, tempfile
path, name = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    sys.exit()
except ValueError:
    sys.exit(f"install-cleanup.sh: {path} is not JSON; nothing written")
if name not in data:
    sys.exit()
del data[name]
fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".auth.json.")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, path)
except BaseException:
    os.unlink(temp)
    raise' "$path" "$name"
    drop "opencode-auth"$'\t'"$path"$'\t'"$name"
  done <<<"$(of_kind opencode-auth)"
}

# Drops Docker Desktop's own block (its first start writes it) from profile $1, when both its markers are there.
docker_block() {
  python3 -I -c '
import re, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    text = f.read()
block = re.compile(r"(?:^\n)?^# The following lines .*by Docker Desktop.*\n(?:.*\n)*?# End of Docker Desktop section\.\n?", re.M)
new = block.sub("", text)
if new != text:
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)' "$1"
}

profiles() {
  local file line
  step "shell profiles"
  if of_kind cask | grep -qx "cask"$'\t'"docker-desktop"; then
    while IFS= read -r file; do
      if [ -f "$file" ]; then docker_block "$file"; fi
    done <<<"$(of_kind '(profile(-file)?|docker-block)' | cut -f2 | sort -u)"
  fi
  while IFS=$'\t' read -r _ file line; do
    [ -n "$file" ] || continue
    if [ -f "$file" ]; then
      grep -vxF -- "$line" "$file" >"$file.tmp" || true
      cat "$file.tmp" >"$file"
      rm -f "$file.tmp"
    fi
    drop "profile"$'\t'"$file"$'\t'"$line"
  done <<<"$(of_kind profile)"
  while IFS=$'\t' read -r _ file; do
    [ -n "$file" ] || continue
    drop "docker-block"$'\t'"$file"
  done <<<"$(of_kind docker-block)"
  while IFS=$'\t' read -r _ file; do
    [ -n "$file" ] || continue
    if [ -f "$file" ] && ! grep -q '[^[:space:]]' "$file"; then rm -f "$file"; fi
    drop "profile-file"$'\t'"$file"
  done <<<"$(of_kind profile-file)"
}

config_file() {
  local backup=$state_dir/config.local.toml.orig
  step config.local.toml
  if grep -qx "config"$'\t'"created" "$ledger"; then
    rm -f "$config"
    drop "config"$'\t'"created"
  fi
  if grep -qx "config"$'\t'"backup" "$ledger"; then
    [ -f "$backup" ] || die "no $backup to restore"
    cp "$backup" "$config.tmp"
    mv "$config.tmp" "$config"
    drop "config"$'\t'"backup"
  fi
}

# Removes registered provider values before restoring the pre-plugin config copies.
opencode_configs() {
  local path backup name parent paths file
  step "OpenCode config"
  paths=$(of_kind '(opencode-provider|opencode-config)' | cut -f2 | sort -u)
  while IFS=$'\t' read -r _ path name; do
    [ -n "$name" ] || continue
    opencode_config_edit remove-provider "$path" "$state_dir" "$name"
    backup=$state_dir/${path##*/}.orig
    if [ -s "$backup" ]; then opencode_config_edit remove-provider "$backup" "$state_dir" "$name"; fi
    drop "opencode-provider"$'\t'"$path"$'\t'"$name"
  done <<<"$(of_kind opencode-provider)"
  while IFS=$'\t' read -r _ path; do
    [ -n "$path" ] || continue
    backup=$state_dir/${path##*/}.orig
    [ -f "$backup" ] || die "no $backup to restore"
    cp "$backup" "$path"
    drop "opencode-config"$'\t'"$path"
  done <<<"$(of_kind opencode-config)"
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    for file in "${path%/*}/opencode.json" "${path%/*}/opencode.jsonc"; do
      [ -f "$file" ] || continue
      if of_kind file | grep -qxF "file"$'\t'"$file"; then continue; fi
      parent=${file%/*}
      while under_home "$parent"; do
        drop "dir"$'\t'"$parent"
        parent=${parent%/*}
      done
    done
  done <<<"$paths"
}

# Ends what still runs from Docker.app once its cask is gone (its quit misses helpers such as docker-agent): TERM, up to
# 10 seconds, then KILL; matched by the literal command-line prefix, so nothing else is signalled.
docker_processes() {
  local pattern left tries
  pattern=^$(printf '%s' "$root/Applications/Docker.app/" | sed 's/[]$*.^+?(){}|[\]/\\&/g')
  pkill -TERM -f "$pattern" </dev/null || return 0
  for tries in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f "$pattern" </dev/null >/dev/null || return 0
    sleep 1
  done
  pkill -KILL -f "$pattern" </dev/null || true
  sleep 1
  if left=$(pgrep -f "$pattern" </dev/null); then die "Docker Desktop processes still running: ${left//$'\n'/ }"; fi
}

brew_packages() {
  local kind name reason
  step "Homebrew packages"
  if [ -n "$(of_kind cask)$(of_kind formula)" ]; then need brew; fi
  while IFS=$'\t' read -r kind name; do
    [ -n "$name" ] || continue
    if [ "$kind" = cask ]; then
      if [ "$name" = docker-desktop ] && [ -x "$root/Applications/Docker.app/Contents/MacOS/uninstall" ]; then
        # Docker's own uninstall (docs.docker.com/desktop/uninstall) asks for an admin password in a macOS dialog,
        # which no held sudo answers: --yes leaves its privileged parts to the cask's uninstall below.
        if [ -n "$yes" ]; then
          skip "Docker Desktop's own uninstall: it asks in a macOS dialog; brew uninstall --cask removes Docker Desktop"
        else
          say "Docker Desktop's own uninstall asks for your admin password in a macOS dialog"
          "$root/Applications/Docker.app/Contents/MacOS/uninstall" </dev/null >&2 || say "    Docker Desktop's uninstall failed; going on"
        fi
      fi
      if brew list --cask "$name" </dev/null >/dev/null 2>&1; then brew uninstall --cask "$name" </dev/null >&2; fi
      if [ "$name" = docker-desktop ]; then docker_processes; fi
    elif brew list --formula "$name" </dev/null >/dev/null 2>&1; then
      if ! reason=$(brew uninstall "$name" </dev/null 2>&1); then
        case $reason in
          *"required by"*) say "    kept $name: $reason" ;;
          *) die "brew uninstall $name failed: $reason" ;;
        esac
      fi
    fi
    drop "$kind"$'\t'"$name"
  done <<<"$(grep -E '^(cask|formula)'$'\t' "$ledger" | sed -n '1!G;h;$p' || true)"
}

homebrew_and_clt() {
  local prefix uninstaller
  step "Homebrew and the Command Line Tools"
  prefix=$(of_kind homebrew | cut -f2)
  if [ -n "$prefix" ]; then
    uninstaller=$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh)
    NONINTERACTIVE=1 bash -c "$uninstaller" </dev/null >&2
    sudo -A rm -rf "$prefix"
    drop "homebrew"$'\t'"$prefix"
  fi
  if grep -qx clt "$ledger"; then
    sudo -A rm -rf "$root/Library/Developer/CommandLineTools"
    drop clt
  fi
}

board_dirs() {
  local path
  step "local board directories"
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    if [ -e "$path" ] || [ -L "$path" ]; then
      if ! rmdir "$path" 2>/dev/null; then say "    kept $path: not empty"; fi
    fi
    drop "board-dir"$'\t'"$path"
  done <<<"$(of_kind board-dir | cut -f2 | awk '{ print length, $0 }' | sort -rn | cut -d' ' -f2-)"
}

files_and_dirs() {
  local path domain
  step "files and directories"
  while IFS=$'\t' read -r _ path; do
    [ -n "$path" ] || continue
    # Through cfprefsd first, so it does not write back a preferences plist it still holds.
    case $path in
      "$HOME/Library/Preferences/"*.plist)
        domain=${path##*/}
        defaults delete "${domain%.plist}" </dev/null >/dev/null 2>&1 || true
        ;;
    esac
    rm -f "$path"
    drop "file"$'\t'"$path"
  done <<<"$(of_kind file)"
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    [ "$path" = "$HOME/.ensemble" ] && continue
    if ! rm -rf "$path" || [ -e "$path" ]; then die "cannot remove $path; its record stays for a rerun"; fi
    drop "dir"$'\t'"$path"
  done <<<"$(of_kind dir | cut -f2 | awk '{ print length, $0 }' | sort -rn | cut -d' ' -f2-)"
}

agent_tree() {
  local path key index=0 keep= root_key
  local -a paths
  step "ensemble runtime data"
  umask 077
  if ! grep -qxF "agent-tree"$'\t'"$HOME/.ensemble" "$ledger"; then return; fi
  root_key=$(LC_ALL=C printf '%q' "$HOME/.ensemble")
  grep -qxF -- "$root_key" "$state_dir/agent-tree.paths" || die "invalid ensemble path inventory; records retained"
  (
    umask 077
    agent_tree_find >"$state_dir/agent-tree.current"
  ) || die "cannot inventory runtime data; records retained"
  : >"$state_dir/agent-tree.index" || die "cannot write runtime inventory; records retained"
  while IFS= read -r -d '' path; do
    paths[index]=$path
    LC_ALL=C printf '%q\t%s\n' "$path" "$index" >>"$state_dir/agent-tree.index" || die "cannot write runtime inventory; records retained"
    index=$((index + 1))
  done <"$state_dir/agent-tree.current"
  LC_ALL=C sort "$state_dir/agent-tree.index" >"$state_dir/agent-tree.sorted" || die "cannot sort runtime inventory; records retained"
  LC_ALL=C join -t $'\t' -v 2 "$state_dir/agent-tree.paths" "$state_dir/agent-tree.sorted" >"$state_dir/agent-tree.added" || die "cannot compare runtime inventory; records retained"
  while IFS=$'\t' read -r key index; do
    path=${paths[$index]}
    [ -e "$path" ] || [ -L "$path" ] || continue
    rm -rf -- "$path" || die "cannot remove runtime data; its records stay for a rerun"
  done <"$state_dir/agent-tree.added"
  (
    shopt -s dotglob nullglob
    for path in "$HOME/.ensemble/"*; do
      case $path in
        "$state_dir") ;;
        "$HOME/.ensemble/board") keep=${keep:-board} ;;
        *) keep=original; break ;;
      esac
    done
    printf '%s' "$keep"
  )
}

main() {
  local platform agent_root= keep_root= path
  set +x
  parse_args "$@"
  platform=$(uname -sm)
  [ "$platform" = "Darwin arm64" ] || die "needs macOS on Apple silicon (Darwin arm64), not $platform"
  if [ ! -e "$root/dev/tty" ] || ! (: <>"$root/dev/tty") 2>/dev/null; then die "needs a terminal for its questions and logins"; fi
  exec 3<>"$(terminal)"
  if [ ! -f "$ledger" ]; then
    say "nothing recorded by install.sh; nothing to remove"
    exit 0
  fi
  validate
  [ ! -f "$state_dir/agent-tree.pending" ] || die "path inventory is incomplete; rerun install.sh before cleanup; nothing removed"
  protected_dirs
  show
  if [ -z "$yes" ] && ! ask_yn "Remove all of this?"; then die "nothing removed"; fi
  trap on_exit EXIT
  sudo_once
  if [ -x "$root/opt/homebrew/bin/brew" ]; then eval "$("$root/opt/homebrew/bin/brew" shellenv)"; fi
  if grep -qxF "dir"$'\t'"$HOME/.ensemble" "$ledger"; then agent_root=1; fi
  launchd_jobs
  board_dirs
  keychain_items
  broker_files
  plugins
  logins
  opencode_keys
  profiles
  config_file
  opencode_configs
  keep_root=$(agent_tree)
  brew_packages
  homebrew_and_clt
  files_and_dirs
  step "~/.ensemble"
  if grep -vxF -e "dir"$'\t'"$HOME/.ensemble" -e "agent-tree"$'\t'"$HOME/.ensemble" "$ledger" | grep -q .; then
    die "records no step removed are left in $ledger"
  fi
  if [ -n "$agent_root" ]; then
    if [ "$keep_root" = original ]; then
      (
        shopt -s dotglob nullglob
        for path in "$state_dir/"*; do
          case $path in "$ledger" | "$state_dir/agent-tree.paths") continue ;; esac
          rm -rf -- "$path"
        done
      )
      : >"$state_dir/agent-tree.cleaned"
      say "    kept preexisting files; path inventory and directory record retained"
    elif [ -n "$keep_root" ] || [ -e "$HOME/.ensemble/board" ] || [ -L "$HOME/.ensemble/board" ]; then
      rm -rf "$state_dir"
      mkdir -p "$state_dir"
      printf 'dir\t%s\n' "$HOME/.ensemble" >"$ledger"
      say "    kept preexisting files or board; directory record retained"
    elif ! rm -rf "$HOME/.ensemble"; then
      mkdir -p "$state_dir"
      printf 'dir\t%s\n' "$HOME/.ensemble" >"$ledger"
      die "cannot remove $HOME/.ensemble; its record stays for a rerun"
    fi
  else
    rm -rf "$state_dir"
  fi
  current=
  say "Removed what install.sh added."
}

main "$@"
