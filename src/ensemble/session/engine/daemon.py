"""The account's Docker target (the broker's TARGET_FILE, not session config), the one docker
command line that reaches it whatever DOCKER_HOST, DOCKER_CONTEXT or DOCKER_CONFIG say, and a capped
run of one whose output is untrusted.
"""

import json
import os
import pwd
import re
import select
import subprocess
import tempfile
import time

from ensemble.session.engine.types import ConfigError, SessionError

CLEARED = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
RUN_TIMEOUT = 10
OUT_MAX = 8 << 20
TARGET_FILE = ".ensemble/docker/target.json"
TARGET_MAX = 64 << 10
TARGET = re.compile(r"ssh://([A-Za-z0-9._-]{1,64}@)?[A-Za-z0-9][A-Za-z0-9.-]{0,252}(:[0-9]{1,5})?")
SSH_DIR = "/usr/local/libexec/ensemble/ssh"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
UNREACHABLE = ("Cannot connect to the Docker daemon", "error during connect")


class Overflow(OSError):
    pass


def account_home() -> str:
    """The system account's home, never $HOME."""
    return pwd.getpwuid(os.getuid()).pw_dir


def endpoint() -> str:
    """TARGET_FILE's ssh:// host, else the local Docker Desktop."""
    path = os.path.join(account_home(), TARGET_FILE)
    try:
        with open(path, "rb") as f:
            data = f.read(TARGET_MAX + 1)
    except FileNotFoundError:
        return f"unix://{account_home()}/.docker/run/docker.sock"
    except OSError as error:
        raise ConfigError(f"Docker target {path}: {error.strerror}") from None
    try:
        found = json.loads(data) if len(data) <= TARGET_MAX else None
    except ValueError:
        found = None
    host = found.get("host") if isinstance(found, dict) and set(found) == {"host"} else None
    if not isinstance(host, str) or not _remote(host):
        raise ConfigError(f'Docker target {path}: want {{"host": "ssh://[user@]host[:port]"}}')
    return host


def _remote(host: str) -> bool:
    match = TARGET.fullmatch(host)
    return match is not None and (match[2] is None or 1 <= int(match[2][1:]) <= 65535)


def ssh_agent() -> dict[str, str]:
    """The one variable a remote target's ssh adds."""
    sock = os.environ.get("SSH_AUTH_SOCK")
    return {"SSH_AUTH_SOCK": sock} if sock else {}


def unreachable(stderr: str) -> bool:
    return any(words in stderr for words in UNREACHABLE)


def container_name(name: str, generation: str) -> str:
    return f"ensemble-{name}-{generation}"


def docker_call(docker: str, *args: str) -> tuple[list[str], dict[str, str]]:
    """argv and env for `docker <args>` against endpoint()."""
    host = endpoint()
    env = {k: v for k, v in os.environ.items() if k not in CLEARED}
    if host.startswith("ssh://"):
        env["PATH"] = os.pathsep.join((ssh_dir(host), env.get("PATH", os.defpath)))
    return [docker, "--host", host, *args], env


def attach_env(host: str) -> tuple[str, ...]:
    """`env` assignments for a remote target's docker run outside this environment."""
    if not host.startswith("ssh://"):
        return ()
    agent = (f"{k}={v}" for k, v in ssh_agent().items())
    return (f"PATH={ssh_dir(host)}{os.pathsep}{SYSTEM_PATH}", *agent)


def ssh_dir(host: str) -> str:
    """SSH_DIR, once its ssh is executable: docker never falls back to the user's ssh."""
    wrapper = os.path.join(SSH_DIR, "ssh")
    if not (os.path.isfile(wrapper) and os.access(wrapper, os.X_OK)):
        raise SessionError(
            f"Docker target {host}: ssh wrapper unavailable: {wrapper}; reinstall the broker"
        )
    return SSH_DIR


def capped(argv, env, *, input=None, timeout=RUN_TIMEOUT) -> subprocess.CompletedProcess:
    """argv's text result; Overflow past OUT_MAX bytes of stdout or stderr, or TimeoutExpired."""
    with tempfile.TemporaryFile() as given:
        given.write((input or "").encode())
        given.seek(0)
        stdin = subprocess.DEVNULL if input is None else given
        deadline = time.monotonic() + timeout
        with subprocess.Popen(
            argv, env=env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as proc:
            try:
                out, err = _drain(proc, deadline, timeout)
                code = proc.wait(max(0.0, deadline - time.monotonic()))
            except BaseException:
                proc.kill()
                raise
    return subprocess.CompletedProcess(
        argv, code, out.decode(errors="replace"), err.decode(errors="replace")
    )


def _drain(proc, deadline: float, timeout: float) -> tuple[bytes, bytes]:
    found = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
    pending = list(found)
    while pending:
        ready = select.select(pending, [], [], max(0.0, deadline - time.monotonic()))[0]
        if not ready:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        for fd in ready:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                pending.remove(fd)
            found[fd] += chunk
            if len(found[fd]) > OUT_MAX:
                raise Overflow(f"output over {OUT_MAX} bytes")
    out, err = found.values()
    return bytes(out), bytes(err)
