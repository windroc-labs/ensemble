"""Native concurrent roster writers preserve both entries behind the lock."""

import fcntl
import os
import select
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from tests.manager.case import child_env
from tests.manager.roster_case import RosterCase
from tests.utils.sandbox import reap


class RosterTest(RosterCase):
    def test_concurrent_distinct_process_writes_preserve_both_entries(self):
        self.assertTrue(Path(self.roster.events).exists())
        records = [
            replace(self.record, name=name, sessions=(name,), sid=str(uuid4()))
            for name in ("first", "second")
        ]
        script = """import sys
from ensemble.manager import Roster, Spec
from ensemble.utils import Record
roster = Roster(sys.argv[1])
record = Record.parse(sys.argv[2])
print('ready', flush=True)
sys.stdin.readline()
print('writing', flush=True)
roster.begin(Spec(record.name, ("start",)), sid=record.sid)
roster.commit(record.name, record)
print('done', flush=True)
"""
        lock = os.open(Path(self.directory, "roster.lock"), os.O_RDWR)
        children = []
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for record in records:
                child = subprocess.Popen(
                    (sys.executable, "-P", "-S", "-c", script, self.directory, record.to_line()),
                    env=child_env(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.addCleanup(reap, child, timeout=5)
                children.append(child)
            for child in children:
                readable, _, _ = select.select([child.stdout], [], [], 10)
                self.assertTrue(readable, "writer did not reach barrier")
                self.assertEqual(child.stdout.readline(), "ready\n")
            for child in children:
                child.stdin.write("go\n")
                child.stdin.flush()
            for child in children:
                readable, _, _ = select.select([child.stdout], [], [], 10)
                self.assertTrue(readable, "writer did not begin its mutation")
                self.assertEqual(child.stdout.readline(), "writing\n")
                self.assertIsNone(child.poll())
            fcntl.flock(lock, fcntl.LOCK_UN)
            for child in children:
                stdout, stderr = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, stderr)
                self.assertEqual(stdout, "done\n")
        finally:
            os.close(lock)
            for child in children:
                reap(child, timeout=5)
                child.communicate(timeout=5)
        entries = self.roster.entries()
        self.assertEqual(set(entries), {"first", "second"})
        self.assertEqual({entry.record for entry in entries.values()}, set(records))
