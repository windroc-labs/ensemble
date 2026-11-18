"""The account's Docker Desktop daemon: its fixed endpoint, the one docker command line that reaches
it whatever DOCKER_HOST, DOCKER_CONTEXT or DOCKER_CONFIG say, and a capped run of one whose output
is untrusted.
"""

import os
import pwd
import select
import subprocess
import tempfile
import time

CLEARED = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
RUN_TIMEOUT = 10
OUT_MAX = 8 << 20


class Overflow(OSError):
    pass


def account_home() -> str:
    """The system account's home, never $HOME."""
    return pwd.getpwuid(os.getuid()).pw_dir


def endpoint() -> str:
    return f"unix://{account_home()}/.docker/run/docker.sock"


def container_name(name: str, generation: str) -> str:
    return f"ensemble-{name}-{generation}"


def docker_call(docker: str, *args: str) -> tuple[list[str], dict[str, str]]:
    """argv and env for `docker <args>` against endpoint()."""
    env = {k: v for k, v in os.environ.items() if k not in CLEARED}
    return [docker, "--host", endpoint(), *args], env


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
