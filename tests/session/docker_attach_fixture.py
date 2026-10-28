"""A fake container, docker CLI and harness for guarded Docker worker tests, without Docker.

`container NAME`: this repo's guard `--serve` as its main process (run dirs under $ENSEMBLE_FAKE_RUN,
`with-secrets --client C` dropped), its stdin held open here as Docker's `StdinOnce=false` does; one
attach at a time on $ENSEMBLE_FAKE_RUN/NAME.sock; when the guard ends, the rest is killed.
`docker [--host EP] attach --sig-proxy=false NAME` relays over that socket. `harness DIR CLIENT...`
holds a DockerWorker over an attach until killed, writing DIR/harness.json.
"""

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

GUARD = str(
    Path(__file__).resolve().parents[2] / "src" / "ensemble" / "session" / "harness" / "guard.py"
)
GEN = "0123456789abcdef0123456789abcdef"
NAME = "c1"
WAIT = 10.0


def _guard_module():
    spec = importlib.util.spec_from_file_location("guard", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _socket_path(name: str) -> str:
    return os.path.join(os.environ["ENSEMBLE_FAKE_RUN"], f"{name}.sock")


def _publish(path, text: str) -> None:
    """path holding text whole once it exists."""
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(temporary, path)


def info_path(name: str) -> str:
    return os.path.join(os.environ["ENSEMBLE_FAKE_RUN"], f"{name}.json")


def serve() -> int:
    guard = _guard_module()
    guard.RUN = os.environ["ENSEMBLE_FAKE_RUN"]
    launch = guard.launch

    def unwrapped(data):
        found = launch(data)
        if found["argv"][:1] == ["with-secrets"]:
            found["argv"] = found["argv"][3:]
        return found

    guard.launch = unwrapped
    return guard.main(["--serve"])


def container(name: str) -> int:
    guard = _guard_module()
    guard.subreaper()
    os.makedirs(os.environ["ENSEMBLE_FAKE_RUN"], exist_ok=True)
    main = subprocess.Popen(
        [sys.executable, "-I", __file__, "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    assert main.stdin is not None and main.stdout is not None
    server = socket.socket(socket.AF_UNIX)
    server.bind(_socket_path(name))
    server.listen(1)
    lock, attached = threading.Lock(), []

    def accept() -> None:
        while True:
            try:
                peer, _ = server.accept()
            except OSError:
                return
            with lock:
                attached[:] = [peer]
            try:
                while data := peer.recv(65536):
                    main.stdin.write(data)
                    main.stdin.flush()
            except OSError:
                pass
            with lock:
                if attached == [peer]:
                    attached.clear()

    threading.Thread(target=accept, daemon=True).start()
    _publish(info_path(name), json.dumps({"container": os.getpid(), "guard": main.pid}))
    while data := os.read(main.stdout.fileno(), 65536):
        with lock:
            for peer in attached:
                try:
                    peer.sendall(data)
                except OSError:
                    attached.clear()
    code = main.wait()
    for pid in guard.descendants(guard.processes(), {}, os.getpid()):
        os.kill(pid, signal.SIGKILL)
    with lock:
        for peer in attached:
            peer.close()
    server.close()
    os.unlink(_socket_path(name))
    while True:
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            return code


def docker(args: list[str]) -> int:
    if args[:1] == ["--host"]:
        args = args[2:]
    if args[:2] != ["attach", "--sig-proxy=false"]:
        return 0
    peer = socket.socket(socket.AF_UNIX)
    peer.connect(_socket_path(args[2]))

    def relay() -> None:
        try:
            while data := os.read(0, 65536):
                peer.sendall(data)
        except OSError:
            pass

    threading.Thread(target=relay, daemon=True).start()
    while data := peer.recv(65536):
        os.write(1, data)
    return 0


def start_container(name: str = NAME) -> tuple[subprocess.Popen, dict]:
    proc = subprocess.Popen(
        [sys.executable, "-I", __file__, "container", name], start_new_session=True
    )
    deadline = time.monotonic() + WAIT
    while not (os.path.exists(_socket_path(name)) and os.path.exists(info_path(name))):
        if proc.poll() is not None or time.monotonic() > deadline:
            raise OSError(f"fake container {name} did not start")
        time.sleep(0.02)
    return proc, json.loads(Path(info_path(name)).read_text())


def attach(name: str = NAME) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-I", __file__, "docker", "attach", "--sig-proxy=false", name],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        bufsize=0,
    )
    assert proc.stdin is not None
    os.set_blocking(proc.stdin.fileno(), False)
    return proc


class _Broker:
    def maintain(self, container, manifest, delivered, *, root):
        return SimpleNamespace(next_check=None, deadline=None, results=[], notices=[])

    def close(self, container):
        return None


def worker(directory: str, proc: subprocess.Popen, client: list[str], *, remove=lambda: None):
    from ensemble.session.hosts.docker.maintenance import Maintenance
    from ensemble.session.hosts.docker.worker import DockerWorker

    handle = DockerWorker(
        "w1",
        GEN,
        proc,
        state=directory,
        events=None,
        hooks=None,
        remove=remove,
        owner=os.open(os.path.join(directory, "guard.lock"), os.O_RDWR | os.O_CREAT, 0o600),
        maintenance=lambda close, report: Maintenance(
            "w1", directory, GEN, _Broker(), report=report, root="/", close=close
        ),
        report=lambda *_: None,
        terminal=False,
    )
    handle.launch(
        {
            "argv": ["with-secrets", "--client", "c", *client],
            "env": {},
            "terminal": False,
            "generation": GEN,
            "size": "80x24",
        }
    )
    handle.await_start()
    return handle


def harness(directory: str, client: list[str]) -> None:
    _, found = start_container()
    proc = attach()
    worker(directory, proc, client)
    _publish(Path(directory, "harness.json"), json.dumps({**found, "docker": proc.pid}))
    while True:
        time.sleep(1)


def main() -> int:
    kind, *args = sys.argv[1:]
    if kind == "serve":
        return serve()
    if kind == "container":
        return container(args[0])
    if kind == "docker":
        return docker(args)
    harness(args[0], args[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
