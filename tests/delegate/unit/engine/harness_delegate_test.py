"""HarnessDelegate names its harness session in a Host's close failure; its harness environment
holds no launcher variable; a run's state lives only in its run dir, which counts attempts, and a
refused start creates nothing; a fresh workdir passes the Docker Host's preflight; preview and
instruct write no state; owner overlays shape its run; resume continues a run in the mode it
records or in the one it is given, and a run whose process died, its tui agent session left open,
at once; replace, resume and detach first kill the callback group a dead harness left; checkouts
lists its agent dir's real checkouts and clean deletes that dir, refusing a live run or a dir out of
place.
"""

import fcntl
import io
import json
import os
import shutil
import signal
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest import mock

from ensemble.delegate import ConfigError, Outcome, Spec
from ensemble.delegate.composer import Composer, RunParams
from ensemble.delegate.engine.drivers import AgentDriver
from ensemble.delegate.engine.harness_delegate import HarnessDelegate
from ensemble.delegate.engine.state import RunState
from ensemble.delegate.engine.types import Finished, Layout, Result
from ensemble.session import (
    Access,
    EnvironmentSpec,
    Exited,
    Launch,
    Mode,
    NotShowable,
    Reply,
    Session,
    SessionBusy,
    SessionError,
    Unanswered,
)
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import Spec as SessionSpec
from ensemble.session.engine.types import Liveness
from ensemble.utils import ACCOUNT_ENV
from tests.delegate.fakes.harness_session import (
    FakeViewer,
    HarnessSession,
    RecordingHost,
    ShowingHost,
    harness_launches,
)
from tests.delegate.harness import SID, HarnessCase
from tests.delegate.replies import block
from tests.delegate.unit.engine.orphans import Clock, Orphan
from tests.session.case import PLUGIN_BIN
from tests.session.unit.hosts.docker import support

ORPHAN = 4242


class CreateTest(HarnessCase):
    def test_resumed_run_keeps_the_owners_brake(self):
        command = "owner-brake researcher"
        record = self.make(
            role="researcher",
            spec=Spec("input", self.work, client="fake", sid=SID),
            overlays=({"roles": {"researcher": {"gate": command}}},),
        ).detach()
        self.stop(record)
        resumed = HarnessDelegate.attach(self.work, session_cls=HarnessSession).resume()
        self.assertEqual(self.metadata()["plan"]["run"]["gate"], command)
        self.assertIn(command, self.metadata()["plan"]["session"]["access"]["commands"])
        self.stop(resumed)

    def test_only_the_owners_overlay_sets_a_brake_regardless_of_callback(self):
        command = "owner-brake researcher"
        for callback in (None, (), ("owner",), ("owner", "writeback")):
            for expected in ("", command):
                with self.subTest(callback=callback, gate=expected):
                    preview = HarnessDelegate.preview(
                        "researcher",
                        Spec("input", self.work, client="fake", sid=SID),
                        callback=callback,
                        overlays=({"roles": {"researcher": {"gate": expected}}},),
                        session_cls=HarnessSession,
                    )
                    self.assertEqual(preview.plan.run.gate, expected)
                    self.assertEqual(
                        expected in preview.plan.session.access.commands, bool(expected)
                    )
                    if expected:
                        self.assertIn(f"`{expected}`", preview.plan.session.prompt)

    def test_create_persists_placement_with_overlays(self):
        class Bound(HarnessDelegate):
            def __init__(self, role, spec, **placement):
                super().__init__(role, spec, session_cls=HarnessSession, **placement)

        overlay = {"effort": "high"}
        view = Bound.create(
            "dummy-tester",
            Spec("input", self.work, client="fake", sid=SID),
            display="none",
            callback=("callback",),
            overlays=(overlay,),
        )
        self.assertIsInstance(view, Bound)
        self.assertEqual(self.metadata()["overlays"], [overlay])
        self.assertEqual(
            (self.metadata()["display"], self.metadata()["callback"]), ("none", ["callback"])
        )
        attached = Bound.attach(self.work, sid=SID, session_cls=HarnessSession)
        self.assertEqual(attached.workdir, self.work)


class ReferenceRepoTest(HarnessCase):
    def test_a_local_run_records_no_reference_repo_and_writes_no_intent(self):
        spec = Spec(
            "Reference: T\nRepo: acme/ensemble\n\nHello.",
            self.work,
            task="echo",
            sid=SID,
            client="fake",
        )
        view = self.make(spec=spec)
        self.assertEqual(self.metadata()["plan"]["repo"], None)
        self.assertFalse(self.metadata()["plan"]["docker"])
        view._complete(
            Result(0, Outcome("needs_input", "Question", "Answer needed", questions=["Which?"]))
        )
        view.resume(input="Reference: T\nRepo: acme/other\n\nThe answer.")
        self.assertEqual(self.metadata()["plan"]["repo"], None)
        self.assertNotIn("container-intent.json", os.listdir(os.path.join(self.work, "run")))


class DockerHarnessTest(HarnessCase):
    def test_a_docker_agent_needs_a_harness_on_another_host(self):
        HarnessSession.hosts["box"] = RecordingHost("box")
        with open(self.local, "a", encoding="utf-8") as file:
            file.write(
                "[session.hosts.box]\n"
                'implementation = "ensemble.session.hosts.docker.host:DockerHost"\n'
                'docker = "/usr/local/bin/docker"\n'
            )
        spec = Spec(
            "Reference: T\nRepo: acme/ensemble\n\nHi.",
            self.work,
            mode="tui",
            client="fake",
            sid=SID,
        )
        for harness in (None, "box"):
            with self.subTest(harness=harness), self.assertRaises(ConfigError) as cm:
                self.make(spec=spec, host="box", harness_host=harness)
            self.assertIn('harness_host = "tmux"', str(cm.exception))
        self.assertFalse(os.path.exists(self.work))
        self.make(spec=spec, host="box", harness_host="harness").close()
        self.assertEqual(self.metadata()["plan"]["docker"], True)
        self.assertEqual(self.metadata()["plan"]["repo"], "acme/ensemble")
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertTrue(view._state.plan.docker and view.driver.docker)


class DockerHeadlessTest(HarnessCase):
    def test_a_headless_session_carries_the_agent_name_only_on_the_docker_host(self):
        HarnessSession.hosts["box"] = RecordingHost("box")
        with open(self.local, "a", encoding="utf-8") as file:
            file.write(
                "[session.hosts.box]\n"
                'implementation = "ensemble.session.hosts.docker.host:DockerHost"\n'
                'docker = "/usr/local/bin/docker"\n'
            )
        name = f"dummy-tester-{SID[:8]}"
        for host, want in (("box", name), ("agent", None)):
            with self.subTest(host=host):
                spec = Spec(
                    "input", self.work, task="echo", mode="headless", client="fake", sid=SID
                )
                placement = {"session_cls": HarnessSession, "host": host, "harness_host": "harness"}
                preview = HarnessDelegate.preview("dummy-tester", spec, **placement)
                self.assertEqual((preview.agent, preview.plan.session.name), (want, want))
                self.assertEqual(preview.plan.docker, want is not None)
                self.assertEqual(preview.plan.spec.name, want)
        spec = Spec("input", self.work, task="echo", mode="headless", client="fake", sid=SID)
        view = self.make(spec=spec, host="box", harness_host="harness")
        self.assertEqual(self.metadata()["plan"]["session"]["name"], name)
        view._complete(Result(0, Outcome("needs_input", "Q", "A", questions=["Which?"])))
        view.resume(input="The answer.")
        self.assertEqual(self.metadata()["plan"]["session"]["name"], name)


class DockerPreflightTest(HarnessCase):
    """The real Session's Docker Host, its broker process faked, no daemon socket in its account
    home and its hook command on PATH, preflights a fresh workdir's run.
    """

    def setUp(self):
        super().setUp()
        self.settings(client="claude")
        with open(self.local, "a", encoding="utf-8") as file:
            file.write(
                "[session.hosts.box]\n"
                'implementation = "ensemble.session.hosts.docker.host:DockerHost"\n'
                'docker = "/usr/local/bin/docker"\n'
            )
        self.calls, self.failing = [], False
        path = os.pathsep.join((PLUGIN_BIN, os.environ.get("PATH", os.defpath)))
        for patcher in (
            mock.patch("subprocess.Popen", side_effect=self.broker),
            mock.patch("pwd.getpwuid", return_value=SimpleNamespace(pw_dir=self.home)),
            mock.patch.dict(os.environ, {"PATH": path}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def broker(self, argv, **_):
        command = argv[1]
        self.calls.append(command)
        if self.failing and command == "build":
            return support.FakeProc(
                argv, err=support.text("error-build_failed.json").encode(), code=8
            )
        return support.FakeProc(argv, out=support.text(f"{command}-output.json").encode())

    def create(self, mode):
        spec = Spec("input", self.work, task="echo", mode=mode, sid=SID)
        return self.make(spec=spec, session_cls=Session, host="box", harness_host="harness")

    def test_a_fresh_workdir_passes_the_docker_preflight(self):
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                self.assertFalse(os.path.exists(self.work))
                self.create(mode)
                self.assertEqual(self.calls, ["version", "build"])
                self.assertTrue(os.path.isdir(Layout(self.work).agent))
                self.assertTrue(self.metadata()["plan"]["docker"])
                shutil.rmtree(self.work)
                self.calls.clear()

    def test_a_refused_docker_preflight_leaves_no_workdir(self):
        self.failing = True
        with self.assertRaisesRegex(ConfigError, "Example build_failed"):
            self.create("headless")
        self.assertEqual(self.calls, ["version", "build"])
        self.assertFalse(os.path.exists(self.work))


class CloseTest(HarnessCase):
    def test_a_failed_harness_close_names_the_harness_session(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec, host="agent", harness_host="harness").detach()
        name = self.metadata()["harness_name"]
        process = RecordingHost.processes["harness", name]
        with (
            mock.patch.object(process, "close", side_effect=SessionError("tmux: boom")),
            self.assertRaises(SessionError) as caught,
        ):
            HarnessDelegate.attach(record).close()
        self.assertEqual(str(caught.exception), f"close {name}: tmux: boom")


class IdentityTest(HarnessCase):
    def start_tui(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        return record, HarnessDelegate.attach(record)

    def test_a_foreign_agent_survives_old_run_close(self):
        record, view = self.start_tui()
        original = HarnessSession.live[record.sessions[1]]
        original.stop()
        replacement = HarnessSession.start(
            replace(original.spec, sid=SID.replace("0", "1")), host="agent"
        )
        self.assertFalse(view.status().alive)
        with self.assertRaisesRegex(ConfigError, "still alive"):
            view.resume(input="answer")
        with self.assertRaisesRegex(ConfigError, "still alive"):
            self.make()
        view.close()
        self.assertTrue(replacement.alive())

    def test_a_cached_agent_cannot_close_its_replacement(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        view = self.make(spec=spec)
        view.run()
        name = self.metadata()["agent_name"]
        original = HarnessSession.live[name]
        original.stop()
        HarnessSession.start(replace(original.spec, sid=SID.replace("0", "1")), host="agent")
        view.close()
        self.assertTrue(RecordingHost.processes["agent", name].alive())

    def test_a_foreign_harness_survives_old_run_close(self):
        for sid in (SID.replace("0", "1"), None):
            with self.subTest(sid=sid):
                HarnessSession.reset()
                self.work = os.path.join(self.home, f"run-{sid}")
                record, view = self.start_tui()
                name = record.sessions[0]
                host = HarnessSession.host("agent")
                host.attach(name).close()
                replacement = host.detach(
                    name,
                    Launch(("replacement",)),
                    record=None if sid is None else replace(record, sid=sid, sessions=(name,)),
                )
                self.assertFalse(view.status().alive)
                view.close()
                self.assertTrue(replacement.alive())
                replacement.close()


class OuterTest(HarnessCase):
    def test_the_hosts_record_is_the_one_detach_returns_named_by_the_outer_starter(self):
        outer = {
            "kind": "pipeline",
            "name": "TASK-1",
            "ctl": ("ensemble-orchestrator",),
            "resume": ("ensemble-orchestrator", "resume", "TASK-1"),
        }
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach(outer=outer)
        stored = RecordingHost.processes["agent", self.metadata()["harness_name"]].record()
        self.assertEqual((record.name, stored), ("TASK-1", record))
        self.assertEqual(HarnessDelegate.attach(record).resume(), record)


class RunDirTest(HarnessCase):
    def record(self):
        return os.path.join(self.work, "run", "delegate.json")

    def stored(self):
        with open(self.record(), encoding="utf-8") as file:
            return json.load(file)

    def test_the_default_session_class_is_null(self):
        self.make(session_cls=Session, driver=AgentDriver(HarnessSession))
        self.assertIsNone(self.stored()["session_cls"])
        self.assertIs(HarnessDelegate.attach(self.work).session_cls, Session)

    def test_a_historical_default_class_path_still_loads(self):
        self.make(session_cls=Session, driver=AgentDriver(HarnessSession))
        data = self.stored()
        data["session_cls"] = f"{Session.__module__}:{Session.__qualname__}"
        with open(self.record(), "w", encoding="utf-8") as file:
            json.dump(data, file)
        self.assertIs(HarnessDelegate.attach(self.work).session_cls, Session)

    def test_an_injected_session_class_is_loaded_from_its_saved_path(self):
        self.make()
        self.assertEqual(
            self.stored()["session_cls"],
            "tests.delegate.fakes.harness_session:HarnessSession",
        )
        self.assertIs(HarnessDelegate.attach(self.work).session_cls, HarnessSession)

    def test_a_stored_outcome_loads_unchecked_for_recovers_replay(self):
        # A run begun before summary's limit fell from 4000 to 300 is still written back.
        self.make()
        data = self.stored()
        outcome = Outcome("done", "T", "S" * 4000)
        data["result"] = {
            "returncode": 0,
            "outcome": asdict(outcome),
            "error": "",
            "limit": None,
            "instruction": None,
        }
        with open(self.record(), "w", encoding="utf-8") as file:
            json.dump(data, file)
        self.assertEqual(HarnessDelegate.attach(self.work).outcome(), outcome)

    def corrupt(self, text):
        self.make()
        with open(self.record(), "w", encoding="utf-8") as file:
            file.write(text)

    def test_the_run_record_is_the_runs_only_state(self):
        self.make()
        self.assertEqual(sorted(os.listdir(self.work)), ["agent", "run"])
        self.assertEqual(self.stored()["spec"]["sid"], SID)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.work, "run"))), ["delegate.json", "delegate.lock"]
        )
        self.assertEqual(os.listdir(self.ensemble), ["config.local.toml"])

    def test_refused_start_creates_nothing(self):
        top = os.path.join(self.home, "a")
        self.work = os.path.join(top, "b", "work")
        HarnessSession.preflight_error = SessionConfigError("preflight refused")
        with self.assertRaisesRegex(ConfigError, "preflight refused"):
            self.make()
        self.assertEqual(len(HarnessSession.preflights), 1)
        HarnessSession.preflight_error = None
        with self.assertRaisesRegex(ConfigError, "absent-role"):
            self.make(role="absent-role")
        self.assertFalse(os.path.lexists(top))
        self.assertEqual(os.listdir(self.ensemble), ["config.local.toml"])

    def test_a_refused_start_on_a_workdir_makes_none_of_its_dirs(self):
        os.mkdir(self.work)
        HarnessSession.preflight_error = SessionConfigError("preflight refused")
        with self.assertRaisesRegex(ConfigError, "preflight refused"):
            self.make()
        self.assertEqual(os.listdir(self.work), [])

    def test_a_live_run_refuses_a_new_one_before_making_the_agent_dir(self):
        self.make()
        os.rmdir(os.path.join(self.work, "agent"))
        before = self.stored()
        with (
            RunState(Layout(self.work)).run_lock(),
            self.assertRaisesRegex(ConfigError, "delegate is still alive"),
        ):
            self.make()
        self.assertEqual((os.listdir(self.work), self.stored()), (["run"], before))

    def test_a_new_run_of_the_same_sid_counts_the_next_attempt(self):
        self.make()
        self.make()
        self.assertEqual(self.stored()["attempt"], 2)
        self.make(spec=Spec("input", self.work, client="fake", sid=SID.replace("0", "1")))
        self.assertEqual(self.stored()["attempt"], 1)

    def test_a_corrupt_run_record_keeps_a_readable_attempt(self):
        for text, attempt in (("not json", 1), ('{"attempt": 4}', 5)):
            with self.subTest(text=text):
                self.corrupt(text)
                self.make()
                self.assertEqual(self.stored()["attempt"], attempt)

    def test_a_held_run_lock_keeps_a_corrupt_run_record(self):
        self.corrupt("not json")
        with (
            RunState(Layout(self.work)).run_lock(),
            self.assertRaisesRegex(ConfigError, "delegate is still alive"),
        ):
            self.make()
        with open(self.record(), encoding="utf-8") as file:
            self.assertEqual(file.read(), "not json")

    def test_an_old_layout_workdir_names_its_missing_run_record(self):
        self.make()
        os.replace(self.record(), os.path.join(self.work, "delegate.json"))
        with self.assertRaises(ConfigError) as caught:
            HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertEqual(str(caught.exception), f"invalid {self.record()}: missing metadata")


def tree(top):
    found = {}
    for root, _dirs, files in os.walk(top):
        for name in files:
            with open(os.path.join(root, name), "rb") as file:
                found[os.path.relpath(os.path.join(root, name), top)] = file.read()
    return found


def go_cache(root):
    """A Go module cache below `root`: read-only directories holding a read-only file."""
    module = os.path.join(root, "gopath", "mod", "github.com", "mattn", "go-shellwords@v1.0.12")
    os.makedirs(module)
    with open(os.path.join(module, "go.mod"), "w") as file:
        file.write("module x\n")
    os.chmod(os.path.join(module, "go.mod"), 0o444)
    while module != os.path.join(root, "gopath"):
        os.chmod(module, 0o555)
        module = os.path.dirname(module)


class CleanTest(HarnessCase):
    def agent(self):
        return Layout(self.work).agent

    def refused(self, message, workdir=None, **options):
        for dry in (False, True):
            with self.subTest(dry=dry), self.assertRaises(ConfigError) as caught:
                HarnessDelegate.clean(workdir or self.work, dry=dry, **options)
            self.assertEqual(str(caught.exception), message)

    def test_checkouts_lists_real_src_entries(self):
        self.assertIsNone(HarnessDelegate.checkouts(self.work))
        self.make()
        self.assertEqual(HarnessDelegate.checkouts(self.work), ())
        src = os.path.join(self.agent(), "src")
        real = os.path.join(os.path.realpath(self.work), "agent", "src")
        for path in ("o/r-TASK-1", "p/q-TASK-1"):
            os.makedirs(os.path.join(src, path))
        elsewhere = os.path.join(self.home, "elsewhere")
        os.makedirs(os.path.join(elsewhere, "n-TASK-1"))
        os.symlink(os.path.join(elsewhere, "n-TASK-1"), os.path.join(src, "o", "s-TASK-1"))
        os.symlink(elsewhere, os.path.join(src, "x"))
        with open(os.path.join(src, "o", "f-TASK-1"), "w") as file:
            file.write("x")
        self.assertEqual(
            HarnessDelegate.checkouts(self.work),
            (os.path.join(real, "o", "r-TASK-1"), os.path.join(real, "p", "q-TASK-1")),
        )
        moved = os.path.join(self.home, "moved")
        os.replace(self.agent(), moved)
        os.symlink(moved, self.agent())
        self.assertEqual(HarnessDelegate.checkouts(self.work), ())

    def test_clean_deletes_agent_files_and_keeps_run_records(self):
        self.make()
        deep = os.path.join(self.agent(), "tmp", "deep")
        os.makedirs(deep)
        os.makedirs(os.path.join(self.agent(), "src", "o", "r-TASK-1"))
        with open(os.path.join(deep, "pr.md"), "w") as file:
            file.write("x")
        os.chmod(os.path.join(deep, "pr.md"), 0o444)
        with open(os.path.join(self.work, "notes.md"), "w") as file:
            file.write("kept")
        run = tree(Layout(self.work).run)
        name = HarnessDelegate.attach(self.work).status().name
        self.assertTrue(HarnessDelegate.clean(self.work, dry=True))
        self.assertTrue(os.path.isdir(deep))
        self.assertTrue(HarnessDelegate.clean(self.work))
        self.assertFalse(os.path.lexists(self.agent()))
        self.assertEqual(tree(Layout(self.work).run), run)
        self.assertEqual(sorted(os.listdir(self.work)), ["notes.md", "run"])
        self.assertIsNone(HarnessDelegate.checkouts(self.work))
        self.assertEqual(HarnessDelegate.attach(self.work).status().name, name)
        self.assertFalse(HarnessDelegate.clean(self.work))
        never = os.path.join(self.home, "never")
        self.assertFalse(HarnessDelegate.clean(never))
        self.assertFalse(os.path.lexists(never))

    def test_scratch_deletes_only_checkouts_and_temp_files_never_following_a_link(self):
        self.make()
        agent = self.agent()
        for path in ("src/o/r-TASK-1/f", "publish/Research/r.md", "prd.md"):
            os.makedirs(os.path.dirname(os.path.join(agent, path)), exist_ok=True)
            with open(os.path.join(agent, path), "w") as file:
                file.write("x")
        elsewhere = os.path.join(self.home, "elsewhere")
        os.makedirs(elsewhere)
        open(os.path.join(elsewhere, "keep"), "w").close()
        os.symlink(elsewhere, os.path.join(agent, "tmp"))
        run = tree(Layout(self.work).run)
        self.assertTrue(HarnessDelegate.clean(self.work, dry=True, scratch=True))
        self.assertEqual(sorted(os.listdir(agent)), ["prd.md", "publish", "src", "tmp"])
        self.assertTrue(HarnessDelegate.clean(self.work, scratch=True))
        self.assertEqual(os.listdir(agent), ["prd.md"])
        self.assertEqual(os.listdir(elsewhere), ["keep"])
        self.assertEqual(tree(Layout(self.work).run), run)
        self.assertFalse(HarnessDelegate.clean(self.work, scratch=True))
        self.assertFalse(HarnessDelegate.clean(os.path.join(self.home, "never"), scratch=True))

    def test_clean_and_scratch_delete_a_read_only_go_module_cache(self):
        for scratch, left in ((True, ["prd.md"]), (False, None)):
            with self.subTest(scratch=scratch):
                self.make()
                go_cache(os.path.join(self.agent(), "tmp"))
                go_cache(os.path.join(self.agent(), "src", "o", "r-TASK-1"))
                open(os.path.join(self.agent(), "prd.md"), "w").close()
                self.assertTrue(HarnessDelegate.clean(self.work, scratch=scratch))
                self.assertEqual(
                    os.listdir(self.agent()) if os.path.lexists(self.agent()) else None, left
                )

    def test_scratch_refuses_a_live_run_or_one_whose_liveness_is_unknown(self):
        self.make()
        os.makedirs(os.path.join(self.agent(), "tmp"))
        with RunState(Layout(self.work)).run_lock():
            self.refused(f"{self.work}: delegate is still alive", scratch=True)
        with open(Layout(self.work).run + "/delegate.json", "w") as file:
            file.write("not json")
        self.refused(f"{self.work}: delegate liveness unknown", scratch=True)
        self.assertTrue(os.path.isdir(os.path.join(self.agent(), "tmp")))

    def test_scratch_refuses_an_agent_session_left_open_after_its_run(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        view.close()
        HarnessSession.script = (Reply("No outcome block."), Exited(0))
        view.run()
        self.assertFalse(view.alive())
        os.makedirs(os.path.join(self.agent(), "tmp"), exist_ok=True)
        self.refused(f"{self.work}: its agent session is still open", scratch=True)
        self.assertTrue(os.path.isdir(os.path.join(self.agent(), "tmp")))

    def test_clean_refuses_a_live_run(self):
        self.make()
        with RunState(Layout(self.work)).run_lock():
            self.refused(f"{self.work}: delegate is still alive")
        with open(Layout(self.work).run + "/delegate.json", "w") as file:
            file.write("not json")
        with RunState(Layout(self.work)).run_lock():
            self.refused(f"{self.work}: delegate is still alive")
        self.assertTrue(os.path.isdir(self.agent()))

    def test_clean_without_agent_files_refuses_a_live_run(self):
        self.make()
        shutil.rmtree(self.agent())
        with RunState(Layout(self.work)).run_lock():
            files = tree(self.work)
            self.refused(f"{self.work}: delegate is still alive")
            self.assertEqual(tree(self.work), files)

    def test_clean_without_agent_files_keeps_an_ended_run(self):
        self.make()
        shutil.rmtree(self.agent())
        files = tree(self.work)
        for dry in (False, True):
            with self.subTest(dry=dry):
                self.assertFalse(HarnessDelegate.clean(self.work, dry=dry))
                self.assertEqual(tree(self.work), files)

    def test_clean_refuses_a_live_detached_run(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec, host="agent", harness_host="harness").detach()
        self.refused(f"{self.work}: delegate is still alive")
        self.assertTrue(os.path.isdir(self.agent()))

    def test_clean_refuses_an_agent_dir_out_of_place(self):
        self.make()
        elsewhere = os.path.join(self.home, "elsewhere")
        os.makedirs(os.path.join(elsewhere, "keep"))
        os.replace(self.agent(), os.path.join(self.home, "agent"))
        os.symlink(elsewhere, self.agent())
        self.refused(f"not a real directory: {self.agent()}, refusing to touch")
        self.assertTrue(os.path.isdir(os.path.join(elsewhere, "keep")))
        os.unlink(self.agent())
        with open(self.agent(), "w") as file:
            file.write("x")
        self.refused(f"not a real directory: {self.agent()}, refusing to touch")
        os.unlink(self.agent())
        os.replace(os.path.join(self.home, "agent"), self.agent())
        link = os.path.join(self.home, "link")
        os.symlink(self.work, link)
        self.refused(f"not a real directory: {link}/agent, refusing to touch", link)
        self.assertTrue(os.path.isdir(self.agent()))

    def test_clean_holds_the_run_mutex_while_it_deletes(self):
        self.make()
        held = []

        def rmtree(path, **_kwargs):
            with open(Layout(self.work).run + "/delegate.lock", "rb") as file:
                try:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(path)

        with mock.patch.object(shutil, "rmtree", side_effect=rmtree):
            HarnessDelegate.clean(self.work)
        self.assertEqual(held, [self.agent()])

    def test_a_new_run_after_clean_lays_out_again_with_the_next_attempt(self):
        self.make()
        HarnessDelegate.clean(self.work)
        self.make()
        self.assertTrue(os.path.isdir(self.agent()))
        self.assertEqual(self.metadata()["attempt"], 2)


class RunEndTest(HarnessCase):
    """A run's end clears agent/tmp but an `out` directly in it, unless its agent session is open."""

    def setUp(self):
        super().setUp()
        self.tmp = Layout(self.work).tmp
        self.elsewhere = os.path.join(self.home, "elsewhere")
        os.makedirs(self.elsewhere)
        open(os.path.join(self.elsewhere, "keep"), "w").close()

    def kept(self, **spec):
        """A run whose `out` is agent/tmp/deliverable.md."""
        out = os.path.join(self.tmp, "deliverable.md")
        return self.make(spec=Spec("input", self.work, sid=SID, client="fake", out=out, **spec))

    def fill(self):
        os.makedirs(os.path.join(self.tmp, "deep"), exist_ok=True)
        for name in ("deep/pr.md", "msg", "deliverable.md"):
            with open(os.path.join(self.tmp, name), "w") as file:
                file.write("x")
        os.symlink(self.elsewhere, os.path.join(self.tmp, "link"))

    def test_a_finished_run_leaves_only_its_out_there(self):
        view = self.kept(task="echo")
        self.fill()
        view.run()
        self.assertEqual(os.listdir(self.tmp), ["deliverable.md"])
        self.assertEqual(os.listdir(self.elsewhere), ["keep"])

    def test_a_run_whose_out_lies_elsewhere_or_without_a_temp_dir_ends_cleanly(self):
        view = self.make()
        self.fill()
        view.run()
        self.assertFalse(os.path.lexists(self.tmp))
        self.assertEqual(os.listdir(self.elsewhere), ["keep"])
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.make().run()
        self.assertNotIn(self.tmp, err.getvalue())

    def test_a_symlinked_temp_dir_is_unlinked_never_followed(self):
        view = self.make()
        os.symlink(self.elsewhere, self.tmp)
        view.run()
        self.assertFalse(os.path.lexists(self.tmp))
        self.assertEqual(os.listdir(self.elsewhere), ["keep"])

    def test_a_run_that_raises_clears_it_too(self):
        view = self.make()
        self.fill()

        def fail(_event):
            raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            view.run(on_event=fail)
        self.assertFalse(os.path.lexists(self.tmp))

    def test_an_agent_session_left_open_keeps_it(self):
        view = self.make(spec=Spec("input", self.work, mode="tui", client="fake", sid=SID))
        self.fill()
        HarnessSession.script = (Reply("No outcome block."), Exited(0))
        view.run()
        self.assertEqual(sorted(os.listdir(self.tmp)), ["deep", "deliverable.md", "link", "msg"])

    def test_a_read_only_go_module_cache_is_cleared_too(self):
        for kept, left in ((True, ["deliverable.md"]), (False, None)):
            with self.subTest(kept=kept):
                view = self.kept(task="echo") if kept else self.make()
                self.fill()
                go_cache(self.tmp)
                view.run()
                self.assertEqual(os.listdir(self.tmp) if os.path.lexists(self.tmp) else None, left)

    def test_a_removal_error_is_printed_and_the_result_kept(self):
        view = self.make()
        self.fill()
        with (
            mock.patch.object(shutil, "rmtree", side_effect=PermissionError("no")),
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            result = view.run()
        self.assertEqual((result.outcome.status, err.getvalue()[-13:]), ("done", "delegate: no\n"))


class PreviewTest(HarnessCase):
    def test_preview_names_the_sessions_hosts_and_callback_and_writes_nothing(self):
        for mode, agent in (("tui", f"dummy-tester-{SID[:8]}"), ("headless", None)):
            with self.subTest(mode=mode):
                spec = Spec("input", self.work, task="echo", mode=mode, client="fake", sid=SID)
                preview = HarnessDelegate.preview(
                    "dummy-tester",
                    spec,
                    session_cls=HarnessSession,
                    harness_host="harness",
                    callback=["writeback", "--issue", "ticket-id"],
                )
                self.assertEqual(
                    (
                        preview.agent,
                        preview.harness,
                        preview.host,
                        preview.harness_host,
                        preview.callback,
                        preview.plan.sid,
                        preview.plan.spec.mode,
                    ),
                    (
                        agent,
                        f"dummy-tester-{SID[:8]}-drive",
                        "agent",
                        "harness",
                        ("writeback", "--issue", "ticket-id"),
                        SID,
                        mode,
                    ),
                )
                self.assertTrue(preview.plan.prompt.endswith("\n# Input\n\ninput\n"))
        self.assertFalse(os.path.exists(self.work))
        self.assertEqual(os.listdir(self.ensemble), ["config.local.toml"])
        self.assertEqual((HarnessSession.started, RecordingHost.launches), ([], []))

    def test_preview_refuses_an_unknown_role_like_the_constructor(self):
        spec = Spec("input", self.work, client="fake", sid=SID)
        with self.assertRaisesRegex(ConfigError, "absent-role"):
            HarnessDelegate.preview("absent-role", spec, session_cls=HarnessSession)
        self.assertFalse(os.path.exists(self.work))


class InstructTest(HarnessCase):
    def test_instruct_delivers_the_composed_prompt_without_state_or_session(self):
        out = os.path.join(self.work, "prompt.md")
        result = HarnessDelegate.instruct(
            "dummy-tester", Spec("Hello.", self.work, task="echo", out=out)
        )
        self.assertEqual(result, Result(0, None, instruction=out))
        composer = Composer()
        want = composer.render(
            composer.resolve("dummy-tester", "echo"),
            RunParams(input="Hello.", out=out, workdir=Layout(self.work).agent),
        )
        with open(out, encoding="utf-8") as file:
            self.assertEqual(file.read(), want)
        self.assertIn("**Your task**: `echo`", want)
        self.assertEqual(os.listdir(self.work), ["prompt.md"])
        self.assertEqual((HarnessSession.started, RecordingHost.launches), ([], []))


class CheckTuiTest(HarnessCase):
    def setUp(self):
        super().setUp()
        HarnessSession.reset(showable=True)
        self.settings(harness_host="harness")
        self.events = os.path.join(self.home, "events")

    def check(self, **kw):
        HarnessDelegate.check_tui(session_cls=HarnessSession, **kw)

    def test_the_agents_host_places_the_display_and_the_harness_takes_the_events(self):
        with (
            mock.patch.object(FakeViewer, "layout", autospec=True) as layout,
            mock.patch.object(ShowingHost, "events_path", autospec=True) as events_path,
        ):
            self.check(display="opener/right", events=self.events)
        [(_, host)] = [call.args for call in layout.call_args_list]
        self.assertEqual(
            (host.name, layout.call_args.kwargs), ("agent", {"display": "opener/right"})
        )
        [(host, path)] = [call.args for call in events_path.call_args_list]
        self.assertEqual((host.name, path), ("harness", self.events))
        self.assertFalse(os.path.exists(self.events))

    def test_a_host_refusing_the_display_or_the_events_is_a_config_error(self):
        for owner, method, error in (
            (FakeViewer, "layout", SessionError("no pane to show the TUI beside (not in tmux)")),
            (FakeViewer, "layout", SessionConfigError("no tmux session absent")),
            (
                FakeViewer,
                "layout",
                SessionConfigError("display 'a/sideways': not <opener>[/<split>]"),
            ),
            (
                ShowingHost,
                "events_path",
                SessionConfigError(f"events file {self.events}: Permission denied"),
            ),
        ):
            with (
                self.subTest(method=method, error=error),
                mock.patch.object(owner, method, side_effect=error),
                self.assertRaises(ConfigError) as caught,
            ):
                self.check(display="d", events=self.events)
            self.assertEqual(str(caught.exception), str(error))
        self.assertEqual(RecordingHost.launches, [])


class AttendedBusyTest(HarnessCase):
    """An attended TUI whose harness ended before its agent went live, by its recorded result."""

    BUSY = "busy: lock dummy-lock is held by ensemble-other-x (owner other)"

    def started(self, error, lock="dummy-lock"):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        delegate = self.make(spec=spec, host="agent", harness_host="harness")
        state = delegate._state
        environment = (
            None if lock is None else EnvironmentSpec(image="x", gateway="workers", lock=lock)
        )
        session = replace(state.plan.session, environment=environment)
        state = replace(
            state, plan=replace(state.plan, session=session), result=Result(1, None, error)
        )
        with (
            mock.patch.object(delegate, "_answered_agent", return_value=None),
            mock.patch.object(delegate, "_harness", return_value=None),
            mock.patch.object(delegate, "_read", return_value=state),
        ):
            delegate._started(state)

    def test_attended_tui_busy_raises_session_busy(self):
        for error in (f"tui: {self.BUSY}", self.BUSY):
            with self.subTest(error), self.assertRaises(SessionBusy) as caught:
                self.started(error)
            self.assertEqual(
                (caught.exception.lock, caught.exception.holder),
                ("dummy-lock", "ensemble-other-x (owner other)"),
            )

    def test_a_foreign_or_unprintable_busy_message_keeps_did_not_start(self):
        for error, lock in (
            (f"tui: {self.BUSY}", "other-lock"),
            (f"tui: {self.BUSY}", None),
            ("tui: busy: lock dummy-lock is held by \x1b[2J (owner o)", "dummy-lock"),
            ("tui: broker: start failed", "dummy-lock"),
        ):
            with self.subTest(error=error, lock=lock), self.assertRaises(SessionError) as caught:
                self.started(error, lock)
            self.assertNotIsInstance(caught.exception, SessionBusy)
            self.assertRegex(str(caught.exception), "did not start$")


class LaunchTest(HarnessCase):
    def test_the_launchers_variables_stay_out_of_the_harness_environment(self):
        launcher = {"ENSEMBLE_SID": SID, "ENSEMBLE_EVENTS": "/manager/events", "KEPT": "kept"}
        lease = (
            "ENSEMBLE_MANAGER",
            "ENSEMBLE_HOLDER_ID",
            "ENSEMBLE_HOLDER_GENERATION",
            "ENSEMBLE_HOLDER_PID",
        )
        with mock.patch.dict(os.environ, launcher | dict.fromkeys(lease, "1")):
            self.make().detach()
        environment = RecordingHost.launches[0]["env"]
        for name in ("ENSEMBLE_SID", "ENSEMBLE_EVENTS", *lease):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["KEPT"], "kept")

    def test_the_harness_is_no_agent_session_so_carries_no_marker(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("ENSEMBLE_AGENT_SESSION", None)
            self.make().detach()
        self.assertNotIn("ENSEMBLE_AGENT_SESSION", RecordingHost.launches[0]["env"])


class SaveFailureTest(HarnessCase):
    def test_a_failed_result_save_still_hands_on_the_runs_real_finished(self):
        delegate, events, real = self.make(), [], os.replace
        record = os.path.join(Layout(self.work).run, "delegate.json")

        def replace(src, dst):
            if str(dst) == record:
                raise OSError("disk full")
            real(src, dst)

        with mock.patch.object(os, "replace", replace):
            result = delegate.run(on_event=events.append)
        finished = [e for e in events if isinstance(e, Finished)]
        self.assertEqual([e.result for e in finished], [result])
        self.assertEqual(result.outcome.status, "done")


class UsageSession(HarnessSession):
    clients = []

    @classmethod
    def usage(cls, client):
        cls.clients.append(client)


class OverlayTest(HarnessCase):
    def test_usage_reads_the_client_the_owner_overlays_resolve(self):
        UsageSession.clients = []
        overlays = ({"roles": {"dummy-tester": {"client": "claude"}}},)
        HarnessDelegate.usage("dummy-tester", session_cls=UsageSession)
        HarnessDelegate.usage("dummy-tester", overlays=overlays, session_cls=UsageSession)
        HarnessDelegate.usage(
            "dummy-tester",
            overlays=({"roles": {"dummy-tester": {"client": "opencode", "tier": 3}}},),
            session_cls=UsageSession,
        )
        self.assertEqual(UsageSession.clients, ["fake", "claude", "opencode"])

    def test_owner_overlays_shape_the_persisted_run(self):
        self.make()
        self.assertEqual(self.metadata()["plan"]["run"]["effort"], "low")
        self.make(overlays=({"roles": {"dummy-tester": {"effort": "high"}}},))
        self.assertEqual(self.metadata()["plan"]["run"]["effort"], "high")

    def test_a_record_without_overlays_is_corrupt(self):
        self.make()
        path = self.state_record()
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
        self.assertEqual(data["overlays"], [])
        del data["overlays"]
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file)
        with self.assertRaises(ConfigError) as caught:
            HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertEqual(str(caught.exception), f"invalid {path}: 'overlays'")

    def test_resume_resolves_with_the_overlays_it_was_built_with(self):
        record = self.make(overlays=({"roles": {"dummy-tester": {"effort": "high"}}},)).detach()
        self.stop(record)
        HarnessDelegate.attach(self.work, session_cls=HarnessSession).resume()
        self.assertEqual(self.metadata()["plan"]["run"]["effort"], "high")


class AccountsTest(HarnessCase):
    """The role's accounts table, resolved with the run's overlays, picks its session's account."""

    def setUp(self):
        super().setUp()
        self.configured(
            "[session.clients.fake.accounts.work]\n[session.clients.fake.accounts.home]\n"
        )

    def configured(self, text):
        with open(self.local, "a", encoding="utf-8") as file:
            file.write(text)

    def env(self):
        return self.metadata()["plan"]["session"]["env"]

    def test_create_takes_the_role_table_then_each_overlay(self):
        self.make()
        self.assertNotIn(ACCOUNT_ENV, self.env())
        self.configured('[delegate.roles.dummy-tester.accounts]\nfake = "work"\n')
        self.make()
        self.assertEqual(self.env()[ACCOUNT_ENV], "work")
        self.make(overlays=({"roles": {"dummy-tester": {"accounts": {"fake": "home"}}}},))
        self.assertEqual(self.env()[ACCOUNT_ENV], "home")
        self.make(overlays=({"accounts": {"claude": "home"}},))
        self.assertEqual(self.env()[ACCOUNT_ENV], "work")

    def test_resume_re_resolves_with_the_current_config_and_its_overlays(self):
        record = self.make(overlays=({"accounts": {"fake": "home"}},)).detach()
        self.assertEqual(self.env()[ACCOUNT_ENV], "home")
        self.stop(record)
        self.configured('[delegate.accounts]\nfake = "work"\n')
        HarnessDelegate.attach(self.work, session_cls=HarnessSession).resume()
        self.assertEqual(self.env()[ACCOUNT_ENV], "home")
        self.work = os.path.join(self.home, "run-2")
        record = self.make().detach()
        self.assertEqual(self.env()[ACCOUNT_ENV], "work")
        self.stop(record)
        self.configured('[delegate.roles.dummy-tester.accounts]\nfake = "home"\n')
        HarnessDelegate.attach(self.work, session_cls=HarnessSession).resume()
        self.assertEqual(self.env()[ACCOUNT_ENV], "home")


class ResumeInputTest(HarnessCase):
    def waiting(self, *, mode="headless", outer=None, close=True):
        events = os.path.join(self.home, "events")
        spec = Spec(
            "initial", self.work, task="echo", mode=mode, client="fake", sid=SID, events=events
        )
        record = self.make(spec=spec, display="none").detach(outer=outer)
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        view._complete(
            Result(0, Outcome("needs_input", "Question", "Answer needed", questions=["Which?"]))
        )
        if close:
            view.close()
        return view, record

    def test_answers_use_the_same_identity_placement_and_new_prompt(self):
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                view, record = self.waiting(mode=mode)
                before = self.metadata()
                resumed = view.resume(input="The answer.")
                state = self.metadata()
                self.assertEqual(
                    (resumed.sid, resumed.name, resumed.display),
                    (record.sid, record.name, record.display),
                )
                self.assertEqual(state["attempt"], before["attempt"] + 1)
                self.assertIsNone(state["result"])
                self.assertEqual(state["spec"]["input"], "The answer.")
                for key in (
                    "role",
                    "display",
                    "callback",
                    "events",
                    "agent_name",
                    "harness_name",
                    "outer",
                    "overlays",
                ):
                    self.assertEqual(state[key], before[key], key)
                for key in ("sid", "task", "mode"):
                    self.assertEqual(state["spec"][key], before["spec"][key], key)
                self.assertTrue(
                    HarnessSession.preflights[-1]["spec"].prompt.endswith(
                        "\n# Input\n\nThe answer.\n"
                    )
                )
                launches = len(harness_launches())
                with self.assertRaises(ConfigError):
                    view.resume(input="The answer.")
                self.assertEqual(len(harness_launches()), launches)
                view.close()

    def test_input_refuses_every_outcome_other_than_needs_input_without_changes(self):
        for status in (None, "done", "failed"):
            with self.subTest(status=status):
                view = self.make()
                if status is not None:
                    HarnessSession.script = (
                        Reply(
                            block(
                                {
                                    "status": status,
                                    "title": "T",
                                    "summary": "S",
                                    "deliverable": "body",
                                }
                            )
                        ),
                        Exited(0),
                    )
                    view.run()
                before, launches = self.metadata(), len(harness_launches())
                with self.assertRaisesRegex(ConfigError, "needs_input"):
                    view.resume(input="answer")
                self.assertEqual(self.metadata(), before)
                self.assertEqual(len(harness_launches()), launches)

    def test_input_refuses_a_still_live_harness(self):
        view, _ = self.waiting(close=False)
        before, launches = self.metadata(), len(harness_launches())
        with self.assertRaisesRegex(ConfigError, "alive"):
            view.resume(input="answer")
        self.assertEqual(self.metadata(), before)
        self.assertEqual(len(harness_launches()), launches)

    def test_pipeline_input_is_refused_only_at_the_delegate_cli(self):
        from ensemble.delegate import cli

        outer = {
            "kind": "pipeline",
            "name": "TASK-1",
            "ctl": ("ensemble-orchestrator",),
            "resume": ("ensemble-orchestrator", "resume", "TASK-1"),
        }
        view, record = self.waiting(outer=outer)
        self.assertEqual(view.ctl(), ("ensemble-orchestrator",))
        before, launches = self.metadata(), len(harness_launches())

        class Bound(HarnessDelegate):
            @classmethod
            def attach(cls, key, *, sid=None):
                return HarnessDelegate.attach(key, sid=sid, session_cls=HarnessSession)

        for arguments, stdin in (
            (["resume", self.work, "--input", "answer"], ""),
            (["resume", "--input", "answer"], record.to_line()),
        ):
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch("sys.stdout", out),
                mock.patch("sys.stderr", err),
                mock.patch("sys.stdin", io.StringIO(stdin)),
            ):
                code = cli.main(arguments, delegate_cls=Bound)
            self.assertEqual((code, out.getvalue()), (2, ""))
            self.assertIn("pipeline", err.getvalue())
            self.assertEqual(self.metadata(), before)
            self.assertEqual(len(harness_launches()), launches)
        self.assertEqual(view.resume(input="answer").ctl, ("ensemble-orchestrator",))


class ResumeModeTest(HarnessCase):
    def gave_up(self, mode):
        """A detached run whose harness ended without an outcome, a tui agent session left open."""
        spec = Spec("input", self.work, mode=mode, client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        view.close()
        HarnessSession.script = (Reply("No outcome block."), Exited(0))
        self.assertIsNone(view.run().outcome)
        self.assertFalse(view.alive())

    def resumed(self, **mode):
        launches = len(harness_launches())
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        record = view.resume(**mode)
        self.assertEqual(len(harness_launches()), launches + 1)
        self.assertEqual(view.resume(**mode), record)
        self.assertEqual(len(harness_launches()), launches + 1)
        return record, self.metadata()

    def modes(self, state):
        return (state["spec"]["mode"], state["plan"]["session"]["mode"], state["attempt"])

    def test_a_role_showing_none_runs_at_the_none_display(self):
        for show, mode, want in (
            ("none", "tui", "none"),
            ("open {{session}}", "tui", "mgr"),
            ("", "headless", "mgr"),
        ):
            with self.subTest(show=show, mode=mode):
                spec = Spec("input", self.work, mode=mode, client="fake", sid=SID)
                made = self.make(spec=spec, overlays=({"show": show},), display="mgr")
                self.assertEqual((made.driver.display, self.metadata()["display"]), (want, want))
        spec = Spec("input", self.work, mode="headless", client="fake", sid=SID)
        record = self.make(spec=spec, overlays=({"show": "none"},), display="mgr").detach()
        self.assertEqual((record.display, harness_launches()[-1]["display"]), ("none", "none"))
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        view.close()
        HarnessSession.script = (Reply("No outcome block."), Exited(0))
        self.assertIsNone(view.run().outcome)
        record, state = self.resumed(mode="tui")
        self.assertEqual((record.sid, record.display, state["display"]), (SID, "none", "none"))
        started = [each for each in RecordingHost.launches if each["name"] == state["agent_name"]]
        self.assertEqual(started[-1]["display"], "none")

    def test_a_tui_run_that_gave_up_resumes_headless_with_its_sid_and_records_the_mode(self):
        self.gave_up("tui")
        agent = HarnessSession.live[self.metadata()["agent_name"]]
        record, state = self.resumed(mode="headless")
        self.assertEqual((record.sid, record.sessions), (SID, (state["harness_name"],)))
        self.assertEqual(self.modes(state), ("headless", "headless", 2))
        self.assertEqual((state["spec"]["name"], state["spec"]["events"]), (None, None))
        self.assertEqual(agent.alive(), False)
        self.assertEqual(HarnessSession.preflights[-1]["spec"].mode, "headless")

    def test_without_a_mode_a_tui_run_resumes_in_tui(self):
        self.gave_up("tui")
        before = self.metadata()
        record, state = self.resumed()
        self.assertEqual(record.sessions, (before["harness_name"], before["agent_name"]))
        self.assertEqual(self.modes(state), ("tui", "tui", 2))
        self.assertEqual(
            (state["spec"]["name"], state["spec"]["events"]),
            (before["spec"]["name"], before["spec"]["events"]),
        )

    def test_a_headless_run_resumes_in_tui_with_its_agent_session_live(self):
        self.gave_up("headless")
        record, state = self.resumed(mode="tui")
        self.assertEqual(record.sessions, (state["harness_name"], state["agent_name"]))
        self.assertEqual(self.modes(state), ("tui", "tui", 2))
        self.assertEqual(
            (state["spec"]["name"], state["spec"]["events"]), (state["agent_name"], state["events"])
        )
        self.assertEqual(HarnessSession.live[state["agent_name"]].sid, SID)

    def test_a_mode_detach_refuses_is_refused_changing_nothing(self):
        self.gave_up("tui")
        before = self.metadata()
        launches = len(harness_launches())
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        for mode in ("in_process", "remote"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ConfigError, repr(mode)):
                view.resume(mode=mode)
        self.assertEqual(self.metadata(), before)
        self.assertEqual(len(harness_launches()), launches)
        self.assertEqual(HarnessSession.live[before["agent_name"]].alive(), True)


class StatusTest(HarnessCase):
    def test_content_is_the_current_status_line(self):
        self.make().detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertEqual(view.content(), view.status().to_line())

    def test_tui_content_uses_the_current_result(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        view = HarnessDelegate.attach(record)
        agent = HarnessSession.live[record.sessions[1]]
        block = "```ensemble-outcome\n{}\n```"
        for result, given, expected in (
            (None, "agent content", "agent content"),
            (Result(0, None), "agent content", "agent content"),
            (None, f"Built X.\n{block}\nDone.", block),
            (Result(0, Outcome("done", "T", "S")), "agent content", None),
            (Result(1, None, error="boom"), "agent content", None),
        ):
            with self.subTest(result=result, given=given):
                if result is not None:
                    view._complete(result)
                with mock.patch.object(agent, "content", return_value=given) as content:
                    self.assertEqual(
                        view.content(),
                        expected if expected is not None else view.status().to_line(),
                    )
                    self.assertEqual(content.call_count, int(expected is not None))
                self.assertTrue(RecordingHost.processes["agent", record.sessions[0]].alive())

    def test_tui_content_without_its_agent_falls_back_to_status(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        HarnessSession.live[record.sessions[1]].stop()
        view = HarnessDelegate.attach(record)
        self.assertEqual(view.content(), view.status().to_line())

    def test_its_state_is_working_while_the_run_lives_else_gone(self):
        record = self.make().detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertEqual((view.status().state, view.status().hidden), ("working", False))
        self.stop(record)
        self.assertEqual((view.status().alive, view.status().state), (False, "gone"))

    def test_an_ended_tui_runs_state_is_its_agents_word_until_it_is_gone(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        view = HarnessDelegate.attach(record)
        view._complete(Result(0, Outcome("done", "T", "S")))
        RecordingHost.processes["agent", record.sessions[0]].close()
        agent = HarnessSession.live[record.sessions[1]]
        for word in ("done", "working", "blocked"):
            with (
                self.subTest(word=word),
                mock.patch.object(
                    agent, "status", return_value=replace(agent.status(), state=word)
                ),
            ):
                status = view.status()
                self.assertEqual(
                    (status.alive, status.state, status.outcome), (False, word, "done")
                )
                self.assertFalse(view.alive())
        agent.stop()
        self.assertEqual((view.status().alive, view.status().state), (False, "gone"))

    def test_a_tui_run_ended_without_an_outcome_shows_its_agents_word(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        view.close()
        HarnessSession.script = (Reply("No outcome block."), Exited(0))
        self.assertIsNone(view.run().outcome)
        status = view.status()
        self.assertEqual((status.alive, status.state, status.outcome), (False, "working", None))
        agent = HarnessSession.live[self.metadata()["agent_name"]]
        blocked = replace(agent.status(), state="blocked")
        with mock.patch.object(agent, "status", return_value=blocked):
            self.assertEqual(view.status().state, "blocked")


class UnansweredTest(HarnessCase):
    """An agent attach that did not answer is no proof its run ended."""

    def unanswered(self):
        return mock.patch.object(
            HarnessSession, "attach", side_effect=Unanswered("docker ps: timed out")
        )

    def gave_up(self):
        """A detached tui run whose harness ended without an outcome, its agent session left open."""
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        view.close()
        HarnessSession.script = (Reply("No outcome block."), Exited(0))
        self.assertIsNone(view.run().outcome)
        return view

    def test_a_live_run_whose_agent_did_not_answer_is_unknown_not_gone(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        view = HarnessDelegate.attach(record)
        with self.unanswered():
            status = view.status()
            self.assertEqual((status.alive, status.state), (True, "unknown"))
            self.assertEqual(view.content(), status.to_line())
        self.stop(record)
        with self.unanswered():
            self.assertEqual((view.status().alive, view.status().state), (False, "unknown"))

    def test_an_agent_confirmed_gone_is_gone(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        view = HarnessDelegate.attach(record)
        HarnessSession.live[record.sessions[1]].stop()
        self.assertEqual((view.status().alive, view.status().state), (False, "gone"))

    def test_a_start_waits_through_an_attach_that_did_not_answer(self):
        attach, calls = HarnessSession.attach, []

        def flaky(*args, **kw):
            calls.append(args)
            if len(calls) == 1:
                raise Unanswered("docker ps: timed out")
            return attach(*args, **kw)

        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        with mock.patch.object(HarnessSession, "attach", side_effect=flaky):
            record = self.make(spec=spec).detach()
        self.assertGreater(len(calls), 1)
        self.assertTrue(HarnessSession.live[record.sessions[1]].alive())

    def test_show_hide_and_attach_command_say_its_liveness_is_unknown(self):
        HarnessSession.reset(showable=True)
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        RecordingHost.processes["agent", record.sessions[0]].close()
        unknown = "^delegate agent session liveness unknown: docker ps: timed out$"
        with self.unanswered():
            for call in (view.show, view.hide, view.attach_command):
                with self.subTest(call=call.__name__), self.assertRaisesRegex(Unanswered, unknown):
                    call()
            self.assertEqual((view.status().state, view.status().attach), ("unknown", None))
        self.assertEqual((FakeViewer.shown, FakeViewer.hidden), ([], []))

    def test_hide_hides_the_harness_then_says_the_agents_liveness_is_unknown(self):
        HarnessSession.reset(showable=True)
        self.settings(harness_host="harness")
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        harness = self.metadata()["harness_name"]
        with self.unanswered():
            self.assertEqual(view.attach_command(), f"harness attach {harness}")
            self.assertEqual(view.status().attach, f"harness attach {harness}")
            with self.assertRaisesRegex(Unanswered, "^delegate agent session liveness unknown: "):
                view.hide()
        self.assertEqual(FakeViewer.hidden, [harness])

    def test_close_closes_the_harness_then_says_the_agent_was_not_checked(self):
        HarnessSession.reset(showable=True)
        self.settings(harness_host="harness")
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        harness, agent = record.sessions
        with self.unanswered(), self.assertRaises(Unanswered) as caught:
            view.close()
        self.assertEqual(
            str(caught.exception),
            f"close {agent}: agent session not checked: docker ps: timed out",
        )
        self.assertFalse(RecordingHost.processes["harness", harness].alive())
        self.assertEqual(FakeViewer.closed, [harness])
        self.assertTrue(HarnessSession.live[agent].alive())

    def test_a_failing_harness_step_still_names_the_unchecked_agent(self):
        HarnessSession.reset(showable=True)
        self.settings(harness_host="harness")
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        harness, agent = record.sessions
        unchecked = "docker ps: timed out"
        process = RecordingHost.processes["harness", harness]
        with (
            self.unanswered(),
            mock.patch.object(FakeViewer, "hide", side_effect=SessionError("hide refused")),
            self.assertRaises(SessionError) as caught,
        ):
            view.hide()
        self.assertEqual(
            str(caught.exception),
            f"hide refused; delegate agent session liveness unknown: {unchecked}",
        )
        with (
            self.unanswered(),
            mock.patch.object(process, "close", side_effect=SessionError("close refused")),
            self.assertRaises(SessionError) as caught,
        ):
            view.close()
        self.assertEqual(
            str(caught.exception),
            f"close {harness}: close refused; close {agent}: agent session not checked: {unchecked}",
        )

    def test_replace_resume_and_clean_refuse_while_the_agent_does_not_answer(self):
        view = self.gave_up()
        before = self.metadata()
        agent = HarnessSession.live[before["agent_name"]]
        refusal = f"^{self.work}: agent session liveness unknown: docker ps: timed out$"
        with self.unanswered():
            with self.assertRaisesRegex(ConfigError, refusal):
                self.make()
            with self.assertRaisesRegex(ConfigError, refusal):
                view.resume()
            with self.assertRaisesRegex(ConfigError, refusal):
                HarnessDelegate.clean(self.work, scratch=True)
        self.assertEqual(self.metadata(), before)
        self.assertTrue(agent.alive())


class ViewTest(HarnessCase):
    def setUp(self):
        super().setUp()
        HarnessSession.reset(showable=True)
        self.settings(harness_host="harness")

    def test_show_opens_only_the_agent_and_hide_closes_the_agent_and_the_harness(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        names = [self.metadata()[key] for key in ("agent_name", "harness_name")]
        view.show()
        self.assertEqual([name for name, _, _ in FakeViewer.shown], names[:1])
        self.assertFalse(view.status().hidden)
        self.assertIsNone(view.hide())
        self.assertEqual(FakeViewer.hidden, names)
        self.assertTrue(view.status().hidden)
        view.show()
        self.assertEqual([name for name, _, _ in FakeViewer.shown], names[:1] * 2)

    def test_show_without_a_live_agent_is_not_showable_and_opens_no_harness(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        HarnessSession.live[self.metadata()["agent_name"]].stop()
        with self.assertRaisesRegex(NotShowable, "delegate has no live showable agent session"):
            view.show()
        self.assertEqual(FakeViewer.shown, [])

    def test_close_closes_the_viewers_harness_view_only_on_an_attachable_host(self):
        for showable in (True, False):
            with self.subTest(showable=showable):
                HarnessSession.reset(showable=showable)
                spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
                self.make(spec=spec).detach()
                HarnessDelegate.attach(
                    self.work, session_cls=HarnessSession, viewer=FakeViewer()
                ).close()
                harness = self.metadata()["harness_name"]
                self.assertEqual(FakeViewer.closed, [harness] if showable else [])

    def test_hide_after_close_is_not_showable(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        view.close()
        with self.assertRaisesRegex(NotShowable, "delegate has no live showable session"):
            view.hide()
        self.assertEqual(FakeViewer.hidden, [])


class CrashTest(HarnessCase):
    def crashed(self, cwd=None):
        """A tui run whose process died mid-run: no outcome stored, no run lock held, its agent
        session left live as the run started it.
        """
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID, cwd=cwd)
        self.make(spec=spec)
        state = self.metadata()
        agent = SessionSpec(
            workdir=self.work,
            access=Access(cwd=self.work),
            client="fake",
            mode=Mode.TUI,
            sid=SID,
            name=state["agent_name"],
        )
        HarnessSession.start(agent, host=state["host"])
        return HarnessDelegate.attach(self.work, session_cls=HarnessSession), state["agent_name"]

    def test_a_tui_run_whose_process_died_is_not_alive(self):
        view, name = self.crashed()
        self.assertEqual(HarnessSession.live[name].alive(), True)
        self.assertFalse(view.alive())
        self.assertFalse(view.status().alive)

    def test_its_status_is_unknown_while_its_agent_sessions_host_cannot_tell(self):
        view, name = self.crashed()
        agent = HarnessSession.live[name]
        self.assertEqual((view.status().state, view.status().hidden), ("working", False))
        agent.liveness = Liveness.UNKNOWN
        self.assertEqual(view.status().state, "unknown")

    def test_resume_closes_the_left_agent_session_and_launches_the_next_attempt(self):
        view, name = self.crashed()
        left = HarnessSession.live[name]
        launches = len(harness_launches())
        record = view.resume()
        self.assertEqual(len(harness_launches()), launches + 1)
        self.assertEqual(left.alive(), False)
        self.assertEqual(record.sid, SID)
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_a_resume_after_its_agent_dir_was_pruned_makes_it_again(self):
        view, _name = self.crashed()
        shutil.rmtree(Layout(self.work).agent)
        view.resume()
        self.assertTrue(os.path.isdir(Layout(self.work).agent))
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_a_resume_refused_for_an_untrusted_cwd_leaves_the_agent_session_live(self):
        project = os.path.join(self.home, "project")
        os.mkdir(project)
        self.settings(trusted_dirs=(project,))
        _view, name = self.crashed(cwd=project)
        self.settings()
        left, launches = HarnessSession.live[name], len(harness_launches())
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        with self.assertRaisesRegex(ConfigError, "no longer in trusted_dirs"):
            view.resume()
        self.assertEqual(left.alive(), True)
        self.assertEqual(len(harness_launches()), launches)

    def test_a_resume_its_preflight_refuses_leaves_the_agent_session_live(self):
        _view, name = self.crashed()
        left, launches = HarnessSession.live[name], len(harness_launches())
        HarnessSession.preflight_error = SessionConfigError("fake: no client binary")
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        with self.assertRaisesRegex(ConfigError, "no client binary"):
            view.resume()
        self.assertEqual((left.alive(), len(harness_launches())), (True, launches))

    def test_resume_preflights_the_live_agents_name_under_a_probe_name(self):
        view, name = self.crashed()
        view.resume()
        self.assertNotEqual(HarnessSession.preflights[-1]["spec"].name, name)
        self.assertEqual(self.metadata()["plan"]["session"]["name"], name)

    def test_resume_refuses_a_run_replaced_since_its_attach(self):
        view, _name = self.crashed()
        other = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
        self.make(spec=Spec("input", self.work, task="echo", sid=other))
        launches = len(harness_launches())
        with self.assertRaisesRegex(ConfigError, f"run {SID} was replaced by {other}"):
            view.resume()
        self.assertEqual(
            (len(harness_launches()), self.metadata()["spec"]["sid"]), (launches, other)
        )

    def test_the_status_of_an_agent_session_another_process_started_comes_from_its_plan(self):
        view, _name = self.crashed()
        self.assertEqual(view.session_status(), HarnessSession.history)
        (inspected,) = HarnessSession.inspected
        self.assertEqual((inspected.sid, inspected.mode, inspected.client), (SID, "tui", "fake"))


class SweepTest(HarnessCase):
    """A callback group left by a dead harness (orphans.Orphan) is killed before anything starts."""

    def setUp(self):
        super().setUp()
        Clock(self)
        self.killpg = self.enterContext(mock.patch("os.killpg"))
        self.seen = []

    def orphan(self) -> Orphan:
        orphan = Orphan(self, RunState(Layout(self.work)).callback_lock, f"{ORPHAN}\n")

        def killpg(pid, signum):
            if signum == signal.SIGKILL:
                self.seen.append((self.metadata()["attempt"], len(harness_launches())))
            orphan.killpg(pid, signum)

        self.killpg.side_effect = killpg
        return orphan

    def record(self) -> bytes:
        with open(self.state_record(), "rb") as file:
            return file.read()

    def kills(self, orphan) -> list:
        return [call for call in orphan.calls if call[1] == signal.SIGKILL]

    def test_a_new_run_kills_the_left_group_before_its_record_replaces_the_old(self):
        self.make()
        orphan = self.orphan()
        self.make()
        self.assertEqual(self.kills(orphan), [(ORPHAN, signal.SIGKILL)])
        self.assertEqual(self.seen, [(1, 0)])
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_resume_kills_the_left_group_before_it_launches_a_harness(self):
        view = self.crashed()
        orphan = self.orphan()
        launches = len(harness_launches())
        view.resume()
        self.assertEqual(self.seen, [(1, launches)])
        self.assertEqual(len(harness_launches()), launches + 1)
        self.assertEqual(self.kills(orphan), [(ORPHAN, signal.SIGKILL)])

    def test_detach_kills_the_left_group_before_it_launches_a_harness(self):
        delegate = self.make()
        orphan = self.orphan()
        delegate.detach()
        self.assertEqual(self.seen, [(1, 0)])
        self.assertEqual(len(harness_launches()), 1)
        self.assertEqual(self.kills(orphan), [(ORPHAN, signal.SIGKILL)])

    def test_a_group_that_will_not_end_refuses_each_start_changing_nothing(self):
        view = self.crashed()
        orphan = self.orphan()
        orphan.dies = False
        before, launches = self.record(), len(harness_launches())
        left = HarnessSession.live[self.metadata()["agent_name"]]
        for start in (view.resume, HarnessDelegate.attach(self.work).detach, self.make):
            with self.subTest(start=start), self.assertRaisesRegex(ConfigError, "still exists"):
                start()
            self.assertEqual((self.record(), len(harness_launches())), (before, launches))
            if start == view.resume:
                self.assertEqual(left.alive(), True)

    def test_a_held_run_lock_refuses_a_new_run_though_its_record_says_completed(self):
        self.make().run()
        self.assertTrue(self.metadata()["completed"])
        self.orphan()
        before = self.record()
        with (
            RunState(Layout(self.work)).run_lock(),
            self.assertRaisesRegex(ConfigError, "delegate is still alive"),
        ):
            self.make()
        self.killpg.assert_not_called()
        self.assertEqual(self.record(), before)

    def crashed(self) -> HarnessDelegate:
        """A tui run whose harness died, its agent session left live."""
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec)
        state = self.metadata()
        agent = SessionSpec(
            workdir=self.work,
            access=Access(cwd=self.work),
            client="fake",
            mode=Mode.TUI,
            sid=SID,
            name=state["agent_name"],
        )
        HarnessSession.start(agent, host=state["host"])
        return HarnessDelegate.attach(self.work, session_cls=HarnessSession)
