"""gh as ~/.ensemble/bin/gh: the next gh on PATH until ~/.ensemble/host-gh exists, then the host App's.

The real gh then runs as a child; the broker puts a new App token in gh's keyring before the last is 50
minutes old, before and beside it. gh reads the keyring per request, so a long --watch keeps working.
"""

import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

STATE_DIR = os.path.expanduser("~/.ensemble/host-gh")
WRAPPER = os.path.expanduser("~/.ensemble/bin/gh")
BROKER = "/usr/local/libexec/ensemble/ensemble-broker"
# verified on the test Mac mini to reread the keyring per request
VERIFIED_GH = "2.102.0"
REFRESH_AFTER = 3000
TOKEN_LIFETIME = 3600
CHECK_EVERY = 60
BROKER_TIMEOUT = 120
MAX_REPLY = 4096
REPLY_KEYS = {"version", "account", "expires_at", "audit_warning"}
PREFIX = "gh (ensemble): "
FORWARDED = (signal.SIGHUP, signal.SIGTERM, signal.SIGUSR1, signal.SIGUSR2)
# the terminal sends these to the whole foreground group, the child included
GROUP = (signal.SIGINT, signal.SIGQUIT)
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
EXPIRY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z")


def find_real_gh(path: str, wrapper: str = WRAPPER) -> str | None:
    skip = os.path.realpath(wrapper)
    for folder in path.split(os.pathsep):
        found = os.path.join(folder, "gh")
        if (
            os.path.isabs(folder)
            and os.path.isfile(found)
            and os.access(found, os.X_OK)
            and os.path.realpath(found) != skip
        ):
            return found
    return None


def insecure(environ) -> bool:
    """Whether gh keeps a github.com token outside the keyring."""
    folder = environ.get("GH_CONFIG_DIR") or os.path.join(
        environ.get("XDG_CONFIG_HOME") or os.path.join(environ.get("HOME", ""), ".config"), "gh"
    )
    try:
        with open(os.path.join(folder, "hosts.yml"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    block = False
    for line in lines:
        if line and not line[0].isspace():
            block = line.strip() == "github.com:"
        elif block and line.strip().startswith("oauth_token:"):
            return True
    return False


def gh_version(real: str, state: str) -> str:
    info = os.stat(real)
    key = {"path": real, "size": info.st_size, "mtime_ns": info.st_mtime_ns}
    cache = os.path.join(state, "gh-version")
    with contextlib.suppress(OSError, ValueError, AttributeError):
        with open(cache, encoding="utf-8") as f:
            found = json.load(f)
        if {k: found.get(k) for k in key} == key and isinstance(found.get("version"), str):
            return found["version"]
    try:
        out = subprocess.run(
            [real, "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    version = m[1] if (m := re.match(r"gh version (\S+)", out)) else "unknown"
    with contextlib.suppress(OSError):
        with open(cache + ".new", "w", encoding="utf-8") as f:
            json.dump({**key, "version": version}, f)
        os.replace(cache + ".new", cache)
    return version


def refusal(real: str, state: str, environ) -> str | None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if environ.get(name):
            return (
                f"{name} is set: gh would use it, not the host App token in its keyring; unset it"
            )
    if insecure(environ):
        return (
            "gh keeps a token in plain text in hosts.yml: run gh auth logout --hostname github.com"
        )
    if (version := gh_version(real, state)) != VERIFIED_GH:
        return (
            f"gh {version} is not {VERIFIED_GH}, the gh a long --watch is verified with: install"
            f" {VERIFIED_GH}, or verify {version} on the test Mac mini and update VERIFIED_GH"
        )
    return None


def age(state: str) -> float:
    """Seconds since the token was issued."""
    try:
        return time.time() - os.stat(os.path.join(state, "refreshed")).st_mtime
    except OSError:
        return float("inf")


def refresh(real: str, state: str, broker: str) -> str | None:
    """The failure, else None."""
    try:
        res = subprocess.run(
            [broker, "host-gh-login", "--gh", real],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=BROKER_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"{broker}: {e}"
    if res.returncode != 0:
        lines = [line for line in res.stderr.splitlines() if line.strip()]
        return lines[-1][:300] if lines else f"{broker} exited {res.returncode}"
    try:
        reply = json.loads(res.stdout[: MAX_REPLY + 1])
        if len(res.stdout) > MAX_REPLY or not isinstance(reply, dict):
            raise ValueError(reply)
        expires = reply["expires_at"]
        if (
            not {"version", "account", "expires_at"} <= reply.keys() <= REPLY_KEYS
            or reply["version"] != 1
            or not LOGIN.fullmatch(reply["account"])
            or not EXPIRY.fullmatch(expires)
        ):
            raise ValueError(expires)
        issued = datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, KeyError):
        return f"{broker}: unusable host-gh-login reply"
    stamp = os.path.join(state, "refreshed")
    with open(stamp, "a", encoding="utf-8"):
        pass
    issued -= TOKEN_LIFETIME
    os.utime(stamp, (issued, issued))
    return None


def ensure_fresh(real: str, state: str, broker: str, after: float = REFRESH_AFTER) -> str | None:
    """Refreshes when due, one process at a time; the failure, else None."""
    if age(state) < after:
        return None
    fd = os.open(os.path.join(state, "refresh.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return None if age(state) < after else refresh(real, state, broker)
    finally:
        os.close(fd)


def warn(failed: str, state: str) -> None:
    head = (
        "token expired; refresh keeps failing"
        if age(state) >= TOKEN_LIFETIME
        else "token refresh failed"
    )
    print(f"{PREFIX}{head}: {failed}", file=sys.stderr, flush=True)


def keep_fresh(
    real, state, broker, stop: threading.Event, *, every=CHECK_EVERY, after=REFRESH_AFTER
):
    while not stop.wait(every):
        if failed := ensure_fresh(real, state, broker, after):
            warn(failed, state)


def run_child(real: str, argv: list[str], state: str, broker: str, *, every: float, after: float):
    child = subprocess.Popen(["gh", *argv], executable=real)
    # after Popen, and no SIG_IGN, which exec would keep: the child gets default dispositions
    for signum in GROUP:
        signal.signal(signum, lambda *_: None)
    for signum in FORWARDED:
        signal.signal(signum, lambda n, _: child.send_signal(n))
    stop = threading.Event()
    checker = threading.Thread(
        target=keep_fresh,
        args=(real, state, broker, stop),
        kwargs={"every": every, "after": after},
        daemon=True,
    )
    checker.start()
    code = child.wait()
    stop.set()
    if code < 0:
        signal.signal(-code, signal.SIG_DFL)
        os.kill(os.getpid(), -code)
        sys.exit(128 - code)
    sys.exit(code)


def main(
    argv: list[str],
    *,
    state: str = STATE_DIR,
    wrapper: str = WRAPPER,
    broker: str = BROKER,
    every: float = CHECK_EVERY,
    after: float = REFRESH_AFTER,
):
    real = find_real_gh(os.environ.get("PATH", ""), wrapper)
    if real is None:
        sys.exit(PREFIX + "no gh on PATH besides the ensemble wrapper")
    if not os.path.isdir(state):
        os.execv(real, ["gh", *argv])
    if refused := refusal(real, state, os.environ):
        sys.exit(PREFIX + refused)
    if failed := ensure_fresh(real, state, broker, after):
        if age(state) >= TOKEN_LIFETIME:
            sys.exit(f"{PREFIX}token refresh failed: {failed}")
        warn(failed, state)
    run_child(real, argv, state, broker, every=every, after=after)


if __name__ == "__main__":
    main(sys.argv[1:])
