"""HostedSession on a private tmux server (tests/session/live_tmux.py), os.environ that server's env,
the fake client's Tui: Session.attach(name).close() and the by-name close Session.retire(name) leave
the same state (FR-4); a display given to start stored on its display and in its worker state, and
listed (FR-48).
"""

import json
import os
import unittest

from ensemble.session import Access, Host, Mode, Session, Spec
from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.hosts.base import worker_state
from ensemble.session.modes import tui
from tests.session import fake_client
from tests.session.case import HOSTS
from tests.session.modules import client_loader
from tests.session.tui_support import MANAGER, Live

DISPLAYS = {"w4": f"{MANAGER}/right", "w5": "elsewhere:1.2"}


class CloseTest(Live):
    def configure_hosted(self) -> None:
        self.addCleanup(setattr, HostedSession, "_setup", None)
        HostedSession.configure(
            clients=self.clients,
            hosts=Host.parse_hosts({**HOSTS, "default": "tmux"}),
            class_loader=client_loader({"ensemble.session.clients.fake": fake_client}),
        )

    def test_attach_then_close_and_close_by_name_leave_the_same_state_fr_4(self):
        self.configure_hosted()
        found = {}
        for name, close_it in (
            ("w1", lambda: self.session_cls.attach("w1").close()),
            ("w2", lambda: Session.retire("w2")),
        ):
            handle = self.start(name, {"read": True})
            close_it()
            found[name] = (
                handle.alive(),
                self.session_cls.list(name),
                os.path.lexists(tui.TuiStore.attach(name, host=self.session_cls.host()).dir),
            )
        self.assertEqual(found, {"w1": (False, [], False), "w2": (False, [], False)})
        self.assertEqual([row.name for row in self.session_cls.list()], [MANAGER])

    def test_list_and_close_by_name_through_the_configured_class(self):
        self.start("w3", {"read": True})
        self.assertEqual([row.name for row in self.session_cls.list("w.*")], ["w3"])
        self.configure_hosted()
        self.assertEqual([row.name for row in HostedSession.list("w.*")], ["w3"])
        self.assertEqual(Session.list("w.*"), HostedSession.list("w.*"))
        Session.retire("w3")
        self.assertEqual(HostedSession.list("w.*"), [])
        self.assertFalse(
            os.path.lexists(tui.TuiStore.attach("w3", host=self.session_cls.host()).dir)
        )


class DisplayTest(Live):
    def test_a_display_given_to_start_is_stored_read_back_and_listed_fr_48(self):
        configured = self.session_cls
        with open(self.clients["fake"]["scenario"], "w") as f:
            json.dump([{"read": True}], f)
        for name, display in DISPLAYS.items():
            spec = Spec(
                workdir=self.workdir,
                access=Access(cwd=self.workdir),
                name=name,
                client="fake",
                mode=Mode.TUI,
                prompt="hello",
            )
            self.addCleanup(configured.start(spec, display=display).stop)
        for name, display in DISPLAYS.items():
            with self.subTest(name=name):
                self.assertEqual(self.server.option(name, "@display"), display)
                self.assertEqual(worker_state(name).display, display)
                self.assertEqual([row.name for row in configured.list(display=display)], [name])


if __name__ == "__main__":
    unittest.main()
