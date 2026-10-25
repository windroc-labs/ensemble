"""DockerPiped, a headless client's `docker exec -i` (DockerHost.pipe): its lines capped, as the
container's output is untrusted; credential renewal is supervised until close(),
which removes its container once.
"""

import os
import threading
from collections.abc import Callable

from ensemble.session.engine.types import SessionError
from ensemble.session.hosts.base import LocalPiped
from ensemble.session.hosts.docker.daemon import OUT_MAX

LINE_MAX = OUT_MAX


class _Capped:
    def __init__(self, name: str, pipe) -> None:
        self.name, self.pipe = name, pipe

    def __iter__(self):
        while line := self.pipe.readline(LINE_MAX + 1):
            if len(line) > LINE_MAX and not line.endswith("\n"):
                raise SessionError(
                    f"docker session {self.name}: a client output line over {LINE_MAX} characters"
                )
            yield line

    def __enter__(self):
        return self

    def __exit__(self, *_) -> None:
        self.pipe.close()


class DockerPiped(LocalPiped):
    """The pipe lock is held until close(), which also stops credential renewal."""

    def __init__(
        self,
        name: str,
        proc,
        *,
        close: Callable[[], None],
        lock: int,
        refresh: Callable[[threading.Event], None],
    ) -> None:
        super().__init__(proc)
        self.stdout, self.stderr = _Capped(name, self.stdout), _Capped(name, self.stderr)
        self._close, self._lock = close, lock
        self._once, self._closed, self._stopped = threading.Lock(), False, threading.Event()
        threading.Thread(target=refresh, args=(self._stopped,), daemon=True).start()

    def close(self) -> None:
        with self._once:
            if self._closed:
                return
            self._closed = True
            self._stopped.set()
            try:
                self._close()
            finally:
                os.close(self._lock)
