"""External Client and Host implementations using only public extension imports."""

import subprocess
import sys

from ensemble.session.extension import (
    Client,
    Display,
    Headless,
    Host,
    InProcess,
    Launch,
    Process,
    Record,
    Reply,
    SessionError,
    Spec,
    Stderr,
    TurnEnd,
)

SCRIPT = "import sys; print(sys.argv[1]); print('done')"


class OutsideClient(Client):
    def headless(self):
        return OutsideHeadless()

    def in_process(self):
        return OutsideInProcess()


class OutsideHeadless(Headless):
    def launch(self, spec: Spec) -> Launch:
        return Launch((sys.executable, "-c", SCRIPT, spec.prompt))

    def events(self, lines):
        last = ""
        for line in lines:
            text = line.rstrip("\n")
            if text == "done":
                yield TurnEnd(last)
            elif text:
                last = text
                yield Reply(text)
            else:
                yield Stderr("outside: empty line")


class OutsideInProcess(InProcess):
    def run(self, spec: Spec):
        yield Reply(spec.prompt)
        yield TurnEnd(spec.prompt)


class OutsideProcess(Process):
    def __init__(self, name: str, child: subprocess.Popen, display: Display, record: Record | None):
        self.name, self.child, self.shown, self.kept = name, child, display, record

    def alive(self) -> bool:
        return self.child.poll() is None

    def close(self) -> None:
        self.kill()

    def wait_exit(self, timeout: float) -> bool:
        try:
            self.child.wait(timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    def kill(self) -> None:
        if self.alive():
            self.child.kill()
        self.child.wait()

    def record(self) -> Record | None:
        return self.kept

    def display(self) -> Display:
        return self.shown


class OutsideHost(Host):
    def __init__(self, name, settings=None):
        super().__init__(name, settings)
        self.processes: dict[str, OutsideProcess] = {}

    def detach(self, name, launch, *, display=None, events=None, record=None):
        child = subprocess.Popen(
            launch.argv,
            cwd=launch.cwd,
            env=dict(launch.env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        self.processes[name] = OutsideProcess(name, child, display, record)
        return self.processes[name]

    def terminal(self, name, launch, *, display=None, events=None, record=None, metadata=None):
        raise SessionError(f"host {self.name} has no terminals")

    def attach(self, name):
        if name not in self.processes:
            raise SessionError(f"no process {name}")
        return self.processes[name]

    def list(self):
        return sorted(self.processes)
