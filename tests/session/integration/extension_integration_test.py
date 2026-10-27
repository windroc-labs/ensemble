"""A Client and a Host written outside the session package (tests/session/outside.py, importing only
ensemble.session.extension and ensemble.session), configured as module.path:Class in
[clients.outside] and [hosts.outside]: an in_process session yields its events, a headless one is
refused (the Host runs no managed worker for a session harness); the Host keeps, lists and ends the
processes it detaches.
"""

import ast
import inspect
import os
import sys
import unittest

from ensemble.session import Access, Exited, Launch, Mode, Reply, Spec, TurnEnd
from ensemble.session.extension import ConfigError, Host, Record, SessionError
from tests.session import harnesses, outside
from tests.session.case import Case

CLIENTS = {"outside": {"implementation": "tests.session.outside:OutsideClient"}}
HOSTS = {"default": "outside", "outside": {"implementation": "tests.session.outside:OutsideHost"}}
TMUX = {"implementation": "ensemble.session.hosts.tmux:TmuxHost"}
PUBLIC = {"ensemble.session", "ensemble.session.extension"}
WAIT = 20  # seconds


class OutsideTest(Case):
    def setUp(self):
        self.session = harnesses.Threaded.configured(clients=CLIENTS, hosts=Host.parse_hosts(HOSTS))
        self.workdir = os.path.join(self.home, "work")

    def started(self, mode: Mode):
        spec = Spec(
            workdir=self.workdir,
            access=Access(cwd=self.workdir),
            client="outside",
            mode=mode,
            prompt="hello",
        )
        return self.session.start(spec)

    def test_the_outside_module_imports_only_the_public_session_modules(self):
        tree = ast.parse(inspect.getsource(outside))
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertNotIn(None, imported)
        others = {m for m in imported if m.split(".")[0] not in sys.stdlib_module_names}
        self.assertEqual(others, {"ensemble.session.extension"})
        self.assertLessEqual(others, PUBLIC)

    def test_a_headless_session_is_refused_on_a_host_without_a_managed_worker(self):
        with self.assertRaises(ConfigError) as caught:
            self.started(Mode.HEADLESS)
        self.assertEqual(
            str(caught.exception),
            "outside headless on host outside: runs only under a session harness",
        )

    def test_an_in_process_session_yields_its_runs_events(self):
        session = self.started(Mode.IN_PROCESS)
        self.assertEqual(list(session.events()), [Reply("hello"), TurnEnd("hello"), Exited(0)])

    def test_a_mode_the_client_does_not_offer_is_refused(self):
        # a Host whose managed workers a session harness runs
        self.session = harnesses.Threaded.configured(
            clients=CLIENTS, hosts=Host.parse_hosts({"default": "tmux", "tmux": TMUX})
        )
        with self.assertRaises(ConfigError) as caught:
            self.started(Mode.TUI)
        self.assertEqual(str(caught.exception), "client 'outside' does not support tui mode")

    def test_the_configured_host_keeps_lists_and_ends_its_processes(self):
        host = self.session.host()
        self.assertIsInstance(host, outside.OutsideHost)
        self.assertIs(self.session.host("outside"), host)
        resume = ("ensemble-session", "resume", "j1")
        record = Record(
            1, "job", "j1", ("j1",), None, None, ("ensemble-session",), resume, "d", None
        )
        process = host.detach(
            "j1",
            Launch((sys.executable, "-c", "import time; time.sleep(60)"), cwd=self.home),
            display="d",
            record=record,
        )
        self.addCleanup(process.close)
        self.assertIs(host.attach("j1"), process)
        self.assertEqual((process.record(), process.display()), (record, "d"))
        self.assertEqual(host.list(), ["j1"])
        self.assertEqual(
            [[row.name for row in self.session.list(display=d)] for d in ("d", "e", "")],
            [["j1"], [], []],
        )
        self.assertTrue(process.alive())
        process.kill()
        self.assertTrue(process.wait_exit(WAIT))
        self.assertFalse(process.alive())
        with self.assertRaises(SessionError):
            host.attach("j2")


if __name__ == "__main__":
    unittest.main()
