"""Session behavior tests with caller-owned dependencies."""

import concurrent.futures
import contextlib
import dataclasses
import functools
import json
import os
import select as select_module
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from ensemble.session.clients.codex import tui
from ensemble.session.clients.codex.common import SessionStore
from ensemble.session.clients.codex.tui import start as tui_start
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ConfigError,
    Exited,
    HookCallback,
    LimitHit,
    Mode,
    Spec,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from tests.session import live_tmux, tui_support
from tests.session.case import PLUGIN_BIN, PY, REPO, ROOTS, Case, configured
from tests.session.integration.clients.codex import fake_codex, fake_tui
from tests.session.unit.clients.codex.support import CHILD, NATIVE, Base
from tests.utils import sandbox
from tests.utils.jsonl import read_jsonl
from tests.utils.sandbox import running, scratch

WAIT = 15

CHANGED = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

ATTACH = """import json, sys
from ensemble.session import Host, Session, TurnEnd
Session.configure(clients=json.loads(sys.argv[1]), hosts=Host.parse_hosts({"default": "tmux", "tmux": {"implementation": "ensemble.session.hosts.tmux:TmuxHost"}}))
h = Session.attach(sys.argv[2])
before = [h.sid, h.alive(), h.reply]
if sys.argv[3]:
    h.send(sys.argv[3])
    turn = next(e for e in h.events() if isinstance(e, TurnEnd))
    print(json.dumps([before, [turn.reply, turn.pending], h.reply]))
else:
    print(json.dumps(before))
"""


class Stream(tui_support.Stream):
    def __init__(self, case, handle):
        super().__init__(case, handle, poke=lambda: case.server.tmux("run-shell", "-b", "true"))


class Live(Case):
    def setUp(self):
        self.root = Path(scratch(self, os.environ.get("ENSEMBLE_TEST_TMP", REPO)))
        self.home, self.work, self.extra = (self.root / n for n in ("home", "work", "extra"))
        self.cwd, self.codex_home = self.work / "project", self.home / ".codex"
        for path in (self.home, self.cwd, self.codex_home, self.extra):
            path.mkdir(parents=True, exist_ok=True)
        self.server = live_tmux.Server(self, str(self.home), bin_dir=str(self.root / "bin"))
        self.server.start("mgr")
        fake_tui.install(self.server.bin)
        self.log, self.scenario, self.events = (
            self.root / n for n in ("client.jsonl", "scenario.json", "events")
        )
        self.scene()
        self.env = self.server.env(CODEX_HOME=str(self.codex_home), TZ="UTC")
        self.enterContext(mock.patch.dict(os.environ, self.env, clear=True))
        self.clients = {
            "codex": {
                "implementation": "ensemble.session.clients.codex:CodexClient",
                "tiers": {"1": "gpt-test"},
                "efforts": {"low": "low"},
                "env": {
                    "FAKE_LOG": str(self.log),
                    "FAKE_SCENARIO": str(self.scenario),
                    "TZ": "UTC",
                },
            }
        }
        self.session_cls = configured(self.clients)
        self.common = self.session_cls._config().registry.client("codex").common
        self.spec = Spec(
            flags=(
                "-s",
                "danger-full-access",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
            ),
            workdir=str(self.work),
            tier=1,
            effort="low",
            access=Access(
                cwd=str(self.cwd), project=True, dirs=(str(self.extra),), commands=("git status",)
            ),
        )
        self.sid = "caller-id"

    def scene(self, turns=None, **fields):
        self.scenario.write_text(
            json.dumps({"tui_turns": [["first"]] if turns is None else turns, **fields})
        )

    def start(self, name="w1", prompt="hello", *, spec=None, sid=None, events=None, **fields):
        handle = self.session_cls.start(
            dataclasses.replace(
                spec or self.spec,
                name=name,
                sid=self.sid if sid is None else sid,
                client="codex",
                mode=Mode.TUI,
                prompt=prompt,
                events=str(self.events) if events is None else events,
                **fields,
            )
        )
        self.addCleanup(handle.stop)
        return handle

    def logged(self):
        return read_jsonl(self.log, missing_ok=True)

    def calls(self, mode="tui"):
        return [row for row in self.logged() if row.get("mode") == mode]

    def hooks(self):
        return [row for row in self.logged() if "hook" in row]

    def lines(self, count):
        def check():
            self.server.tmux("run-shell", "-b", "true")
            rows = live_tmux.events(str(self.events))
            return rows if len(rows) >= count else None

        return [
            line.split(" ", 1)[1]
            for line in live_tmux.wait(check, WAIT, what=f"{count} event lines")
        ]

    def bound(self, sid=None):
        return self.common.load(str(self.work), sid or self.sid)

    def record(self):
        return next(Path(SessionStore().root).glob("*.json"))

    def client_ids(self):
        log = RunLog(str(self.work)).read()
        return [line["client_id"] for line in log if line["kind"] == "client_session"]

    def attach(self, text="", name="w1"):
        result = subprocess.run(
            [*PY, "-c", ATTACH, json.dumps(self.clients), name, text],
            env=self.server.child_env(),
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=WAIT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def headless(self, *, resume=False):
        handle = self.session_cls.start(
            dataclasses.replace(
                self.spec,
                sid=self.sid,
                client="codex",
                mode=Mode.HEADLESS,
                prompt="headless",
                resume=resume,
            )
        )
        self.addCleanup(handle.stop)
        stream = Stream(self, handle)
        return stream.until(Exited)


class HookTrustIntegrationTest(Live):
    def test_interleaved_catalog_notifications_exact_hashes_and_cleanup(self):
        self.scene([["trusted"]])
        handle = self.start()
        self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("trusted"))
        (probe,) = self.calls("app-server")
        self.assertFalse(self.pid_exists(probe["pid"]))
        received = [r["received"] for r in self.logged() if "received" in r]
        self.assertEqual(
            [r["method"] for r in received], ["initialize", "initialized", "hooks/list"]
        )
        self.assertEqual(received[-1]["params"], {"cwds": [str(self.cwd)]})
        self.assertEqual(
            received[0],
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "ensemble", "version": "0"},
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        rows = next(r["catalog"] for r in self.logged() if "catalog" in r)
        argv = self.calls()[0]["argv"]
        config = fake_tui.configure([argv[i + 1] for i, word in enumerate(argv) if word == "-c"])
        self.assertEqual(
            config["hooks"]["state"],
            {row["key"]: {"trusted_hash": row["currentHash"]} for row in rows},
        )
        self.assertEqual(
            [row["eventName"] for row in rows],
            [
                "sessionStart",
                "userPromptSubmit",
                "postToolUse",
                "permissionRequest",
                "preToolUse",
                "stop",
                "interrupt",
            ],
        )
        self.assertEqual([row["timeoutSec"] for row in rows], [10, 10, 10, 10, 10, 10, 3])
        self.assertEqual(
            [row["matcher"] for row in rows], [None] * 4 + ["^request_user_input$", None, None]
        )
        self.assertFalse((self.codex_home / "config.toml").exists())

    pid_exists = staticmethod(sandbox.pid_exists)

    def test_disabled_unknown_malformed_and_mismatched_catalogs_never_start_interactive(self):
        cases = (
            "off",
            "disabled",
            "foreign",
            "missing",
            "duplicate",
            "command",
            "hash",
            "malformed",
            "errors",
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                self.scene(catalog_case=case)
                handle = self.start(
                    f"bad{index}", sid=f"bad-{index}", events=str(self.root / f"events-{index}")
                )
                stream = Stream(self, handle)
                self.server.tmux("run-shell", "-b", "true")
                live_tmux.wait(
                    lambda *, index=index: (
                        "codex tui:"
                        in self.server.tmux(
                            "capture-pane", "-p", "-J", "-S", "-1000", "-t", f"=bad{index}:"
                        ).stdout
                    ),
                    WAIT,
                    what="preflight diagnostic",
                )
                self.assertEqual(stream.until(Exited), [Exited(1)])
                self.assertFalse(any(row.get("mode") == "tui" for row in self.logged()))
                self.assertTrue(
                    all(not self.pid_exists(row["pid"]) for row in self.calls("app-server"))
                )
                self.assertIsNone(self.bound(f"bad-{index}").native_id)
                handle.stop()

    def test_untrusted_modified_user_hooks_fail_and_trusted_disabled_managed_are_allowed(self):
        for index, status in enumerate(("untrusted", "modified", "trusted", "disabled", "managed")):
            with self.subTest(status=status):
                self.scene(user_hook=status)
                before = len(self.calls())
                handle = self.start(f"u{index}", sid=f"user-{index}")
                stream = Stream(self, handle)
                if status in ("untrusted", "modified"):
                    pane = functools.partial(
                        self.server.tmux,
                        "capture-pane",
                        "-p",
                        "-J",
                        "-S",
                        "-1000",
                        "-t",
                        f"=u{index}:",
                    )
                    try:
                        live_tmux.wait(
                            lambda pane=pane: "review it" in pane().stdout,
                            WAIT,
                            what="user hook trust diagnostic",
                        )
                    except AssertionError as timeout:
                        shown = pane()
                        raise AssertionError(
                            f"{timeout}; pane: {shown.stdout or shown.stderr!r}; "
                            f"fake codex log tail: {self.logged()[-20:]}"
                        ) from None
                    self.assertEqual(stream.until(Exited), [Exited(1)])
                    self.assertEqual(len(self.calls()), before)
                else:
                    self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("first"))
                    self.assertEqual(len(self.calls()), before + 1)
                    if status != "disabled":
                        live_tmux.wait(
                            lambda: any(h.get("source") == "user" for h in self.hooks()),
                            WAIT,
                            what="trusted user hook result",
                        )
                        self.assertTrue(
                            all(h["exit"] == 0 for h in self.hooks() if h.get("source") == "user")
                        )
                handle.stop()
        self.assertFalse((self.codex_home / "hooks.json").exists())

    def test_appserver_exit_is_retried_once_then_cleaned_up(self):
        self.scene(catalog_case="exit")
        handle = self.start()
        live_tmux.wait(
            lambda: (
                "app-server exited"
                in self.server.tmux("capture-pane", "-p", "-J", "-S", "-1000", "-t", "=w1:").stdout
            ),
            WAIT,
            what="bounded retry diagnostic",
        )
        self.assertEqual(Stream(self, handle).until(Exited), [Exited(1)])
        probes = self.calls("app-server")
        self.assertEqual(len(probes), 2)
        self.assertTrue(all(not self.pid_exists(row["pid"]) for row in probes))
        self.assertEqual(self.calls(), [])

    def test_concurrent_independent_starts_serialize_the_same_home_probe(self):
        self.scene(catalog_delay=0.1)

        def start(index):
            return self.start(
                f"parallel{index}",
                sid=f"parallel-{index}",
                events=str(self.root / f"parallel-{index}.events"),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            handles = list(pool.map(start, (0, 1)))
        for handle in handles:
            self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
        logs = self.logged()
        positions = [i for i, row in enumerate(logs) if row.get("mode") == "app-server"]
        first, second = (logs[i]["pid"] for i in positions)
        closed = next(i for i, row in enumerate(logs) if row.get("probe_closed") == first)
        self.assertLess(positions[0], closed)
        self.assertLess(closed, positions[1])
        self.assertNotEqual(self.bound("parallel-0").native_id, self.bound("parallel-1").native_id)
        self.assertFalse(self.pid_exists(first))
        self.assertFalse(self.pid_exists(second))

    def test_silent_appserver_times_out_and_kills_group_with_a_fake_clock(self):
        self.scene(catalog_case="silent")
        run = self.session_cls._config().registry.client("codex").tui()
        launch = run.launch(
            dataclasses.replace(self.spec, sid=self.sid, prompt="hello"),
            (os.path.join(PLUGIN_BIN, "ensemble-session"), "hook", "/protected/session"),
        )
        data = json.loads(launch.argv[-1])
        env = {**dict(os.environ), **launch.env}
        clock, select, wait = (
            time.monotonic,
            select_module.select,
            subprocess.Popen.wait,
        )
        started = clock()
        with (
            mock.patch.object(
                tui_start.time,
                "monotonic",
                side_effect=lambda: (
                    clock() * 100 if self.log.exists() and self.log.stat().st_size else clock()
                ),
            ),
            mock.patch.object(
                select_module,
                "select",
                side_effect=lambda r, w, e, t: select(r, w, e, min(t, 0.01)),
            ),
            mock.patch.object(
                subprocess.Popen,
                "wait",
                lambda proc, timeout=None: wait(
                    proc, timeout=min(timeout, 0.01) if timeout is not None else None
                ),
            ),
            self.assertRaisesRegex(tui_start.StartError, "no answer"),
        ):
            tui_start.hooks_list(
                data["executable"],
                tui.trust_args(tui.decode(data), run.state.snapshot),
                str(self.cwd),
                env,
            )
        self.assertLess(clock() - started, 3)
        (probe,) = self.calls("app-server")
        self.assertFalse(self.pid_exists(probe["pid"]))
        self.assertEqual(self.calls(), [])

    def test_stop_and_sigterm_during_preflight_kill_probe_and_owned_descendants(self):
        def cleanup(pid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)

        for index, action in enumerate(("stop", "term")):
            with self.subTest(action=action):
                before = len(self.calls("app-server"))
                descendants_before = len([r for r in self.logged() if "descendant" in r])
                self.scene(catalog_case="silent_tree")
                handle = self.start(f"interrupted{index}", sid=f"interrupted-{index}")
                probe = live_tmux.wait(
                    lambda *, before=before: self.calls("app-server")[before:] or None,
                    WAIT,
                    what="silent preflight",
                )[0]
                self.addCleanup(cleanup, probe["pid"])
                descendants = live_tmux.wait(
                    lambda *, descendants_before=descendants_before: (
                        [r["descendant"] for r in self.logged() if "descendant" in r][
                            descendants_before:
                        ]
                        if len([r for r in self.logged() if "descendant" in r])
                        >= descendants_before + 2
                        else None
                    ),
                    WAIT,
                    what="owned descendants",
                )
                if action == "stop":
                    handle.stop()
                else:
                    os.kill(probe["parent_pid"], signal.SIGTERM)
                live_tmux.wait(
                    lambda *, descendants=descendants, probe=probe: (
                        not self.pid_exists(probe["pid"])
                        and all(not running(pid) for pid in descendants)
                    ),
                    4,
                    what="interrupted preflight cleanup",
                )
                self.assertEqual(len(self.calls("app-server")), before + 1)
                self.assertEqual(self.calls(), [])


class TuiLaunchIntegrationTest(Live):
    def test_start_permission_reply_send_and_separate_process_attach(self):
        raw = "[ensemble-progress:start] raw reply\n\nfinal"
        self.scene([[{"kind": "permission"}, raw], ["second"], ["third"]])
        handle = self.start(prompt="quoted ' prompt\nnext")
        stream = Stream(self, handle)
        found = stream.until(TurnEnd)
        self.assertEqual(found, [Activity(), TurnStarted(), Blocked("Bash"), TurnEnd(raw)])
        self.assertEqual(self.lines(2), ["w1 blocked", "w1 done"])
        self.assertEqual(handle.reply, raw)
        self.assertEqual(len(self.server.panes("=mgr:")), 1)
        self.assertEqual([r.name for r in self.session_cls.list("w1")], ["w1"])
        self.assertEqual(self.server.option("w1", "@sid"), self.sid)
        self.assertEqual(stat.S_IMODE(self.record().stat().st_mode), 0o600)
        hook_store = self.home / ".ensemble" / "tui" / "w1" / "hooks.jsonl"
        self.assertEqual(stat.S_IMODE(hook_store.stat().st_mode), 0o600)
        handle.send("more")
        self.assertEqual(stream.until(TurnEnd), [TurnStarted(), TurnEnd("second")])
        self.assertEqual(self.attach("last"), [[self.sid, True, "second"], ["third", 0], "third"])
        self.assertEqual(stream.until(TurnEnd), [TurnStarted(), TurnEnd("third")])
        self.assertEqual(self.lines(4), ["w1 blocked", "w1 done", "w1 done", "w1 done"])
        live_tmux.wait(lambda: len(self.hooks()) == 8, WAIT, what="all hook results logged")
        (call,) = self.calls()
        self.assertEqual(
            call["argv"][:5],
            ["--no-daemon", "-C", str(self.cwd), "-c", 'tui.notification_method="bel"'],
        )
        self.assertEqual(
            call["argv"][5:11],
            [
                "-s",
                "danger-full-access",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
            ],
        )
        self.assertIn("--add-dir", call["argv"])
        self.assertFalse(any(word.startswith("sandbox_workspace_write.") for word in call["argv"]))
        self.assertEqual(
            call["argv"][-2:],
            [
                "--",
                "quoted ' prompt\nnext\n\n"
                + self.session_cls._config().registry.client("codex").client_note,
            ],
        )
        self.assertEqual(
            [h["hook"] for h in self.hooks()],
            [
                "SessionStart",
                "UserPromptSubmit",
                "PermissionRequest",
                "Stop",
                "UserPromptSubmit",
                "Stop",
                "UserPromptSubmit",
                "Stop",
            ],
        )
        self.assertTrue(
            all((h["exit"], h["stdout"], h["cwd"]) == (0, "", str(self.cwd)) for h in self.hooks())
        )
        self.assertEqual(
            [h["payload"]["prompt"] for h in self.hooks() if h["hook"] == "UserPromptSubmit"],
            [call["argv"][-1], "more", "last"],
        )
        kinds = [r["kind"] for r in RunLog(str(self.work)).read()]
        self.assertEqual(
            [kind for kind in kinds if kind != "session_metadata"], ["session", "client_session"]
        )
        self.assertTrue(handle.info().transcript)
        self.assertIn(self.bound().native_id, handle.info().resume)
        handle.stop()
        self.assertEqual(stream.until(Exited), [Exited(-signal.SIGHUP)])
        self.assertEqual(self.session_cls.list("w1"), [])

    def test_a_plan_mode_question_blocks_until_answered(self):
        self.scene(
            [[{"kind": "question"}, {"kind": "tool", "tool_name": "request_user_input"}, "ok"]]
        )
        handle = self.start()
        found = Stream(self, handle).until(TurnEnd)
        self.assertEqual(
            found,
            [
                Activity(),
                TurnStarted(),
                Blocked("question: Scope"),
                Activity("request_user_input"),
                TurnEnd("ok"),
            ],
        )
        self.assertEqual(self.lines(2), ["w1 blocked", "w1 done"])

    def test_no_opening_input(self):
        handle = self.start()
        stream = Stream(self, handle)
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("first"))
        live_tmux.wait(lambda: len(self.hooks()) == 3, WAIT, what="the turn's hook results")
        stream.quiet(0.4)
        self.assertEqual(
            [h["hook"] for h in self.hooks()], ["SessionStart", "UserPromptSubmit", "Stop"]
        )

    def test_workspace_write_flags_and_saved_policy_through_the_session(self):
        self.spec = dataclasses.replace(
            self.spec,
            flags=(
                "-s",
                "workspace-write",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
                "-c",
                "sandbox_workspace_write.network_access=false",
                "-c",
                "sandbox_workspace_write.writable_roots=[]",
            ),
        )
        self.session_cls = configured(self.clients)
        handle = self.start()
        Stream(self, handle).until(TurnEnd)
        native = self.bound().native_id
        handle.stop()
        self.spec = dataclasses.replace(self.spec, flags=("-s", "danger-full-access"))
        self.session_cls = configured(self.clients)
        resumed = self.start(resume=True)
        Stream(self, resumed).until(TurnEnd)
        first, second = self.calls()
        expected = [
            "-s",
            "workspace-write",
            "-c",
            'approval_policy="on-request"',
            "-c",
            'approvals_reviewer="auto_review"',
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-c",
            "sandbox_workspace_write.writable_roots=[]",
            "-m",
            "gpt-test",
            "-c",
            "model_reasoning_effort=low",
            "--add-dir",
            str(self.extra),
        ]
        self.assertEqual(first["argv"][5 : 5 + len(expected)], expected)
        self.assertEqual(second["argv"], ["resume", native, *first["argv"]])
        self.assertEqual(self.bound().snapshot.flags[1], "workspace-write")

    def test_session_start_waits_for_first_submission_fresh_and_resumed(self):
        spec = dataclasses.replace(
            self.spec, access=dataclasses.replace(self.spec.access, commands=())
        )
        first = self.start(prompt="", spec=spec)
        live_tmux.wait(lambda: len(self.calls()) == 1, WAIT, what="interactive startup waits")
        time.sleep(0.3)
        self.assertEqual(self.hooks(), [])
        first.send("first")
        self.assertEqual(Stream(self, first).until(TurnEnd)[-1], TurnEnd("first"))
        first.stop()
        hooks = len(self.hooks())
        spec = dataclasses.replace(
            self.spec, access=dataclasses.replace(self.spec.access, commands=())
        )
        resumed = self.start(prompt="", resume=True, spec=spec)
        live_tmux.wait(lambda: len(self.calls()) == 2, WAIT, what="interactive resume starts")
        time.sleep(0.3)
        self.assertEqual(len(self.hooks()), hooks)
        resumed.send("again")
        self.assertEqual(Stream(self, resumed).until(TurnEnd)[-1], TurnEnd("first"))
        self.assertEqual(self.hooks()[hooks]["payload"]["source"], "resume")

    def test_parent_markers_and_injected_defaults_are_removed_fresh_resumed_and_nested(self):
        inherited = {**dict.fromkeys(fake_codex.MARKERS, "inherited"), **fake_codex.DEFAULTS}
        self.assert_nested_environment(inherited, dict.fromkeys(inherited))

    def test_external_environment_is_inherited_fresh_resumed_and_nested(self):
        markers = dict.fromkeys(fake_codex.MARKERS, "inherited")
        external = {
            **dict.fromkeys(fake_codex.DEFAULTS, "less"),
            "NO_COLOR": "0",
            "CODEX_CI": "operator",
        }
        self.assert_nested_environment(
            {**markers, **external}, {**dict.fromkeys(markers), **external}
        )

    def test_explicit_operator_environment_is_forwarded_and_not_written_to_config(self):
        cfg = self.clients["codex"]
        cfg["env"].update(
            NO_COLOR="0", CODEX_CI="operator", PAGER="less", FAKE_ADDITION="configured"
        )
        self.session_cls = configured(self.clients)
        handle = self.start()
        self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
        env = self.calls()[0]["env"]
        self.assertEqual(
            {key: env[key] for key in ("NO_COLOR", "CODEX_CI", "PAGER", "FAKE_ADDITION")},
            {
                "NO_COLOR": "0",
                "CODEX_CI": "operator",
                "PAGER": "less",
                "FAKE_ADDITION": "configured",
            },
        )
        self.assertFalse((self.codex_home / "config.toml").exists())
        self.assertFalse((self.codex_home / "hooks.json").exists())

    def test_explicit_no_color_controls_interactive_rendering(self):
        for index, value in enumerate((None, "", "1", "0")):
            with self.subTest(value=value):
                env = self.clients["codex"]["env"]
                env.pop("NO_COLOR", None)
                if value is not None:
                    env["NO_COLOR"] = value
                self.session_cls = configured(self.clients)
                handle = self.start(f"color{index}", sid=f"color-{index}")
                self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
                screen = self.server.tmux(
                    "capture-pane", "-p", "-e", "-t", f"=color{index}:"
                ).stdout
                self.assertEqual("\x1b[36mfirst" in screen, value in (None, "0"))
                self.assertEqual(self.calls()[-1]["env"]["NO_COLOR"], value)
                handle.stop()

    def test_startup_dialog_screens_are_blocked_events(self):
        screens = {
            "Trust this folder?": "Trust this folder? Codex can read files.\n\n› 1. Trust and continue",
            "Hooks need review": "Hooks need review\n1 hook is new or changed.\n\n› 1. Review hooks",
        }
        for index, (dialog, screen) in enumerate(screens.items()):
            with self.subTest(dialog=dialog):
                release = self.root / f"release-{index}"
                self.scene(
                    [[{"kind": "screen", "text": screen}, {"kind": "pause", "file": str(release)}]]
                )
                handle = self.start(f"w{index}", sid=f"dialog-{index}")
                found = Stream(self, handle).until(Blocked)
                self.assertEqual(found[-1], Blocked(dialog))
                handle.stop()
        self.assertEqual(self.lines(2), ["w0 blocked", "w1 blocked"])

    def test_realistic_usage_limit_screen_emits_reset_and_deduplicates(self):
        release = self.root / "release"
        reset = datetime(2027, 1, 2, 3, 4, tzinfo=ZoneInfo("UTC")).timestamp()
        screen = "Codex > gpt-test\n■ You've hit your usage limit. Try again at Jan 2nd, 2027 3:04 AM.\n› "
        self.scene([[{"kind": "screen", "text": screen}, {"kind": "pause", "file": str(release)}]])
        handle = self.start()
        stream = Stream(self, handle)
        self.assertEqual(stream.until(LimitHit)[-1], LimitHit(reset))
        self.assertIn("You've hit your usage limit", handle.screen())
        stream.quiet(0.4)
        handle.stop()

    def test_configured_home_keeps_caller_store_for_stop_binding_and_resume(self):
        client_home = self.root / "client-home"
        client_home.mkdir()
        self.clients["codex"]["env"]["HOME"] = str(client_home)
        self.session_cls = configured(self.clients)
        self.scene([["configured home"]])
        first = self.start()
        self.assertEqual(Stream(self, first).until(TurnEnd)[-1], TurnEnd("configured home"))
        native = self.bound().native_id
        first.stop()
        second = self.start(resume=True)
        self.assertEqual(Stream(self, second).until(TurnEnd)[-1], TurnEnd("configured home"))
        self.assertEqual(self.bound().native_id, native)
        self.assertEqual(len(list(self.record().parent.glob("*.json"))), 1)
        self.assertFalse((client_home / ".ensemble").exists())
        self.assertTrue(all(call["env"]["HOME"] == str(client_home) for call in self.calls()))

    def test_configured_home_in_writable_roots_keeps_caller_store_fresh_and_resumed(self):
        self.scene([["writable client home"]])
        for index, grant in enumerate((self.work, self.extra)):
            with self.subTest(grant=grant):
                client_home = grant / "client-home"
                client_home.mkdir()
                self.clients["codex"]["env"]["HOME"] = str(client_home)
                self.session_cls = configured(self.clients)
                name, sid = f"configured{index}", f"configured-{index}"
                before = len(self.calls())
                first = self.start(name, sid=sid)
                first_stream = Stream(self, first)
                found = [first_stream.next()]
                while not isinstance(found[-1], TurnEnd):
                    self.assertNotIsInstance(found[-1], Exited)
                    found.append(first_stream.next())
                self.assertEqual(found[-1], TurnEnd("writable client home"))
                native = self.bound(sid).native_id
                first.stop()
                second = self.start(name, sid=sid, resume=True)
                self.assertEqual(
                    Stream(self, second).until(TurnEnd)[-1], TurnEnd("writable client home")
                )
                second.stop()
                self.assertEqual(self.bound(sid).native_id, native)
                self.assertFalse((client_home / ".ensemble").exists())
                self.assertTrue(
                    all(call["env"]["HOME"] == str(client_home) for call in self.calls()[before:])
                )
        self.assertEqual(len(list(self.record().parent.glob("*.json"))), 2)

    def test_writable_dirs_api_queries_tui_saved_roots_and_environment(self):
        first_tmp, next_tmp, ignored_home = (
            self.root / n for n in ("first-tmp", "next-tmp", "ignored-home")
        )
        first_tmp.mkdir()
        next_tmp.mkdir()
        self.clients["codex"]["env"]["TMPDIR"] = str(first_tmp)
        self.session_cls = configured(self.clients)
        fresh = self.session_cls.writable_dirs(dataclasses.replace(self.spec, client="codex"))
        self.assertEqual(list(fresh[:3]), [str(p) for p in (self.cwd, self.codex_home, self.extra)])
        self.assertIn(str(first_tmp), fresh)
        self.assertFalse(Path(SessionStore().root).parent.exists())
        self.assertEqual(self.logged(), [])
        first = self.start()
        self.assertEqual(Stream(self, first).until(TurnEnd)[-1], TurnEnd("first"))
        saved = self.bound().writable_roots
        first.stop()
        self.clients["codex"]["env"].update(
            {"TMPDIR": str(next_tmp), "CODEX_HOME": str(ignored_home)}
        )
        self.session_cls = configured(self.clients)
        changed_spec = dataclasses.replace(
            self.spec, access=Access(cwd=str(self.extra), project=True)
        )
        before = self.record().read_bytes()
        resumed = self.session_cls.writable_dirs(
            dataclasses.replace(changed_spec, client="codex", sid=self.sid, resume=True)
        )
        self.assertIsInstance(saved, tuple)
        self.assertEqual(tuple(resumed[: len(saved)]), saved)
        self.assertNotIn(str(next_tmp), resumed)
        self.assertNotIn(str(ignored_home), resumed)
        self.assertEqual(self.record().read_bytes(), before)
        second = self.start(resume=True)
        self.assertEqual(Stream(self, second).until(TurnEnd)[-1], TurnEnd("first"))
        self.assertEqual(self.calls()[-1]["env"]["TMPDIR"], str(first_tmp))
        self.assertEqual(self.calls()[-1]["env"]["CODEX_HOME"], str(self.codex_home))

    def assert_nested_environment(self, inherited, expected):
        with mock.patch.dict(os.environ, inherited):
            first = self.start()
            self.assertEqual(Stream(self, first).until(TurnEnd)[-1], TurnEnd("first"))
            first.stop()
            second = self.start(resume=True)
            self.assertEqual(Stream(self, second).until(TurnEnd)[-1], TurnEnd("first"))
            with mock.patch.dict(os.environ, self.server.inside("w1")):
                nested = self.start("z1", sid="nested", events=str(self.root / "nested.events"))
                self.assertEqual(Stream(self, nested).until(TurnEnd)[-1], TurnEnd("first"))
        for call in self.calls():
            self.assertEqual({key: call["env"][key] for key in inherited}, expected)
            self.assertEqual(call["env"]["CODEX_HOME"], str(self.codex_home))
        self.assertNotEqual(self.bound().native_id, self.bound("nested").native_id)
        screen = self.server.tmux("capture-pane", "-p", "-e", "-t", "=z1:").stdout
        self.assertRegex(screen, r"\x1b\[[0-9;]*36mfirst")


class TuiRecoveryIntegrationTest(Live):
    def test_hooks_persist_binding_without_any_consumer(self):
        release = self.root / "release"
        self.scene([[{"kind": "pause", "file": str(release)}, "done"]])
        handle = self.start()
        live_tmux.wait(lambda: self.bound().native_id, WAIT, what="durable root binding")
        # its harness, not the caller, reads the hooks: no events() call is needed
        self.assertEqual(RunLog(str(self.work)).read()[0]["kind"], "session")
        native = self.bound().native_id
        handle.stop()
        self.scene([["recovered"]])
        resumed = self.start(resume=True)
        stream = Stream(self, resumed)
        self.assertEqual(
            stream.until(TurnEnd),
            [Activity(), TurnStarted(), TurnEnd("recovered")],
        )
        self.assertEqual(self.calls()[-1]["argv"][:2], ["resume", native])
        self.assertEqual(
            list(Path(SessionStore().root).glob("*.json")),
            [self.record()],
        )

    def test_interrupt_completes_without_stop_or_the_previous_reply(self):
        self.scene(
            [["kept"], ["partial", {"kind": "interrupt"}], [{"kind": "interrupt"}], ["after"]]
        )
        handle = self.start()
        stream = Stream(self, handle)
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("kept"))
        handle.send("interrupt")
        self.assertEqual(stream.until(TurnEnd), [TurnStarted(), TurnEnd()])
        self.assertEqual(self.lines(2), ["w1 done", "w1 done"])
        self.assertEqual(
            self.attach("interrupt again"),
            [[self.sid, True, ""], ["", 0], ""],
        )
        self.assertEqual(stream.until(TurnEnd), [TurnStarted(), TurnEnd()])
        self.assertEqual(self.attach(), [self.sid, True, ""])
        handle.send("recover")
        self.assertEqual(stream.until(TurnEnd), [TurnStarted(), TurnEnd("after")])
        live_tmux.wait(lambda: len(self.hooks()) == 9, WAIT, what="all hook results logged")
        self.assertEqual([h["hook"] for h in self.hooks()].count("Stop"), 2)
        self.assertEqual([h["hook"] for h in self.hooks()].count("Interrupt"), 2)
        self.assertEqual(self.lines(4), ["w1 done"] * 4)

    def test_interrupt_before_stop_and_null_stop_have_empty_turn_replies(self):
        self.scene([[{"kind": "interrupt"}], []])
        handle = self.start()
        stream = Stream(self, handle)
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd())
        self.assertEqual(self.attach(), [self.sid, True, ""])
        handle.send("silent")
        self.assertEqual(stream.until(TurnEnd), [TurnStarted(), TurnEnd()])
        live_tmux.wait(
            lambda: any(row["hook"] == "Stop" for row in self.hooks()),
            WAIT,
            what="null-reply Stop hook logged",
        )
        self.assertIsNone(self.hooks()[-1]["payload"]["last_assistant_message"])
        self.assertEqual(self.lines(2), ["w1 done"] * 2)

    def test_a_failed_turn_without_stop_is_a_turn_failed(self):
        self.scene([[{"kind": "fail", "message": "bad request"}], ["after"]])
        handle = self.start()
        stream = Stream(self, handle)
        self.assertEqual(stream.until(TurnFailed)[-1], TurnFailed("other", "bad request"))
        self.assertNotIn("Stop", [h["hook"] for h in self.hooks()])
        handle.send("recover")
        # The failed turn's hook Activities may come after its TurnFailed, read first.
        after = stream.until(TurnEnd)
        self.assertEqual((set(after[:-1]), after[-1]), ({TurnStarted()}, TurnEnd("after")))

    def test_overloaded_first_and_later_pane_input_each_fail_once_without_stop(self):
        failed = {"kind": "fail", "info": "server_overloaded", "message": "try again later"}
        self.scene([[failed], [failed], ["recovered"]])
        handle = self.start()
        stream = Stream(self, handle)
        first = stream.until(TurnFailed)
        self.assertEqual(first.count(TurnStarted()), 1)
        self.assertEqual(first[-1], TurnFailed("server_overloaded", "try again later"))
        self.server.tmux("send-keys", "-t", "=w1:", "-l", "continue")
        self.server.tmux("send-keys", "-t", "=w1:", "Enter")
        second = stream.until(TurnFailed)
        self.assertEqual(
            second, [TurnStarted(), TurnFailed("server_overloaded", "try again later")]
        )
        self.assertNotIn("Stop", [hook["hook"] for hook in self.hooks()])
        handle.send("continue again")
        self.assertEqual(stream.until(TurnEnd), [TurnStarted(), TurnEnd("recovered")])

    def test_root_child_cwd_and_rebinding_filters_publish_only_valid_hooks(self):
        invalid = [
            {"agent_id": CHANGED, "last_assistant_message": "child"},
            {"cwd": str(self.extra), "last_assistant_message": "foreign cwd"},
            {"session_id": CHANGED, "last_assistant_message": "rebound"},
            {"session_id": "invalid", "last_assistant_message": "invalid"},
            {"hook_event_name": "Interrupt", "last_assistant_message": "wrong label"},
        ]
        self.scene(
            [
                [
                    *(dict(kind="hook", label="Stop", payload=value) for value in invalid),
                    {
                        "kind": "hook",
                        "label": "PermissionRequest",
                        "payload": {"agent_id": CHANGED},
                    },
                    {"kind": "tool", "tool_name": "Bash"},
                    "root",
                ]
            ]
        )
        handle = self.start()
        stream = Stream(self, handle)
        found = stream.until(TurnEnd)
        self.assertEqual(
            found,
            [
                Activity(),
                TurnStarted(),
                Activity("Bash"),
                TurnEnd("root"),
            ],
        )
        self.assertEqual(self.lines(1), ["w1 done"])
        self.assertEqual(self.attach()[2], "root")
        stream.quiet(0.4)

    def test_resume_keeps_native_id_cwd_home_model_effort_dirs_network_and_commands(self):
        handle = self.start()
        stream = Stream(self, handle)
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("first"))
        native, saved = self.bound().native_id, self.bound().snapshot
        handle.stop()
        changed = self.root / "changed"
        changed.mkdir()
        cfg = self.clients["codex"]
        cfg.update(
            tiers={"1": "changed"},
            efforts={"low": "high"},
        )
        cfg["env"]["CODEX_HOME"] = str(changed)
        self.session_cls = configured(self.clients)
        spec = dataclasses.replace(
            self.spec,
            tier=4,
            effort="max",
            access=Access(cwd=str(changed), project=False, commands=("changed command",)),
        )
        self.scene([["resumed"]])
        resumed = self.start(resume=True, spec=spec)
        self.assertTrue(resumed.info().project)
        self.assertTrue(resumed.info().transcript)
        self.assertIn(native, resumed.info().resume)
        found = Stream(self, resumed).until(TurnEnd)
        self.assertEqual(found, [Activity(), TurnStarted(), TurnEnd("resumed")])
        first, second = self.calls()
        self.assertEqual(second["argv"], ["resume", native, *first["argv"]])
        self.assertEqual(
            (second["cwd"], second["env"]["CODEX_HOME"]), (str(self.cwd), str(self.codex_home))
        )
        self.assertEqual(self.bound().snapshot, saved)
        starts = [h for h in self.hooks() if h["hook"] == "SessionStart"]
        self.assertEqual([h["payload"]["source"] for h in starts], ["startup", "resume"])
        self.assertEqual(
            starts[0]["payload"]["transcript_path"], starts[1]["payload"]["transcript_path"]
        )
        self.assertEqual(
            self.session_cls.inspect(
                Spec(
                    workdir=str(self.work),
                    access=Access(cwd=str(self.work)),
                    client="codex",
                    sid=self.sid,
                )
            ).resumable,
            "yes",
        )

    def test_headless_to_tui_to_headless_resume_uses_one_store(self):
        self.headless()
        handle = self.start(resume=True)
        self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
        handle.stop()
        self.headless(resume=True)
        self.assertEqual(self.client_ids(), [fake_codex.NATIVE] * 3)
        self.assertEqual(self.bound().native_id, fake_codex.NATIVE)
        self.assertEqual(self.calls()[0]["argv"][:2], ["resume", fake_codex.NATIVE])
        self.assertEqual(len(list(self.record().parent.glob("*.json"))), 1)

    def test_tui_to_headless_resume_uses_the_hook_binding(self):
        handle = self.start()
        self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
        native = self.bound().native_id
        handle.stop()
        self.headless(resume=True)
        self.assertEqual(self.client_ids(), [native, native])
        self.assertEqual(self.bound().native_id, native)
        self.assertEqual(len(list(self.record().parent.glob("*.json"))), 1)

    def test_resume_missing_binding_and_fifo_record_fail_before_spawn(self):
        with self.assertRaisesRegex(ConfigError, "no transcript to resume"):
            self.start(resume=True)
        self.assertEqual(self.logged(), [])
        handle = self.start()
        self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
        handle.stop()
        path, before = self.record(), len(self.logged())
        path.unlink()
        os.mkfifo(path, 0o600)
        started = time.monotonic()
        with self.assertRaises(ConfigError):
            self.start(resume=True)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(self.logged()), before)

    def test_source_roots_interpreter_prefix_and_tmux_bin_are_grantable(self):
        for index, root in enumerate((*ROOTS, sys.prefix, self.server.bin)):
            with self.subTest(root=root):
                spec = dataclasses.replace(
                    self.spec, access=dataclasses.replace(self.spec.access, dirs=(root,))
                )
                self.start(f"g{index}", sid=f"granted-{index}", spec=spec)
                self.assertEqual(
                    [r.name for r in self.session_cls.list(f"g{index}")], [f"g{index}"]
                )

    def test_store_grants_and_untrusted_settings_are_rejected_before_tmux_or_client(self):
        roots = (str(self.home),)
        for index, root in enumerate(roots):
            with self.subTest(root=root):
                spec = dataclasses.replace(
                    self.spec, access=dataclasses.replace(self.spec.access, dirs=(root,))
                )
                with (
                    mock.patch(
                        "subprocess.Popen", side_effect=AssertionError("spawned before guard")
                    ),
                    self.assertRaisesRegex(ConfigError, "writable|rewrite"),
                ):
                    self.start(sid=f"unsafe-{index}", spec=spec)
        (self.cwd / ".codex").mkdir()
        spec = dataclasses.replace(
            self.spec, access=dataclasses.replace(self.spec.access, project=False)
        )
        with self.assertRaisesRegex(ConfigError, "project Codex settings"):
            self.start(spec=spec)
        self.assertEqual(self.logged(), [])
        self.assertEqual([r.name for r in self.session_cls.list()], ["mgr"])
        self.assertFalse((self.work / "run.jsonl").exists())


class HookTest(Base):
    def test_generic_isolated_hook_loads_binding_callback_and_records_once(self):
        self.prepare()
        folder = self.root / "hook-session"
        folder.mkdir()

        command = (os.path.join(PLUGIN_BIN, "ensemble-session"), "hook", str(folder), "Stop")
        env = {**os.environ, "PATH": os.pathsep.join((os.path.dirname(sys.executable), os.defpath))}
        state = {
            "client": "codex",
            "sid": self.spec.sid,
            "workdir": str(self.work),
            "cwd": str(self.cwd),
            "project": False,
            "transcript": "",
            "resume": "",
            "events": None,
            "words": {},
            "tmux_path": sys.executable,
            "client_hook": HookCallback(tui.__name__, {"store": SessionStore().root}).fields(),
        }
        (folder / "session.json").write_text(json.dumps(state))
        result = subprocess.run(
            command,
            input=json.dumps(
                {"session_id": NATIVE, "cwd": str(self.cwd), "last_assistant_message": "reply"}
            ),
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        records = [json.loads(line) for line in (folder / "hooks.jsonl").read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["payload"]["hook_event_name"], "Stop")
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        result = subprocess.run(
            command,
            input=json.dumps({"session_id": NATIVE, "cwd": str(self.cwd), "agent_id": CHILD}),
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(len((folder / "hooks.jsonl").read_text().splitlines()), 1)
