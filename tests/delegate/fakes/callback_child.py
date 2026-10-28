"""CallbackChild, a subprocess.Popen for a run's callback: hands its event line to `deliver`, exits 0."""

from collections.abc import Callable


class CallbackChild:
    def __init__(self, deliver: Callable[[str], object]):
        self.deliver, self.pid, self.returncode = deliver, 4321, 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def communicate(self, line: str, timeout: float | None = None) -> tuple[str, str]:
        self.deliver(line)
        return "", ""
