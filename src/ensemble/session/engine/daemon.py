"""The account's Docker target (the broker's TARGET_FILE, not session config), the one docker
command line that reaches it whatever DOCKER_HOST, DOCKER_CONTEXT or DOCKER_CONFIG say, a capped
run of one whose output is untrusted, a daemon's ID, and the record of which daemon holds a
session's state.
"""

import errno
import json
import os
import pwd
import re
import select
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping
from typing import NamedTuple

from ensemble.session.engine.types import ConfigError, DockerAccess, SessionError
from ensemble.utils import DOCKER_STATE, read_regular, save

CLEARED = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
RUN_TIMEOUT = 10
OUT_MAX = 8 << 20
TARGET_FILE = ".ensemble/docker/target.json"
TARGET_MAX = 64 << 10
TARGET = re.compile(r"ssh://([A-Za-z0-9._-]{1,64}@)?[A-Za-z0-9][A-Za-z0-9.-]{0,252}(:[0-9]{1,5})?")
SSH_DIR = "/usr/local/libexec/ensemble/ssh"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
UNREACHABLE = ("Cannot connect to the Docker daemon", "error during connect")
STATE_READER_LABEL = "ensemble.state-reader"
ACCESS_FILE = "state-access.json"
ACCESS_MAX = 4096
DAEMON = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")
ENV_ITEM = re.compile(r"ensemble-broker-env/[a-z0-9][a-z0-9._-]{0,63}")
LOCAL_SECRETS_MAX = 16


class Target(NamedTuple):
    host: str
    local_secrets: tuple[str, ...]


class Overflow(OSError):
    pass


def state_volume(name: str) -> str:
    return f"ensemble-state-{name}"


def _access_path(name: str) -> str:
    return os.path.join(os.path.expanduser(DOCKER_STATE), name, ACCESS_FILE)


def save_access(name: str, access: DockerAccess) -> None:
    record = {"docker": access.docker, "host": access.host, "daemon": access.daemon}
    save(_access_path(name), json.dumps(record), durable=True)


def load_access(name: str) -> DockerAccess | None:
    """None: no record; OSError (EINVAL): malformed."""
    path = _access_path(name)
    data = read_regular(path, limit=ACCESS_MAX + 1)
    if data is None:
        return None
    try:
        found = json.loads(data) if len(data) <= ACCESS_MAX else None
    except ValueError:
        found = None
    if (
        isinstance(found, dict)
        and set(found) == {"docker", "host", "daemon"}
        and all(isinstance(v, str) for v in found.values())
        and os.path.isabs(found["docker"])
        and (_remote(found["host"]) or _local(found["host"]))
        and DAEMON.fullmatch(found["daemon"])
    ):
        return DockerAccess(**found)
    raise OSError(errno.EINVAL, "malformed Docker state access record", path)


def _local(host: str) -> bool:
    path = host.removeprefix("unix://")
    return host.startswith("unix:///") and "\0" not in path and os.path.normpath(path) == path


def account_home() -> str:
    """The system account's home, never $HOME."""
    return pwd.getpwuid(os.getuid()).pw_dir


def local_endpoint() -> str:
    """The local Docker Desktop."""
    return f"unix://{account_home()}/.docker/run/docker.sock"


def describe(host: str) -> str:
    return f"the local Docker Desktop ({host})" if host == local_endpoint() else host


def endpoint() -> str:
    return target().host


def target() -> Target:
    """TARGET_FILE, by the broker's rule; no file: local, no local secrets."""
    path = os.path.join(account_home(), TARGET_FILE)
    try:
        with open(path, "rb") as f:
            data = f.read(TARGET_MAX + 1)
    except FileNotFoundError:
        return Target(local_endpoint(), ())
    except OSError as error:
        raise ConfigError(f"Docker target {path}: {error.strerror}") from None
    try:
        found = json.loads(data) if len(data) <= TARGET_MAX else None
    except ValueError:
        found = None
    host = items = None
    if isinstance(found, dict) and set(found) == {"host", "local_secrets"}:
        host, items = found["host"], found["local_secrets"]
    if (
        not isinstance(host, str)
        or not _remote(host)
        or not isinstance(items, list)
        or len(items) > LOCAL_SECRETS_MAX
        or not all(isinstance(item, str) and ENV_ITEM.fullmatch(item) for item in items)
        or len(set(items)) != len(items)
    ):
        raise ConfigError(
            f'Docker target {path}: want {{"host": "ssh://[user@]host[:port]", "local_secrets":'
            f' [up to {LOCAL_SECRETS_MAX} distinct "ensemble-broker-env/<name>" items]}}'
        )
    return Target(host, tuple(items))


def daemons() -> tuple[str, ...]:
    host = endpoint()
    local = local_endpoint()
    return (host,) if host == local else (host, local)


def placement(items: Iterable[str]) -> str:
    """The local Docker Desktop for a worker given any local secret, else the target."""
    found = target()
    return local_endpoint() if set(items) & set(found.local_secrets) else found.host


def daemon_flag(host: str) -> tuple[str, ...]:
    """close and workspace-rm's arguments naming `host`."""
    current = daemons()
    if host == current[0]:
        return ()
    if host in current:
        return ("--daemon", "local")
    raise SessionError(
        f"Docker daemon {host} is not the local Docker Desktop or the target {current[0]}"
    )


def env_items(credentials: Mapping[str, str]) -> tuple[str, ...]:
    """Env secret items, merged as the broker does: `credentials`' env wins."""
    items: tuple[str, ...] = ()
    for key in ("environment_credentials", "credentials"):
        try:
            spec = json.loads(credentials.get(key) or "{}")
        except ValueError:
            continue
        env = spec.get("env") if isinstance(spec, dict) else None
        if isinstance(env, list):
            items = tuple(
                e["item"] for e in env if isinstance(e, dict) and isinstance(e.get("item"), str)
            )
    return items


def _remote(host: str) -> bool:
    match = TARGET.fullmatch(host)
    return match is not None and (match[2] is None or 1 <= int(match[2][1:]) <= 65535)


def ssh_agent() -> dict[str, str]:
    """The one variable a remote target's ssh adds."""
    sock = os.environ.get("SSH_AUTH_SOCK")
    return {"SSH_AUTH_SOCK": sock} if sock else {}


def unreachable(stderr: str) -> bool:
    return any(words in stderr for words in UNREACHABLE)


def daemon_id(docker: str, host: str, *, run=None) -> str:
    """SessionError naming `host` when it has no usable one."""
    argv, env = docker_call(docker, "info", "--format", "{{json .ID}}", host=host)
    try:
        done = (run or capped)(argv, env, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise SessionError(f"Docker target {host}: docker info timed out") from None
    except OSError as error:
        raise SessionError(f"Docker target {host}: {error}") from None
    if done.returncode:
        lines = done.stderr.strip().splitlines()
        last = lines[-1] if lines else f"exit status {done.returncode}"
        why = "unreachable: " if unreachable(done.stderr) else ""
        raise SessionError(f"Docker target {host}: {why}{last!r:.80}")
    try:
        found = json.loads(done.stdout)
    except ValueError:
        found = None
    if not isinstance(found, str) or not DAEMON.fullmatch(found):
        raise SessionError(f"Docker target {host}: no usable daemon ID in docker info")
    return found


def container_name(name: str, generation: str) -> str:
    return f"ensemble-{name}-{generation}"


def docker_call(docker: str, *args: str, host: str) -> tuple[list[str], dict[str, str]]:
    """argv and env for `docker <args>` against `host`."""
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
