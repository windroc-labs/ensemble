"""Session preflight shares startup validation without creating state, events or runtime work."""

import itertools
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ensemble.session import Access, ConfigError, Effort, Host, Mode, SessionError, Spec
from ensemble.session.engine.config import read_config
from ensemble.session.engine.types import Liveness
from ensemble.session.hosts.base import GUARD_WAIT
from ensemble.session.hosts.tmux import TmuxHost
from tests.session import harnesses
from tests.session.case import HOSTS, PY, REPO, SPAWNS, Case, child_env, no_spawn
from tests.session.fakes.preflight import PreflightClient
from tests.session.hosted_support import SID, fake_tmux
from tests.utils.sandbox import scratch


class Preflight(Case):
    def setUp(self):
        self.work = os.path.join(self.home, "absent-work")
        self.events = os.path.join(self.home, "absent-events")
        self.tmux = fake_tmux()
        self.configure()

    def configure(self, *, client=PreflightClient, tmux_settings=None, **settings):
        PreflightClient.prepared, PreflightClient.committed, PreflightClient.ran = [], [], []
        self.session = harnesses.Threaded.configured(
            clients={
                "fake": {
                    "implementation": f"{client.__module__}:{client.__qualname__}",
                    **settings,
                }
            },
            hosts=Host.parse_hosts(
                {"tmux": {**HOSTS["tmux"], **(tmux_settings or {})}, "default": "tmux"}
            ),
            host_factory=self._host_factory,
        )

    def _host_factory(self, kind, name, settings):
        return kind(name, settings, tmux=self.tmux)

    def spec(self, mode=Mode.HEADLESS, **changes):
        return Spec(
            **{
                "workdir": self.work,
                "access": Access(cwd=self.work),
                "client": "fake",
                "mode": mode,
                "prompt": "input",
                "sid": SID,
                **changes,
            }
        )

    def tree(self):
        """HOME's paths but the harness root a refused tui start leaves empty."""
        found = (
            os.path.relpath(os.path.join(root, name), self.home)
            for root, dirs, files in os.walk(self.home)
            for name in (*dirs, *files)
        )
        return sorted(path for path in found if path != os.path.join(".ensemble", "harness"))

    def no_client(self, *, harness=False):
        """No process spawned; nor a thread, unless `harness`: a tui start's own harness thread
        (tests/session/harnesses.py), whose checks refuse it.
        """
        return no_spawn(*(() if harness else ("threading.Thread.start",)))

    def unchanged(self, before):
        self.assertEqual(self.tree(), before)
        self.assertEqual((PreflightClient.committed, PreflightClient.ran), ([], []))
        self.tmux.checked.assert_not_called()

    def refusal(self, spec, message, **placement):
        before = self.tree()
        diagnostics = []
        for method in (self.session.preflight, self.session.start):
            harness = method == self.session.start and spec.mode == Mode.TUI
            with self.subTest(method=method.__name__):
                with self.no_client(harness=harness), self.assertRaises(ConfigError) as caught:
                    method(spec, **placement)
                self.assertIn(message, str(caught.exception))
                diagnostics.append(str(caught.exception))
                self.unchanged(before)
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(diagnostics[0], diagnostics[1])

    def test_missing_claude_binary_is_config_error_for_preflight_and_driver_error_for_start(self):
        clients = dict(read_config(local=os.devnull)["clients"])
        account = {"test": {"dir": os.path.join(self.home, ".claude")}}
        clients["claude"] = {**clients["claude"], "accounts": account}
        probe_bin = os.path.join(self.home, "probe-bin")
        os.mkdir(probe_bin)
        Path(probe_bin, "claude").touch(mode=0o755)
        os.makedirs(os.path.join(self.home, ".ensemble", "probe"), 0o700)
        path = os.pathsep.join((probe_bin, os.environ.get("PATH", os.defpath)))
        self.enterContext(mock.patch.dict(os.environ, {"PATH": path}))
        self.session = harnesses.Threaded.configured(
            clients=clients,
            hosts=Host.parse_hosts({"tmux": HOSTS["tmux"], "default": "tmux"}),
            host_factory=self._host_factory,
            probe=lambda *_, **__: "Total cost: $0.00\n",
        )
        for name in (None, "named-worker"):
            spec = Spec(
                workdir=self.work,
                access=Access(cwd=self.work),
                client="claude",
                mode=Mode.TUI,
                sid=SID,
                name=name,
                events=self.events,
                env={"PATH": os.path.join(self.home, "missing-bin")},
            )
            before = self.tree()
            with self.subTest(name=name):
                with self.no_client(), self.assertRaises(ConfigError) as refusal:
                    self.session.preflight(spec)
                self.assertEqual(str(refusal.exception), "command not found: claude")
                with self.no_client(harness=True), self.assertRaises(SessionError) as failure:
                    self.session.start(spec)
                session_name = name if name is not None else f"claude-{SID[:8]}"
                self.assertEqual(
                    str(failure.exception),
                    f"cannot start tui session {session_name}: command not found: claude",
                )
            self.unchanged(before)
            self.assertFalse(os.path.exists(self.events))

    def test_events_file_creation_failure_is_a_driver_error_before_any_session_state(self):
        message = f"events file {self.events}: Permission denied"
        spec = self.spec(Mode.TUI, name="event-worker", events=self.events)
        before = self.tree()
        real_open = os.open

        def refused(path, *args, **kw):
            if path == self.events:
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kw)

        with self.no_client(), mock.patch("os.open", side_effect=refused):
            self.assertIsNone(self.session.preflight(spec))
        with (
            self.no_client(harness=True),
            mock.patch("os.open", side_effect=refused),
            self.assertRaises(SessionError) as failure,
        ):
            self.session.start(spec)
        self.assertEqual(
            str(failure.exception), f"cannot start tui session event-worker: {message}"
        )
        self.unchanged(before)

    def test_all_modes_accept_fresh_and_resume_without_writing_or_committing(self):
        for mode, resume in itertools.product(Mode, (False, True)):
            with self.subTest(mode=mode, resume=resume):
                spec = self.spec(mode, resume=resume)
                before = self.tree()
                with self.no_client():
                    self.assertIsNone(self.session.preflight(spec))
                self.unchanged(before)
                self.assertEqual(PreflightClient.prepared[-1].sid, SID)
                self.assertEqual(PreflightClient.prepared[-1].resume, resume)

    def test_abc_forwards_preflight_to_the_configured_session_class(self):
        code = """import contextlib, json, sys
from unittest import mock
from ensemble.session import Access, Host, Mode, Session, Spec
work, sid, log, hosts, spawns = sys.argv[1:]
Session.configure(
    clients={"fake": {"implementation": "tests.session.fakes.preflight:PreflightClient", "log": log}},
    hosts=Host.parse_hosts({**json.loads(hosts), "default": "tmux"}),
)
spec = Spec(workdir=work, access=Access(cwd=work), client="fake", mode=Mode.HEADLESS,
            prompt="input", sid=sid)
with contextlib.ExitStack() as stack:
    for target in (*json.loads(spawns), "threading.Thread.start"):
        stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
    result = Session.preflight(spec)
print(json.dumps(result))
"""
        log = os.path.join(scratch(self, real=False), "preflight.jsonl")
        before = self.tree()
        child = subprocess.run(
            [*PY, "-c", code, self.work, SID, log, json.dumps(HOSTS), json.dumps(SPAWNS)],
            env=child_env(None, REPO),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        with open(log) as f:
            noted = [json.loads(line) for line in f]
        self.assertEqual(
            [
                json.loads(child.stdout),
                [value for kind, value in noted if kind == "prepared"][-1],
                [value for kind, value in noted if kind == "committed"],
                [value for kind, value in noted if kind == "ran"],
            ],
            [None, SID, [], []],
        )
        self.unchanged(before)

    def test_missing_sid_can_be_prepared_without_mutating_the_callers_spec(self):
        spec = replace(self.spec(), sid=None)
        before = self.tree()
        with self.no_client():
            self.session.preflight(spec)
        self.assertIsNone(spec.sid)
        self.assertTrue(PreflightClient.prepared[-1].sid)
        self.unchanged(before)

    def test_missing_client_mode_unknown_client_and_host_have_startup_parity(self):
        for spec, message, placement in (
            (replace(self.spec(), client=None), "spec.client", {}),
            (replace(self.spec(), mode=None), "spec.mode", {}),
            (replace(self.spec(), client="absent"), "clients.absent", {}),
            (self.spec(), "unknown host absent", {"host": "absent"}),
        ):
            with self.subTest(spec=spec, placement=placement):
                self.refusal(spec, message, **placement)

    def test_unsupported_modes_and_selection_flags_are_refused_before_state(self):
        for mode in Mode:
            with self.subTest(mode=mode):
                self.configure(unsupported=mode.value)
                self.refusal(self.spec(mode), f"does not support {mode.value} mode")
                self.configure()
                self.refusal(self.spec(mode, flags=("--resume",)), "picks the session")

    def test_non_tui_placement_and_invalid_names_are_refused_without_events_file(self):
        for mode in (Mode.HEADLESS, Mode.IN_PROCESS):
            for spec, placement, message in (
                *(
                    ((self.spec(mode, name="window"), {}, "name: tui mode only"),)
                    if mode == Mode.IN_PROCESS
                    else ()
                ),
                (self.spec(mode, events=self.events), {}, "events: tui mode only"),
                (self.spec(mode), {"display": "opaque"}, "display: tui mode only"),
            ):
                with self.subTest(mode=mode, message=message):
                    self.refusal(spec, message, **placement)
        self.refusal(self.spec(Mode.TUI, name="bad name", events=self.events), "name must match")
        self.refusal(self.spec(Mode.HEADLESS, name="bad name"), "name must match")

    def test_idle_display_client_launch_and_in_process_checks_have_startup_parity(self):
        for timeout in (0, -1, float("nan"), True, "1"):
            with self.subTest(timeout=timeout):
                self.refusal(self.spec(), "idle_timeout", idle_timeout=timeout)
        self.refusal(self.spec(Mode.TUI), "display", display="bad\nvalue")
        self.configure(empty_argv=True)
        for mode in (Mode.HEADLESS, Mode.TUI):
            with self.subTest(mode=mode):
                self.refusal(self.spec(mode), "gave no command")
        self.configure(bad_env=True)
        for mode in (Mode.HEADLESS, Mode.TUI):
            with self.subTest(mode=mode):
                self.refusal(self.spec(mode), "launch: env")
        self.configure()
        self.refusal(self.spec(Mode.IN_PROCESS, tier=4), "tier 4 is unmapped")

    def test_cwd_grants_and_client_dirs_over_state_and_hook_paths_preflight_in_all_modes(self):
        hook_root = os.path.join(self.ensemble, "tui")
        alias = os.path.join(self.home, "alias")
        os.symlink(self.work, alias)
        for mode, resume, path, field in itertools.product(
            Mode,
            (False, True),
            (self.work, alias, hook_root, self.home),
            ("cwd", "dirs", "client"),
        ):
            with self.subTest(mode=mode, resume=resume, path=path, field=field):
                self.configure(writable=(path,) if field == "client" else ())
                access = (
                    Access(cwd=path)
                    if field == "cwd"
                    else Access(cwd=self.work, dirs=(path,) if field == "dirs" else ())
                )
                before = self.tree()
                with self.no_client():
                    self.session.preflight(self.spec(mode, resume=resume, access=access))
                self.unchanged(before)

    def test_read_only_grants_and_both_environment_sources_are_preserved(self):
        protected = os.path.join(self.home, "read-only")
        access = Access(
            cwd=self.work, reads=(protected,), env={"FROM_ACCESS": "kept", "SHARED": "access"}
        )
        for mode in Mode:
            with self.subTest(mode=mode):
                spec = self.spec(
                    mode,
                    access=access,
                    env={"FROM_SPEC": "kept", "SHARED": "spec"},
                )
                before = self.tree()
                with self.no_client():
                    self.session.preflight(spec)
                prepared = PreflightClient.prepared[-1]
                self.assertEqual(prepared.access.reads, (protected,))
                self.assertEqual(
                    dict(prepared.access.env), {"FROM_ACCESS": "kept", "SHARED": "access"}
                )
                self.assertEqual(dict(prepared.env), {"FROM_SPEC": "kept", "SHARED": "spec"})
                self.unchanged(before)

    def test_tui_preflight_validates_events_without_creating_the_file(self):
        before = self.tree()
        with self.no_client():
            self.session.preflight(self.spec(Mode.TUI, events=self.events))
        self.unchanged(before)
        self.refusal(self.spec(Mode.TUI, events=self.events + "#bad"), "tmux would misread")
        self.refusal(
            self.spec(Mode.TUI, events=self.events, flags=("--resume",)), "picks the session"
        )

    def test_tui_preflight_refuses_a_live_same_named_worker_and_passes_an_ended_one(self):
        liveness = [Liveness.LIVE]

        class Workers(TmuxHost):
            def worker_liveness(self, name, generation):
                return liveness[0]

        self.session = harnesses.Threaded.configured(
            clients={"fake": {"implementation": "tests.session.fakes.preflight:PreflightClient"}},
            hosts=Host.parse_hosts({**HOSTS, "default": "tmux"}),
            host_factory=lambda _kind, name, settings: Workers(name, settings, tmux=self.tmux),
        )
        owner = os.path.join(self.ensemble, "harness", "a" * 32)
        os.makedirs(owner, 0o700)
        worker = os.path.join(self.ensemble, "tui", "worker")
        os.makedirs(worker, 0o700)
        with open(os.path.join(worker, "session.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "client": "fake",
                    "sid": SID,
                    "workdir": self.work,
                    "cwd": self.work,
                    "project": False,
                    "transcript": "",
                    "resume": "",
                    "events": None,
                    "words": {},
                    "generation": "b" * 32,
                    "host": "tmux",
                    "display": None,
                    "owner": owner,
                },
                f,
            )
        spec = self.spec(Mode.TUI, name="worker", events=self.events)
        before = self.tree()
        with (
            self.no_client(),
            mock.patch("time.monotonic", side_effect=itertools.count(0, GUARD_WAIT)),
            self.assertRaises(ConfigError) as refusal,
        ):  # LIVE past the guard's reaping wait
            self.session.preflight(spec)
        self.assertEqual(str(refusal.exception), "tui session worker is running")
        self.unchanged(before)
        liveness[0] = Liveness.ABSENT
        with self.no_client():
            self.assertIsNone(self.session.preflight(spec))
        self.unchanged(before)
        self.assertEqual(self.tmux.mock_calls, [])

    def test_tui_settings_are_validated_before_state(self):
        self.configure(tmux_settings={"status_line": "invalid"})
        self.refusal(self.spec(Mode.TUI), "status_line: want true or false")

    def test_codex_oversized_recovery_state_is_refused_before_any_directory_is_created(self):
        self.home = scratch(self, os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        self.work = os.path.join(self.home, "absent-work")
        settings = read_config(local=os.devnull)
        self.session = harnesses.Threaded.configured(
            clients=settings["clients"], hosts=Host.parse_hosts(settings["hosts"])
        )
        spec = Spec(
            workdir=self.work,
            access=Access(cwd=self.work),
            client="codex",
            mode=Mode.HEADLESS,
            sid=SID,
            tier=2,
            effort=Effort.HIGH,
            env={"LARGE": "x" * 70000},
        )
        lexists = os.path.lexists
        with (
            mock.patch.dict(
                os.environ, {"HOME": self.home, "CODEX_HOME": os.path.join(self.home, ".codex")}
            ),
            mock.patch(
                "os.path.lexists",
                side_effect=lambda path: (
                    os.path.commonpath((os.path.abspath(path), self.home)) == self.home
                    and lexists(path)
                ),
            ),
        ):
            self.refusal(spec, "recovery state is too large")
