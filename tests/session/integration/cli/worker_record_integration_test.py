"""A worker's Record and identity through bin/ensemble-session on a private tmux server: the one
Record line start, restart and resume print, as its Host stores it, `ls --here --records` returns
it and worker.json backs it (Q05); ENSEMBLE_SID and ENSEMBLE_EVENTS as its session id and events
file, and their conflicts (Q06); the producer half of a manager's adoption, which sees only its own
workers (Q14).
"""

import json
import os
import re
import stat
import uuid
from concurrent.futures import ThreadPoolExecutor

from ensemble.utils import Record, Status
from tests.session import live_tmux
from tests.session.cli_support import MANAGER, WorkerCase, logged

GIVEN = "36900000-0000-4000-8000-000000000006"
OTHER = "36900000-0000-4000-8000-000000000007"
SECOND = "mgr2"


class RecordCase(WorkerCase):
    def expected(self, name: str, sid: str, display: str = MANAGER) -> Record:
        return Record(
            v=1,
            kind="worker",
            name=name,
            sessions=(name,),
            sid=sid,
            workdir=os.path.join(self.ensemble, "workers", sid),
            ctl=("ensemble-session",),
            resume=("ensemble-session", "resume", name),
            display=display,
            detail="claude",
        )

    def stored(self, record: Record, inside: str = MANAGER) -> None:
        """record is what the Host holds for its session and what `ls --here --records` prints."""
        self.assertEqual(Record.parse(self.server.option(record.name, "@record")), record)
        res = self.ok(self.session("ls", "--here", "--records", inside=inside))
        self.assertEqual((res.stdout, res.stderr), (record.to_line() + "\n", ""))


class RecordTest(RecordCase):
    def test_start_prints_one_record_that_the_host_stores_and_worker_json_backs(self):
        res = self.start("w1", "--env", "WORKER_KEY=v", "--", "--model", "m x")
        record = self.record(res)
        self.assertEqual(str(uuid.UUID(record.sid)), record.sid)
        self.assertEqual(record, self.expected("w1", record.sid))
        self.stored(record)
        workers = os.path.join(self.ensemble, "workers")
        path = os.path.join(workers, record.sid, "worker.json")
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        for directory in (workers, os.path.dirname(path)):
            self.assertEqual(stat.S_IMODE(os.lstat(directory).st_mode), 0o700)
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        self.assertIsInstance(state.pop("token"), str)
        self.assertEqual(
            state,
            {
                "version": 1,
                "name": "w1",
                "sid": record.sid,
                "client": "claude",
                "cwd": self.cwd,
                "host": "tmux",
                "flags": ["--model", "m x"],
                "env": {"WORKER_KEY": "v"},
                "events": self.events,
                "display": MANAGER,
                "environment": None,
            },
        )

    def test_restart_and_resume_each_print_the_record_start_printed(self):
        record = self.record(self.start("w1"))
        self.wait_event("w1", "done")
        self.assertEqual(self.record(self.session("resume", "w1")), record)
        self.assertEqual(len(self.calls("w1")), 1)
        self.assertEqual(self.record(self.session("restart", "w1", **self.scene("w1"))), record)
        self.assertEqual(self.calls("w1")[1]["argv"][:2], ["--resume", record.sid])
        self.stored(record)
        self.ok(self.session("close", "w1"))
        self.assertFalse(self.live("w1"))
        self.assertEqual(self.record(self.session("resume", "w1", **self.scene("w1"))), record)
        self.assertEqual(len(self.calls("w1")), 3)
        self.stored(record)


class IdentityTest(RecordCase):
    def test_ensemble_sid_and_events_are_the_workers_sid_and_events_file(self):
        dump = self.dumping()
        events = os.path.join(self.root, "caller-events")
        record = self.record(self.start("w1", ENSEMBLE_SID=GIVEN, ENSEMBLE_EVENTS=events))
        self.assertEqual(record, self.expected("w1", GIVEN))
        self.stored(record)
        (call,) = self.calls("w1")
        self.assertEqual(call["argv"][:2], ["--session-id", GIVEN])
        self.wait_event("w1", "done", events=events)
        self.assertEqual(live_tmux.events(self.events), [])
        (dumped,) = logged(dump)
        self.assertNotIn("ENSEMBLE_SID", dumped["env"])

    def test_of_two_starts_claiming_one_sid_at_once_one_starts_the_other_exits_2(self):
        names = ("w1", "w2")
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda name: self.start(name, ENSEMBLE_SID=GIVEN), names))
        codes = [res.returncode for res in results]
        self.assertEqual(sorted(codes), [0, 2], [res.stderr for res in results])
        won, lost = (0, 1) if codes[0] == 0 else (1, 0)
        self.assertEqual(self.record(results[won]), self.expected(names[won], GIVEN))
        self.assertEqual(
            (results[lost].stdout, results[lost].stderr),
            ("", f"ensemble-session: sid {GIVEN} is worker {names[won]}'s\n"),
        )
        self.assertFalse(self.live(names[lost]))
        self.assertEqual(self.calls(names[lost]), [])

    def test_without_ensemble_sid_each_start_gets_a_fresh_session_id(self):
        sids = [self.record(self.start(name)).sid for name in ("w1", "w2")]
        for name, sid in zip(("w1", "w2"), sids, strict=True):
            self.assertEqual(uuid.UUID(sid).version, 4)
            self.assertEqual(str(uuid.UUID(sid)), sid)
            (call,) = self.calls(name)
            self.assertEqual(call["argv"][:2], ["--session-id", sid])
        self.assertNotEqual(*sids)

    def test_start_resume_with_another_ensemble_sid_exits_2_and_starts_nothing(self):
        res = self.start("w1", "--resume", OTHER, ENSEMBLE_SID=GIVEN)
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr),
            (2, "", f"ensemble-session: ENSEMBLE_SID {GIVEN} differs from --resume {OTHER}\n"),
        )
        self.assertFalse(self.live("w1"))
        self.assertEqual(self.calls("w1"), [])
        self.assertFalse(os.path.exists(os.path.join(self.ensemble, "workers")))

    def test_start_resume_without_ensemble_sid_resumes_that_conversation(self):
        project = re.sub("[^A-Za-z0-9]", "-", self.cwd)
        transcript = os.path.join(self.home, ".claude", "projects", project, f"{OTHER}.jsonl")
        os.makedirs(os.path.dirname(transcript))
        with open(transcript, "w", encoding="utf-8") as f:
            line = {"type": "user", "sessionId": OTHER, "cwd": self.cwd, "message": {}}
            f.write(json.dumps(line) + "\n")
        record = self.record(self.start("w1", "--resume", OTHER))
        self.assertEqual(record, self.expected("w1", OTHER))
        (call,) = self.calls("w1")
        self.assertEqual(call["argv"][:2], ["--resume", OTHER])
        self.assertNotIn("--session-id", call["argv"])

    def test_resume_of_a_gone_worker_keeps_its_ensemble_sid_and_refuses_another_workers(self):
        w1 = self.record(self.start("w1"))
        w2 = self.record(self.start("w2"))
        self.ok(self.session("close", "w1"))
        res = self.session("resume", "w1", ENSEMBLE_SID=w2.sid, **self.scene("w1"))
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr),
            (2, "", f"ensemble-session: sid {w2.sid} is not worker w1's (its latest: {w1.sid})\n"),
        )
        self.assertFalse(self.live("w1"))
        self.assertEqual(len(self.calls("w1")), 1)
        record = self.record(self.session("resume", "w1", ENSEMBLE_SID=w1.sid, **self.scene("w1")))
        self.assertEqual(record, w1)
        self.assertEqual(self.calls("w1")[1]["argv"][:2], ["--resume", w1.sid])


class AdoptionTest(RecordCase):
    def test_ls_here_and_status_report_a_started_record_to_its_manager_only(self):
        self.ok(self.server.tmux("new-session", "-d", "-s", SECOND, *live_tmux.IDLE))
        self.server.attach(SECOND)
        w1 = self.record(self.start("w1"))
        w2 = self.record(self.start("w2", inside=SECOND))
        self.assertEqual(w2, self.expected("w2", w2.sid, display=SECOND))
        self.stored(w1)
        self.stored(w2, inside=SECOND)
        self.wait_event("w1", "done")
        status = [
            Status(
                name="w1",
                alive=True,
                state="done",
                outcome=None,
                attach="tmux attach -t '=w1'",
                cwd=self.cwd,
                hidden=False,
            )
        ]
        self.assertEqual(self.status("w1", inside=None), status)
        self.assertEqual(self.status(stdin=w1.to_line() + "\n", inside=None), status)
