"""Tui session test support."""

import json
import os
import queue
import threading
import time
from unittest import mock

from ensemble.session import Session
from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.types import (
    Access,
    Exited,
    IdleTimeout,
    Mode,
    Spec,
)
from tests.session import fake_client, live_tmux
from tests.session.case import SID as SID
from tests.session.case import Case, configured
from tests.session.modules import client_loader
from tests.session.threads import capture_errors
from tests.utils.sandbox import scratch

MANAGER = "mgr"

IDLE = 0.5  # s: idle_timeout

WAIT = 20  # s: any wait's bound

DIALOG = "Pick one: 1. this 2. that"

TMUX = "/opt/tmux/bin/tmux"


class Stream:
    """A session's events drained by a daemon; its scoped exception hook carries failures."""

    def __init__(self, case, handle: Session, poke=None):
        self.queue, self.poke = queue.Queue(), poke
        self.thread = threading.Thread(target=self._drain, args=(handle,), daemon=True)
        case.enterContext(capture_errors(self.thread, self.queue.put))
        case.addCleanup(self.close, handle)
        self.thread.start()

    def _drain(self, handle):
        for event in handle.events():
            self.queue.put(event)

    def close(self, handle):
        handle.stop()
        self.thread.join(WAIT)
        if self.thread.is_alive():
            raise AssertionError(f"events() did not finish within {WAIT}s")
        for item in list(self.queue.queue):
            if isinstance(item, BaseException):
                raise item

    def next(self, poke=None, *, idle=True):
        """Next event within WAIT s; idle=False skips IdleTimeout; poke runs meanwhile."""
        poke = poke or self.poke
        deadline = time.monotonic() + WAIT
        while True:
            try:
                item = self.queue.get(timeout=live_tmux.POLL)
            except queue.Empty:
                if time.monotonic() > deadline:
                    raise AssertionError(f"no event within {WAIT} s") from None
                if poke:
                    poke()
                continue
            if isinstance(item, BaseException):
                raise item
            if idle or not isinstance(item, IdleTimeout):
                return item

    def until(self, kind, *, idle=True) -> list:
        found = [self.next(idle=idle)]
        while not isinstance(found[-1], kind):
            found.append(self.next(idle=idle))
        return found

    def until_exited(self) -> list:
        """Events up to Exited, IdleTimeouts dropped."""
        return self.until(Exited, idle=False)

    def quiet(self, seconds: float = 1.0) -> None:
        time.sleep(seconds)
        if not self.queue.empty():
            raise AssertionError(f"an event within {seconds} s: {self.queue.get()!r}")


class Live(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        patch = mock.patch.dict(os.environ, self.server.env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.dir = scratch(self)
        self.workdir = os.path.join(self.dir, "work")
        self.events = os.path.join(self.dir, "events")
        self.clients = {
            "fake": {
                "implementation": "ensemble.session.clients.fake:FakeClient",
                "tui": True,
                "scenario": os.path.join(self.dir, "scenario.json"),
                "log": os.path.join(self.dir, "client.log"),
            }
        }
        self.session_cls = self.fake_session()

    def fake_session(self, **dependencies) -> type[HostedSession]:
        """self.clients' session class, loading the fake client."""
        return configured(
            self.clients,
            class_loader=client_loader({"ensemble.session.clients.fake": fake_client}),
            **dependencies,
        )

    def start(self, name: str, *steps, sid=None, events=None, **kwargs) -> Session:
        """Tui session `name` of the fake client running `steps`; events: self.events."""
        with open(self.clients["fake"]["scenario"], "w") as f:
            json.dump(list(steps), f)
        handle = self.session_cls.start(
            Spec(
                workdir=self.workdir,
                access=Access(cwd=self.workdir),
                name=name,
                sid=sid,
                client="fake",
                mode=Mode.TUI,
                prompt="hello",
                events=self.events if events is None else events,
            ),
            **kwargs,
        )
        self.addCleanup(handle.stop)
        return handle

    def lines(self, *want: str, events: str | None = None) -> None:
        """Waits until the events file's lines, times dropped, are `want`; pokes meanwhile."""

        def check():
            self.poke()
            self.assertEqual(
                [line.split(" ", 1)[1] for line in live_tmux.events(events or self.events)],
                list(want),
            )
            return True

        live_tmux.wait(check, what=f"events lines {want}")

    def poke(self):
        self.server.tmux("run-shell", "-b", "true")
