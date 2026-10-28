"""drives.end ends each run's detached drive under a root and returns once its controller is gone."""

import json
import os
import shutil
from unittest import mock

from tests.delegate import drives
from tests.delegate.case import Case
from tests.utils.sandbox import scratch


class FakeControl:
    """A drive's ProcessControl: a directory that `ends` removes on terminate (or kill)."""

    def __init__(self, path: str, *, ends: str | None = "terminate"):
        os.makedirs(path)
        self.path, self.ends, self.ended, self.calls = path, ends, False, []

    def terminate(self):
        self.calls.append("terminate")
        if self.ends == "terminate":
            shutil.rmtree(self.path)

    def kill(self):
        self.calls.append("kill")
        if self.ends == "kill":
            shutil.rmtree(self.path)


class EndTest(Case):
    def setUp(self):
        super().setUp()
        self.root = scratch(self, real=False)
        self.controls: dict[str, object] = {}
        attach = mock.patch.object(drives.ProcessControl, "attach", side_effect=self.attach)
        attach.start()
        self.addCleanup(attach.stop)

    def attach(self, path):
        found = self.controls[path]
        if isinstance(found, OSError):
            raise found
        return found

    def run_with(self, name: str, text: str) -> None:
        run = os.path.join(self.root, name, "run")
        os.makedirs(run)
        with open(os.path.join(run, "delegate.json"), "w", encoding="utf-8") as f:
            f.write(text)

    def drive(self, name: str, **kwargs) -> FakeControl:
        path = os.path.join(self.root, "controls", name)
        control = FakeControl(path, **kwargs)
        self.controls[path] = control
        self.run_with(name, json.dumps({"drive": path}))
        return control

    def test_a_root_without_runs_ends_at_once(self):
        drives.end(self.root)

    def test_a_live_drive_is_terminated_and_awaited(self):
        controls = [self.drive("a"), self.drive("b")]
        drives.end(self.root)
        self.assertEqual([c.calls for c in controls], [["terminate"], ["terminate"]])
        self.assertFalse(any(os.path.isdir(c.path) for c in controls))

    def test_unreadable_missing_and_ended_drives_are_skipped(self):
        self.run_with("garbled", "{")
        self.run_with("none", json.dumps({"drive": None}))
        refused = os.path.join(self.root, "refused")
        self.controls[refused] = PermissionError("process controller is not a directory")
        self.run_with("refused", json.dumps({"drive": refused}))
        ended = self.drive("ended")
        ended.ended = True
        drives.end(self.root)
        self.assertEqual(ended.calls, [])

    def test_a_stuck_drive_is_killed_and_fails_naming_it(self):
        stuck, fine = self.drive("stuck", ends="kill"), self.drive("fine")
        with (
            mock.patch.object(drives, "KILL_WAIT", 0.2),
            self.assertRaisesRegex(
                AssertionError, rf"drives still running after 0.1s, then killed: \['{stuck.path}'\]"
            ),
        ):
            drives.end(self.root, wait=0.1)
        self.assertEqual((stuck.calls, fine.calls), (["terminate", "kill"], ["terminate"]))

    def test_stop_ends_one_drive(self):
        control = FakeControl(os.path.join(self.root, "one"))
        drives.stop(control)
        self.assertEqual(control.calls, ["terminate"])
        stuck = FakeControl(os.path.join(self.root, "stuck"), ends=None)
        with (
            mock.patch.object(drives, "KILL_WAIT", 0.1),
            self.assertRaisesRegex(AssertionError, "drives still running after 0.1s, then killed"),
        ):
            drives.stop(stuck, wait=0.1)
        self.assertEqual(stuck.calls, ["terminate", "kill"])
