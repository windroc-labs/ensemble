"""Log boundary behavior."""

import fcntl
import json
import os
import re
from datetime import datetime, timedelta
from unittest import mock

from ensemble.orchestrator.engine import log, types
from ensemble.orchestrator.engine.flow.types import ISSUE_ID
from ensemble.orchestrator.engine.types import ConfigError, LinearError
from ensemble.utils import one_line
from tests.orchestrator.case import Case
from tests.orchestrator.unit.clock import logs_at
from tests.orchestrator.unit.linear_fixtures import (
    Logs,
)


class Helpers(Case):
    def test_issue_id_is_unanchored(self):
        self.assertEqual(re.findall(ISSUE_ID, "see ENG-12, PM-3."), ["ENG-12", "PM-3"])
        for bad in ("task-7", "7-ENG", "ENG-", "ENG-7 "):
            self.assertIsNone(re.fullmatch(ISSUE_ID, bad), bad)

    def test_application_exception_text(self):
        self.assertEqual(one_line(LinearError("api error")), "LinearError: api error")


class Log(Logs):
    def log(self, *args, tty=False, fail=None, **kw):
        return self.stderr(lambda: log.log(*args, now=self.now, **kw), tty=tty, fail=fail)[1]

    def test_captured_diagnostics_read_history_once_and_track_new_keys(self):
        self.log("router", "claim-skip", "TASK-0", reason="no configured role delegate")
        with mock.patch("builtins.open", wraps=open) as opened:
            with log.captured():
                for number in range(1, 21):
                    for _ in range(2):
                        self.log(
                            "router",
                            "claim-skip",
                            f"TASK-{number}",
                            once=True,
                            reason="no configured role delegate",
                        )
                    with log.captured():
                        self.log(
                            "router",
                            "claim-skip",
                            f"TASK-{number}",
                            once=True,
                            reason="no configured role delegate",
                        )
            reads = [
                c
                for c in opened.call_args_list
                if c.args[0] == self.path and (len(c.args) == 1 or "r" in c.args[1])
            ]
        self.assertEqual(len(reads), 1)
        self.assertEqual([d["issue"] for d in self.lines()], [f"TASK-{n}" for n in range(21)])
        self.log("router", "claim-skip", "TASK-21", reason="no configured role delegate")
        with log.captured():
            self.log(
                "router", "claim-skip", "TASK-21", once=True, reason="no configured role delegate"
            )
        self.assertEqual(len(self.lines()), 22)

    def test_captured_dedup_keeps_the_window_for_each_event(self):
        future = datetime.fromisoformat(self.TS) + timedelta(hours=25)
        with log.captured():
            self.log(
                "router", "claim-skip", "TASK-1", once=True, reason="no configured role delegate"
            )
            for _ in range(2):
                self.stderr(
                    lambda: log.log(
                        "router",
                        "claim-skip",
                        "TASK-1",
                        once=True,
                        reason="no configured role delegate",
                        now=lambda _tz=None: future,
                    )
                )
        self.assertEqual(len(self.lines()), 2)

    def test_line_shape_and_key_order(self):
        self.assertEqual(self.log("router", "claim", "TASK-1", role="pm", task=None), "")
        self.log("prune", "prune-skip", entry="TASK-1/src/o/n", reason="not a clone é")
        self.assertEqual(
            self.text(),
            f'{{"ts": "{self.TS}", "src": "router", "kind": "claim", "issue": "TASK-1", '
            '"role": "pm"}\n'
            f'{{"ts": "{self.TS}", "src": "prune", "kind": "prune-skip", '
            '"entry": "TASK-1/src/o/n", "reason": "not a clone é"}\n',
        )

    def test_token_error_logs_the_identity_and_reason(self):
        report = log.token_error("promote")
        self.assertEqual(self.stderr(lambda: report("harness", "HTTP 401"))[1], "")
        self.assertEqual(
            [(d["src"], d["kind"], d["identity"], d["reason"]) for d in self.lines()],
            [("promote", "token-error", "harness", "HTTP 401")],
        )
        dry = log.token_error("router", dry=True)
        self.assertIn('"kind": "token-error"', self.stderr(lambda: dry("harness", "x"))[1])
        self.assertEqual(len(self.lines()), 1)

    def test_the_file_is_0600_in_logs_dir_resolved_per_call(self):
        self.log("run", "end", "TASK-1", sid="s", exit=0)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        other = os.path.join(self.tmp, "other")
        with logs_at(other):
            self.log("run", "end", "TASK-2", sid="s", exit=0)
        self.assertEqual([d["issue"] for d in self.lines()], ["TASK-1"])
        with open(os.path.join(other, "orchestrator.jsonl")) as f:
            self.assertEqual(json.loads(f.read())["issue"], "TASK-2")

    def test_no_logs_dir_writes_no_file(self):
        with (
            logs_at(None),
            mock.patch.object(os, "makedirs") as makedirs,
            mock.patch.object(os, "open") as opened,
        ):
            self.assertEqual(
                self.log("run", "end", "TASK-1", once=True, tty=True),
                self.json("run", "end", "TASK-1"),
            )
            self.assertEqual(self.log("run", "end", "TASK-1"), "")
        makedirs.assert_not_called()
        opened.assert_not_called()
        self.assertEqual(os.listdir(self.tmp), [])

    def test_an_oserror_from_the_file_never_raises(self):
        os.makedirs(self.path)
        self.assertEqual(
            self.log("run", "end", "TASK-1", tty=True), self.json("run", "end", "TASK-1")
        )
        file = os.path.join(self.tmp, "file")
        open(file, "w").close()
        with logs_at(os.path.join(file, "logs")):
            self.log("run", "end", "TASK-1")

    def test_an_oserror_from_stderr_never_raises(self):
        for dry in (False, True):
            self.log(
                "router",
                "skip",
                tty=True,
                dry=dry,
                fail=OSError(5, "Input/output error"),
                reason="x",
            )
        self.assertEqual(len(self.lines()), 1)

    def test_a_stderr_copy_only_on_a_terminal_or_dry_and_printable(self):
        self.assertEqual(self.log("run", "tui-error", "TASK-1", msg="a"), "")
        self.assertEqual(
            self.log("run", "tui-error", "TASK-1", tty=True, msg="a\x9b2Jb"),
            self.json("run", "tui-error", "TASK-1", msg="a2Jb"),
        )
        self.assertEqual(self.lines()[-1]["msg"], "a\x9b2Jb")
        self.assertEqual(
            self.log("router", "pick", "TASK-2", dry=True, queue=1),
            self.json("router", "pick", "TASK-2", queue=1),
        )
        self.assertEqual(len(self.lines()), 2)

    def test_captured_keeps_each_line_with_whether_stderr_showed_it(self):
        self.log("router", "pick", "TASK-0", queue=1)
        with log.captured() as said:
            self.log("router", "pick", "TASK-1", queue=1)
            self.log("router", "pick", "TASK-2", tty=True, queue=1)
            self.log("router", "skip", "TASK-3", told=True, reason="busy")
            for _ in range(2):
                self.log("router", "blocked", "TASK-4", once=True, by=["TASK-5"])
            with log.captured() as inner:
                self.log("router", "pick", "TASK-6", queue=1)
        self.log("router", "pick", "TASK-7", queue=1)
        self.assertEqual(
            [(line["kind"], line["issue"], shown, text) for line, shown, text in said],
            [
                ("pick", "TASK-1", False, None),
                ("pick", "TASK-2", True, None),
                ("skip", "TASK-3", True, None),
                ("blocked", "TASK-4", False, None),
                ("blocked", "TASK-4", False, None),
            ],
        )
        self.assertEqual([line["issue"] for line, _, _ in inner], ["TASK-6"])
        self.assertIsNone(log.CAPTURED)
        self.assertEqual(len(self.lines()), 7)

    def test_a_captured_stop_carries_its_own_stderr_text(self):
        for tty in (False, True):
            with self.subTest(tty=tty), log.captured() as said:
                err = self.stderr(
                    lambda: log.config_error("router", ConfigError("bad key")), tty=tty
                )[1]
                [(line, shown, text)] = said
                self.assertEqual(
                    (line["kind"], shown, text), ("config-error", tty, "router.py: bad key")
                )
                self.assertEqual(err.endswith("router.py: bad key\n"), tty)

    def test_once_dedups_on_src_kind_issue_entry_and_reason(self):
        base = dict(
            src="router",
            kind="usage-skip",
            issue=None,
            mode="new",
            reason="blocked by usage",
            usage="five_hour=0.95",
        )
        cases = [
            ({}, True),
            ({"usage": "five_hour=0.97", "mode": "resume"}, False),
            ({"reason": "other"}, True),
            ({"issue": "TASK-1"}, True),
            ({"src": "promote"}, True),
            ({"kind": "blocked"}, True),
            ({"entry": "TASK-1/tmp"}, True),
            ({}, False),
            ({"issue": "TASK-1"}, False),
        ]
        for change, written in cases:
            with self.subTest(change=change):
                before = len(self.lines())
                kw = {**base, **change}
                self.log(kw.pop("src"), kw.pop("kind"), kw.pop("issue"), once=True, **kw)
                self.assertEqual(len(self.lines()) - before, int(written))
        self.log("router", "usage-skip", mode="new", reason="blocked by usage", usage="x")
        self.assertEqual(len(self.lines()), 7)

    def test_once_reads_the_last_24_hours_of_the_file(self):
        now = datetime.fromisoformat(self.TS)
        recent, old = (now - timedelta(hours=h) for h in (23, 25))
        os.makedirs(self.logs)
        lines = [
            {
                "ts": recent.isoformat(),
                "src": "router",
                "kind": "blocked",
                "issue": "TASK-1",
                "by": ["TASK-7"],
            },
            {
                "ts": old.isoformat(),
                "src": "router",
                "kind": "blocked",
                "issue": "TASK-2",
                "by": ["TASK-7"],
            },
            {
                "ts": recent.isoformat(),
                "src": "prune",
                "kind": "prune-skip",
                "entry": "TASK-3/tmp",
                "reason": "r",
            },
        ]
        junk = [
            "router.py: crash",
            "[1]",
            '"x"',
            "{",
            json.dumps({"src": "router", "kind": "blocked", "issue": "TASK-4"}),
            json.dumps({"ts": "yesterday", "src": "router", "kind": "blocked", "issue": "TASK-5"}),
            json.dumps(
                {
                    "ts": "9999-12-31T23:59:59-05:00",
                    "src": "router",
                    "kind": "blocked",
                    "issue": "TASK-6",
                }
            ),
            json.dumps(
                {
                    "ts": now.replace(tzinfo=None).isoformat(),
                    "src": "router",
                    "kind": "blocked",
                    "issue": "TASK-7",
                }
            ),
        ]
        with open(self.path, "w") as f:
            f.write("".join(json.dumps(d) + "\n" for d in lines) + "".join(j + "\n" for j in junk))
        for issue in ("TASK-1", "TASK-2", "TASK-4", "TASK-5", "TASK-6", "TASK-7"):
            self.log("router", "blocked", issue, once=True, by=["TASK-8"])
        self.log("prune", "prune-skip", once=True, entry="TASK-3/tmp", reason="r")
        self.assertEqual(
            [
                json.loads(line)["issue"]
                for line in self.text().splitlines()[len(lines) + len(junk) :]
            ],
            ["TASK-2", "TASK-4", "TASK-5", "TASK-7"],
        )

    def test_a_dry_run_ignores_once(self):
        self.log("router", "blocked", "TASK-1", once=True, by=["TASK-7"])
        for _ in range(2):
            self.assertEqual(
                self.log("router", "blocked", "TASK-1", once=True, dry=True, by=["TASK-7"]),
                self.json("router", "blocked", "TASK-1", by=["TASK-7"]),
            )
        self.assertEqual([d["issue"] for d in self.lines()], ["TASK-1"])

    def test_once_keeps_a_cache_per_log_path(self):
        other = os.path.join(self.tmp, "other")
        for logs, written in ((self.logs, 1), (other, 1), (self.logs, 0), (other, 0)):
            with logs_at(logs):
                path = os.path.join(logs, "orchestrator.jsonl")
                before = len(self.text(path).splitlines())
                self.log("router", "blocked", "TASK-1", once=True, by=["TASK-7"])
                self.assertEqual(len(self.text(path).splitlines()) - before, written, logs)


MIB10 = 10 << 20


class Rotation(Logs):
    def setUp(self):
        super().setUp()
        os.makedirs(self.logs)
        self.line = self.json("router", "claim", "TASK-1")

    def big(self, size=MIB10):
        with open(self.path, "wb") as f:
            f.write(b"first\n")
            f.truncate(size)
        return self.raw(self.path)

    def raw(self, path):
        with open(path, "rb") as f:
            return f.read()

    def log(self, **kw):
        self.stderr(lambda: log.log("router", "claim", "TASK-1", now=self.now, **kw))

    def test_rotates_at_10_mib(self):
        with open(self.path + ".1", "w") as f:
            f.write("older\n")
        original = self.big()
        self.log()
        self.assertEqual(self.raw(self.path + ".1"), original)
        self.assertEqual(self.text(), self.line)

    def test_below_10_mib_appends(self):
        original = self.big(MIB10 - 1)
        self.log()
        self.assertFalse(os.path.exists(self.path + ".1"))
        self.assertEqual(self.raw(self.path), original + self.line.encode())

    def test_a_rotation_by_another_process_is_not_repeated(self):
        original, real = self.big(), fcntl.flock

        def raced(fd, operation):
            os.replace(self.path, self.path + ".1")
            with open(self.path, "w") as f:
                f.write("other\n")
            real(fd, operation)

        with mock.patch.object(fcntl, "flock", side_effect=raced) as flock:
            self.log()
        self.assertEqual(flock.call_count, 1)
        self.assertEqual(self.raw(self.path + ".1"), original)
        self.assertEqual(self.text(), "other\n" + self.line)

    def test_a_failed_rotation_still_logs(self):
        original = self.big()
        os.mkdir(self.path + ".lock")
        self.log()
        self.assertEqual(self.raw(self.path), original + self.line.encode())
        self.assertFalse(os.path.exists(self.path + ".1"))

    def test_dry_or_no_logs_never_rotates(self):
        original = self.big()
        self.log(dry=True)
        with logs_at(None):
            self.log()
        self.assertEqual(self.raw(self.path), original)
        self.assertEqual(sorted(os.listdir(self.logs)), ["orchestrator.jsonl"])


class ApiErrors(Logs):
    def error(self, e, src="router", *, tty=False, dry=False):
        return self.stderr(lambda: log.linear_error(src, e, dry=dry, now=self.now), tty=tty)

    def test_once_a_day_per_reason_printed_only_on_a_terminal(self):
        timeout, limit = (
            types.Unavailable("teams", reason="TimeoutError: timed out"),
            types.Unavailable("commentCreate", status=400, reason="RATELIMITED"),
        )
        line = {"src": "router", "kind": "linear-error"}
        cases = [
            (types.Unavailable("teams", status=502), False, "", {"op": "teams", "status": 502}),
            (
                types.Unavailable("issues", status=503),
                True,
                "router.py: Linear unavailable: issues: HTTP 503\n",
                None,
            ),
            (
                timeout,
                True,
                self.json(**line, op="teams", reason="TimeoutError: timed out")
                + "router.py: Linear unavailable: teams: TimeoutError: timed out\n",
                {"op": "teams", "reason": "TimeoutError: timed out"},
            ),
            (limit, False, "", {"op": "commentCreate", "status": 400, "reason": "RATELIMITED"}),
            (limit, False, "", None),
            (timeout, False, "", None),
        ]
        written = []
        for e, tty, err, new in cases:
            with self.subTest(e=str(e), tty=tty):
                self.assertEqual(self.error(e, tty=tty), (1, err))
                written += [{"ts": self.TS, **line, **new}] if new else []
                self.assertEqual(self.lines(), written)
        self.assertEqual(self.error(timeout, src="promote"), (1, ""))
        self.assertEqual(
            self.lines()[-1],
            {
                "ts": self.TS,
                "src": "promote",
                "kind": "linear-error",
                "op": "teams",
                "reason": "TimeoutError: timed out",
            },
        )

    def test_dry_prints_the_line_and_writes_nothing(self):
        e = types.Unavailable("teams", status=503)
        for tty in (False, True):
            for _ in range(2):
                self.assertEqual(
                    self.error(e, tty=tty, dry=True),
                    (1, self.json("router", "linear-error", op="teams", status=503)),
                )
        self.assertFalse(os.path.exists(self.logs))


class ConfigurationFailures(Logs):
    def test_error_destination_deduplicates_without_changing_the_configured_log(self):
        destination = os.path.join(self.tmp, "rejected-config")
        error = ConfigError("invalid role", destination)
        code, said = self.stderr(lambda: log.config_error("router", error, now=self.now))
        self.assertEqual((code, said), (2, ""))
        code, said = self.stderr(lambda: log.config_error("router", error, now=self.now), tty=True)
        self.assertEqual((code, said), (2, "router.py: invalid role\n"))
        text = self.text(os.path.join(destination, "orchestrator.jsonl"))
        self.assertEqual(text, self.json("router", "config-error", reason="invalid role"))
        self.assertEqual((log.LOGS_DIR, self.text()), (self.logs, ""))

    def test_dry_failure_prints_without_creating_either_log(self):
        destination = os.path.join(self.tmp, "rejected-config")
        code, said = self.stderr(
            lambda: log.config_error(
                "router", ConfigError("invalid role", destination), dry=True, now=self.now
            )
        )
        self.assertEqual(
            (code, said), (2, self.json("router", "config-error", reason="invalid role"))
        )
        self.assertEqual((log.LOGS_DIR, os.listdir(self.tmp)), (self.logs, []))

    def test_a_failure_without_a_destination_does_not_use_a_previous_config(self):
        self.stderr(
            lambda: log.config_error("router", ConfigError("invalid work path"), now=self.now)
        )
        self.assertEqual((log.LOGS_DIR, os.listdir(self.tmp)), (self.logs, []))

    def test_plain_reasons_keep_the_configured_destination(self):
        code, said = self.stderr(lambda: log.config_error("run", "invalid task", now=self.now))
        self.assertEqual((code, said), (2, ""))
        self.assertEqual(self.text(), self.json("run", "config-error", reason="invalid task"))
