"""ensemble-session ls through bin/ensemble-session on a private tmux server (Q13): plain ls is its
header and Session.list's rows; --match fullmatches, a bad RE exits 2; --display D lists the
sessions opened from D's opener whatever their placement, --here the caller's, nothing outside
tmux; --records prints the stored Records alone; --descendants adds the workers opened from inside
a listed one; a second, non-default Host's sessions and Records are listed too (the module entry).
"""

import os
import sys
import uuid
from unittest import mock

from ensemble.session import Host
from ensemble.session.clients.registry import settings
from ensemble.session.engine.config import read_config
from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.types import Launch
from ensemble.utils import Record
from tests.session.cli_support import MANAGER, CliCase
from tests.session.fakes.host import FakeHost

HEADER = "name\tstate\tclient\tsid\tdisplay"
PROG = "ensemble-session"
OTHER = "other"
SLEEP = "import time; time.sleep(120)"


def lines(*records: Record) -> str:
    return "".join(record.to_line() + "\n" for record in records)


def table(*rows: str) -> str:
    return "".join(line + "\n" for line in (HEADER, *rows))


def row(record: Record, state: str = "done") -> str:
    return "\t".join((record.name, state, "claude", record.sid, record.display))


class ListingCase(CliCase):
    def ls(self, *args: str, **kw) -> str:
        return self.ok(self.session("ls", *args, **kw)).stdout

    def session_list(self, match: str | None = None) -> str:
        """HostedSession.list(match) as plain ls prints it, configured in this process from the
        same config.
        """
        config = read_config()
        hosts = Host.parse_hosts(config.get("hosts"))
        own = HostedSession.configured(**settings(config, hosts=hosts))
        with mock.patch.dict(os.environ, self.server.env()):
            rows = own.list(match)
        values = ((r.name, r.state, r.client, r.sid, r.display) for r in rows)
        return table(*("\t".join("-" if v is None else str(v) for v in each) for each in values))

    def done(self, name: str, *args: str, **kw) -> Record:
        record = self.record(self.start(name, *args, **kw))
        self.wait_event(name, "done")
        return record


class ListingTest(ListingCase):
    def test_plain_ls_is_the_header_and_session_lists_rows_match_fullmatching(self):
        w1, w12 = self.done("w1"), self.done("w12")
        manager = f"{MANAGER}\tworking\t-\t-\t-"
        self.assertEqual(self.ls(), table(manager, row(w1), row(w12)))
        self.assertEqual(self.ls(), self.session_list())
        self.assertEqual(self.ls("--match", "w1"), table(row(w1)))
        self.assertEqual(self.ls("--match", "w1"), self.session_list("w1"))
        self.assertEqual(self.ls("--match", "w1.*"), table(row(w1), row(w12)))
        self.assertEqual(self.ls("--match", "w"), table())
        self.assertEqual(self.ls("--records", "--match", "w1"), lines(w1))
        for args in (("--match", "("), ("--records", "--match", "(")):
            with self.subTest(args=args):
                result = self.session("ls", *args)
                self.assertEqual((result.returncode, result.stdout), (2, ""), result.stderr)
                self.assertTrue(result.stderr.startswith(f"{PROG}: "), result.stderr)
                self.assertIn("'('", result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)

    def test_display_lists_the_sessions_opened_from_its_opener_whatever_their_placement(self):
        w1 = self.done("w1", "--display", f"{MANAGER}/below")
        w2 = self.done("w2", "--display", f"{MANAGER}/right")
        w3 = self.done("w3", "--display", OTHER)
        self.assertEqual(
            [w1.display, w2.display, w3.display], [f"{MANAGER}/below", f"{MANAGER}/right", OTHER]
        )
        mine = table(row(w1), row(w2))
        self.assertEqual(self.ls("--display", MANAGER), mine)
        self.assertEqual(self.ls("--here"), mine)
        self.assertEqual(self.ls("--display", OTHER), table(row(w3)))
        self.assertEqual(self.ls("--display", ""), table())
        self.assertEqual(self.ls("--here", "--records"), lines(w1, w2))
        self.assertEqual(self.ls("--display", MANAGER, "--records"), lines(w1, w2))
        self.assertEqual(self.ls("--records"), lines(w1, w2, w3))
        self.assertEqual(self.ls("--display", OTHER, "--records"), lines(w3))
        self.assertEqual(self.ls("--display", "", "--records"), "")

    def test_here_outside_tmux_lists_nothing(self):
        self.done("w1")
        self.assertEqual(self.ls("--here", "--records", inside=None), "")
        self.assertEqual(self.ls("--here", "--records", "--descendants", inside=None), "")
        self.assertEqual(self.ls("--here", inside=None), table())

    def test_here_descendants_adds_the_workers_opened_from_inside_a_listed_one(self):
        w1 = self.done("w1")
        w2 = self.done("w2", inside="w1")
        self.assertEqual((w1.display, w2.display), (MANAGER, "w1"))
        self.assertEqual(self.ls("--here", "--records"), lines(w1))
        self.assertEqual(self.ls("--here", "--records", "--descendants"), lines(w1, w2))
        self.assertEqual(self.ls("--here", "--records", inside="w1"), lines(w2))

    def test_here_still_lists_the_tmux_sessions_once_another_host_is_the_default(self):
        w1 = self.done("w1")
        self.fake_host(default="fake")
        self.assertEqual(self.ls("--here", "--records", module=True), lines(w1))
        self.assertEqual(self.ls("--records", module=True), lines(w1))


class SecondHostTest(ListingCase):
    def test_a_session_on_a_second_host_is_listed_with_its_record(self):
        w1 = self.done("w1")
        host = FakeHost("fake", {"root": self.fake_host()})
        f1 = Record(
            v=1,
            kind="worker",
            name="f1",
            sessions=("f1",),
            sid=str(uuid.uuid4()),
            workdir=None,
            ctl=(PROG,),
            resume=(PROG, "resume", "f1"),
            display=MANAGER,
            detail="claude",
        )
        process = host.terminal(
            "f1",
            Launch((sys.executable, "-I", "-c", SLEEP), cwd=self.cwd),
            display=MANAGER,
            record=f1,
        )
        self.addCleanup(process.close)
        bare = host.terminal(
            "f2", Launch((sys.executable, "-I", "-c", SLEEP), cwd=self.cwd), display=MANAGER
        )
        self.addCleanup(bare.close)
        listed = self.ls(module=True)
        self.assertEqual(listed, self.session_list())
        rows = [line.split("\t") for line in listed.splitlines()]
        self.assertEqual([r[0] for r in rows], ["name", "f1", "f2", MANAGER, "w1"])
        self.assertEqual([rows[1][i] for i in (1, 3, 4)], ["working", f1.sid, MANAGER])
        self.assertEqual([rows[2][i] for i in (1, 3, 4)], ["working", "-", MANAGER])
        self.assertEqual(rows[4], row(w1).split("\t"))
        self.assertEqual(self.ls("--records", module=True), lines(f1, w1))
        self.assertEqual(self.ls("--display", MANAGER, "--records", module=True), lines(f1, w1))
        self.assertEqual(self.ls("--match", "f.*", "--records", module=True), lines(f1))
        self.assertEqual(self.ls("--here", "--records", module=True), lines(w1))
