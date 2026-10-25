"""HostedSession on a private tmux server (session/tests/live_tmux.py), os.environ that server's env,
the fake client's Tui: Session.attach(name).close() and TuiStore.attach(name, host=...).close() leave the
same state (FR-4); list and a by-name close refused until a class is configured (FR-2: the session reads no config
itself); a display given to start stored, read back and listed (FR-48).
"""

import json
import os
import subprocess
import unittest

from ensemble.session import Access, Host, Mode, Session, Spec
from ensemble.session.engine.hosted_session import UNCONFIGURED, HostedSession
from ensemble.session.modes import tui

from session.tests import fake_client
from session.tests.case import HOSTS, PY
from session.tests.modules import client_loader
from session.tests.tui_support import MANAGER, Live

DISPLAYS = {"w4": f"{MANAGER}/right", "w5": "elsewhere:1.2"}
UNCONFIGURED_CALLS = """import json
from ensemble.session import ConfigError, Session
from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.modes.tui import TuiStore
found = []
for call in (
    lambda: HostedSession.list("w.*"),
    lambda: Session.list("w.*"),
    lambda: TuiStore.attach("w3", host=Session.host()).close(),
):
    try:
        call()
        found.append(None)
    except ConfigError as error:
        found.append(str(error))
print(json.dumps(found))
"""


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
            ("w2", lambda: tui.TuiStore.attach("w2", host=Session.host()).close()),
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

    def test_list_and_close_by_name_need_a_configured_class(self):
        """Unconfigured in a child process: another test may have configured HostedSession here."""
        self.start("w3", {"read": True})
        res = subprocess.run(
            [*PY, "-c", UNCONFIGURED_CALLS],
            env=self.server.child_env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), [UNCONFIGURED] * 3)
        self.assertEqual([row.name for row in self.session_cls.list("w.*")], ["w3"])
        self.configure_hosted()
        self.assertEqual([row.name for row in HostedSession.list("w.*")], ["w3"])
        self.assertEqual(Session.list("w.*"), HostedSession.list("w.*"))
        tui.TuiStore.attach("w3", host=Session.host()).close()
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
        host = configured.host()
        for name, display in DISPLAYS.items():
            with self.subTest(name=name):
                self.assertEqual(self.server.option(name, "@display"), display)
                self.assertEqual(host.attach(name).display(), display)
                self.assertEqual([row.name for row in configured.list(display=display)], [name])


if __name__ == "__main__":
    unittest.main()
