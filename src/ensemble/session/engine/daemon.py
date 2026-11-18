"""A session's Docker daemon record, the one docker command line that reaches a daemon the
broker's target command listed whatever DOCKER_HOST, DOCKER_CONTEXT or DOCKER_CONFIG say, and a
capped run of one whose output is untrusted.
"""

import errno
import json
import os
import re
import select
import subprocess
import tempfile
import time

from ensemble.session.engine.types import Daemon, DaemonName, DaemonRecord, SessionError, Target
from ensemble.utils import DOCKER_STATE, read_regular, save

CLEARED = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
RUN_TIMEOUT = 10
OUT_MAX = 8 << 20
# As the protocol's state-volume.json.
CLIENT_HOMES = "/home/agent/.ensemble-state"
STATE_VOLUME_PREFIX = "ensemble-state-"
STATE_LABEL = "ensemble.state"
STATE_OWNER_LABEL = "ensemble.state-owner"
STATE_READER_LABEL = "ensemble.state-reader"
RECORD_FILE = "daemon.json"
RECORD_MAX = 4096
SESSION_RECORD = "host.json"  # the Docker Host's record of a session's last start
SESSION_RECORD_MAX = 1 << 20
GENERATION = re.compile(r"[0-9a-f]{32}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
DAEMON = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")
TEXT = re.compile(r"[^\x00-\x1f\x7f]+")


class Overflow(OSError):
    pass


def state_volume(name: str) -> str:
    return f"{STATE_VOLUME_PREFIX}{name}"


def client_home(client: str) -> str:
    return f"{CLIENT_HOMES}/{client}"


def result_file(generation: str) -> str:
    return f"{generation}.result.json"


def recorded_image(name: str) -> str:
    """The image ID session `name`'s last start ran; OSError (EIO): none recorded."""
    folder = os.path.join(os.path.expanduser(DOCKER_STATE), name)
    generation = _recorded(os.path.join(folder, SESSION_RECORD), "generation")
    if generation is not None and GENERATION.fullmatch(generation):
        digest = _recorded(os.path.join(folder, result_file(generation)), "image_digest")
        if digest is not None and DIGEST.fullmatch(digest):
            return digest
    raise OSError(errno.EIO, "no recorded worker image", name)


def _recorded(path: str, key: str) -> str | None:
    data = read_regular(path, limit=SESSION_RECORD_MAX)
    try:
        found = json.loads(data) if data is not None else None
    except (ValueError, RecursionError):
        return None
    value = found.get(key) if isinstance(found, dict) else None
    return value if isinstance(value, str) else None


def _record_path(name: str) -> str:
    return os.path.join(os.path.expanduser(DOCKER_STATE), name, RECORD_FILE)


def save_record(name: str, record: DaemonRecord) -> None:
    daemon = record.daemon
    fields = {"name": daemon.name, "id": daemon.id, "endpoint": daemon.endpoint}
    save(
        _record_path(name),
        json.dumps({**fields, "path": daemon.path, "docker": record.docker}),
        durable=True,
    )


def load_record(name: str) -> DaemonRecord | None:
    """None: no record; OSError (EINVAL): malformed."""
    path = _record_path(name)
    data = read_regular(path, limit=RECORD_MAX + 1)
    if data is None:
        return None
    try:
        found = json.loads(data) if len(data) <= RECORD_MAX else None
    except ValueError:
        found = None
    if (
        isinstance(found, dict)
        and set(found) == {"name", "id", "endpoint", "path", "docker"}
        and all(isinstance(v, str) for v in found.values())
        and found["name"] in tuple(DaemonName)
        and DAEMON.fullmatch(found["id"])
        and TEXT.fullmatch(found["endpoint"])
        and TEXT.fullmatch(found["path"])
        and os.path.isabs(found["docker"])
    ):
        daemon = Daemon(
            found["name"],
            found["endpoint"],
            found["name"] == DaemonName.REMOTE,
            found["path"],
            found["id"],
        )
        return DaemonRecord(daemon, found["docker"])
    raise OSError(errno.EINVAL, "malformed Docker daemon record", path)


def daemon_named(target: Target, name: str) -> Daemon | None:
    return next((d for d in target.daemons if d.name == name), None)


def daemon_with_id(target: Target, daemon_id: str) -> Daemon | None:
    """The reached daemon whose ID is `daemon_id`."""
    return next((d for d in target.daemons if d.id is not None and d.id == daemon_id), None)


def describe(daemon: Daemon) -> str:
    return daemon.endpoint if daemon.remote else f"the local Docker Desktop ({daemon.endpoint})"


def ssh_agent() -> dict[str, str]:
    """The one variable a remote target's ssh adds."""
    sock = os.environ.get("SSH_AUTH_SOCK")
    return {"SSH_AUTH_SOCK": sock} if sock else {}


def container_name(name: str, generation: str) -> str:
    return f"ensemble-{name}-{generation}"


def docker_call(docker: str, *args: str, daemon: Daemon) -> tuple[list[str], dict[str, str]]:
    """argv and env for `docker <args>` against `daemon`, its PATH first."""
    env = {k: v for k, v in os.environ.items() if k not in CLEARED}
    env["PATH"] = os.pathsep.join((_path(daemon), env.get("PATH", os.defpath)))
    return [docker, "--host", daemon.endpoint, *args], env


def attach_env(daemon: Daemon) -> tuple[str, ...]:
    """`env` assignments for a remote daemon's docker run outside this environment."""
    if not daemon.remote:
        return ()
    agent = (f"{k}={v}" for k, v in ssh_agent().items())
    return (f"PATH={_path(daemon)}", *agent)


def _path(daemon: Daemon) -> str:
    """`daemon`'s PATH, once a remote one's first entry holds an executable ssh: docker never
    falls back to the user's ssh.
    """
    if daemon.remote:
        wrapper = os.path.join(daemon.path.split(os.pathsep)[0], "ssh")
        if not (os.path.isfile(wrapper) and os.access(wrapper, os.X_OK)):
            raise SessionError(
                f"Docker target {daemon.endpoint}: ssh wrapper unavailable: {wrapper};"
                " reinstall the broker"
            )
    return daemon.path


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
