"""A fake docker CLI and harness for guarded Docker worker tests, without Docker or credentials.

`docker [--host EP] exec [-i|-it] [--workdir D] [--env K]... CONTAINER PYTHON -I GUARD ARGS...`
runs this repo's guard with ARGS as a child in a session of its own (the container), its
/run/ensemble/guard paths under $ENSEMBLE_FAKE_RUN and `with-secrets --client C` dropped, relaying
stdin and stdout as `docker exec -i` does: its stdin's EOF closes the child's. Other commands exit
0. `harness DIR CLIENT...` holds a DockerWorker over it, writing DIR/harness.json, until killed.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

GUARD = str(
    Path(__file__).resolve().parents[2] / "src" / "ensemble" / "session" / "harness" / "guard.py"
)
RUN = "/run/ensemble/guard"


def _local(value: str) -> str:
    return value.replace(RUN, os.environ["ENSEMBLE_FAKE_RUN"])


def _copy(source, sink, *, close: bool) -> None:
    try:
        while data := os.read(source.fileno(), 65536):
            sink.write(data)
            sink.flush()
    except OSError:
        pass
    finally:
        if close:
            sink.close()


def docker(args: list[str]) -> int:
    if args[:1] == ["--host"]:
        args = args[2:]
    if args[:1] != ["exec"]:
        return 0
    index = 1
    while args[index].startswith("-"):
        index += 2 if args[index] in ("--workdir", "--env") else 1
    rest = [_local(arg) for arg in args[index + 4 :]]
    if "--" in rest:
        end = rest.index("--")
        command = rest[end + 1 :]
        if command[:1] == ["with-secrets"]:
            command = command[3:]
        rest = [*rest[: end + 1], *command]
    env = {key: _local(value) for key, value in os.environ.items()}
    child = subprocess.Popen(
        [sys.executable, "-I", GUARD, *rest],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    os.makedirs(os.environ["ENSEMBLE_FAKE_RUN"], exist_ok=True)
    Path(os.environ["ENSEMBLE_FAKE_RUN"], "docker.pid").write_text(str(os.getpid()))
    assert child.stdin is not None and child.stdout is not None
    threading.Thread(
        target=_copy, args=(sys.stdin.buffer, child.stdin), kwargs={"close": True}, daemon=True
    ).start()
    _copy(child.stdout, sys.stdout.buffer, close=False)
    return child.wait()


class _Broker:
    def maintain(self, container, manifest, delivered, *, root):
        return SimpleNamespace(next_check=None, deadline=None, results=[], notices=[])

    def close(self, container):
        return None


def harness(directory: str, client: list[str]) -> None:
    from ensemble.session.hosts.docker.maintenance import Maintenance
    from ensemble.session.hosts.docker.worker import DockerWorker

    run = os.path.join(directory, "run", "g1")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-I",
            __file__,
            "docker",
            *("exec", "-i", "c1", "python", "-I", "guard"),
            *("--hook-socket", f"{RUN}/g1/hook", "--lease", f"{RUN}/g1/lease", "--"),
            *client,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        start_new_session=True,
        bufsize=0,
    )
    assert proc.stdin is not None
    os.set_blocking(proc.stdin.fileno(), False)
    owner = os.open(os.path.join(directory, "guard.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    worker = DockerWorker(
        "w1",
        "g1",
        proc,
        state=directory,
        events=None,
        hooks=None,
        remove=lambda: None,
        owner=owner,
        maintenance=lambda close, report: Maintenance(
            "w1", directory, "g1", _Broker(), report=report, root="/", close=close
        ),
        report=lambda *_: None,
        terminal=False,
    )
    worker.await_start()
    Path(directory, "harness.json").write_text(json.dumps({"docker": proc.pid, "run": run}))
    while True:
        time.sleep(1)


def main() -> int:
    kind, *args = sys.argv[1:]
    if kind == "docker":
        return docker(args)
    harness(args[0], args[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
