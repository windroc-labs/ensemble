"""Isolated Codex unit fixtures with recovery records and native session files."""

import dataclasses
import json
import os
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ensemble.session.clients.codex import CodexClient
from ensemble.session.clients.codex.common import SessionStore
from ensemble.session.engine.types import LOCAL_CONTEXT, Access, Launch, Permission, Spec
from tests.session.case import REPO, Case
from tests.session.unit.clients.support import packaged_table
from tests.utils.sandbox import scratch

PLUGIN_OFF = ("-c", "plugins.ensemble@windroc.enabled=false")
TOOLS_OFF = (
    *(
        ("-c", f"plugins.{name}@openai-bundled.enabled=false")
        for name in (
            "browser",
            "chrome",
            "chrome-dev",
            "chrome-internal",
            "computer-use",
            "unified-computer-use",
        )
    ),
    ("--disable", "apps"),
    ("--disable", "tool_suggest"),
)
UNIFIED = (
    ("-s", "danger-full-access"),
    ("-c", 'approval_policy="on-request"'),
    ("-c", 'approvals_reviewer="auto_review"'),
)
AUTO = tuple(word for flag in UNIFIED for word in flag)
BYPASS = "--dangerously-bypass-approvals-and-sandbox"
MODES = {"auto": list(AUTO), "yolo": [BYPASS]}
NATIVE = "11111111-2222-3333-4444-555555555555"
CHILD = "aaaaaaaa-2222-3333-4444-555555555555"


def packaged(local: str = os.devnull) -> CodexClient:
    """The client the packaged session config.toml, under `local`, builds."""
    return CodexClient(packaged_table("codex", local))


def said(role, *texts):
    """A Codex session file message record."""
    kind = "output_text" if role == "assistant" else "input_text"
    content = [{"type": kind, "text": text} for text in texts]
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": content},
    }


def committed_headless(
    client: CodexClient, spec: Spec, permission: Permission = Permission.AUTO
) -> Launch:
    run = client.headless()
    run._context = replace(LOCAL_CONTEXT, permission=permission)
    launch = run.launch(spec)
    run.commit()
    return launch


def committed_tui(
    client: CodexClient, spec: Spec, hook, permission: Permission = Permission.AUTO
) -> Launch:
    run = client.tui()
    run._context = replace(LOCAL_CONTEXT, permission=permission)
    launch = run.launch(spec, hook)
    run.commit()
    return launch


class Base(Case):
    def setUp(self):
        self.root = Path(scratch(self, os.environ.get("ENSEMBLE_TEST_TMP", REPO)))
        self.home = self.root / "home"
        self.work = self.root / "work"
        self.cwd = self.work / "project"
        self.extra = self.root / "extra"
        self.codex_home = self.home / ".codex"
        for path in (self.home, self.cwd, self.extra, self.codex_home):
            path.mkdir(parents=True, exist_ok=True)
        self.enterContext(
            mock.patch.dict(
                os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}
            )
        )
        lexists = os.path.lexists
        self.enterContext(
            mock.patch(
                "os.path.lexists",
                side_effect=lambda path: (
                    os.path.commonpath((os.path.abspath(path), str(self.root))) == str(self.root)
                    and lexists(path)
                ),
            )
        )
        self.client = CodexClient({"tiers": {"1": "gpt-test"}, "efforts": {"low": "low"}})
        self.common = self.client.common
        self.spec = Spec(
            workdir=str(self.work),
            sid="caller-id",
            tier=1,
            effort="low",
            access=Access(cwd=str(self.cwd), dirs=(str(self.extra),), commands=("git status",)),
        )

    def prepare(self):
        return self.common.save(self.common.prepare(self.spec))

    def rollout(self, native=NATIVE, *, cwd=None, parent=None, stamp=100):
        path = self.codex_home / "sessions" / "2026" / "10" / "11" / f"rollout-test-{native}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"id": native, "cwd": cwd or str(self.cwd), "source": "exec"}
        if parent:
            payload["parent_thread_id"] = parent
            payload["source"] = {
                "subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}
            }
        path.write_text(
            json.dumps({"type": "session_meta", "payload": payload}) + "\n", encoding="utf-8"
        )
        os.utime(path, (stamp, stamp))
        return path

    def record(self):
        return next(Path(SessionStore().root).glob("*.json"))

    def edit_record(self, **fields):
        path = self.record()
        value = json.loads(path.read_text())
        value.update(fields)
        path.write_text(json.dumps(value), encoding="utf-8")


class LaunchPolicyCases:
    """A mode's `argv(client, spec, *, resume=False)` replays the supplied policy."""

    def generated(self, options):
        pass

    def test_supplied_policy_and_saved_replay_in_both_modes(self):
        for saved, current in (
            ("workspace-write", "danger-full-access"),
            ("danger-full-access", "workspace-write"),
        ):
            with self.subTest(mode=self.mode, saved=saved):
                flags = (
                    "-s",
                    saved,
                    "-c",
                    'approval_policy="on-request"',
                    "-c",
                    'approvals_reviewer="auto_review"',
                    "-c",
                    "sandbox_workspace_write.network_access=false",
                )
                spec = dataclasses.replace(
                    self.spec, sid=f"{self.mode}-{saved}", flags=flags, env={"K": "saved"}
                )
                first = self.argv(self.client, spec)
                self.assertEqual(first[first.index("-s") :][: len(flags)], list(flags))
                self.common.bind(str(self.work), spec.sid, NATIVE)
                self.rollout()
                changed = dataclasses.replace(spec, flags=("-s", current), env={"K": "changed"})
                second = self.argv(self.client, changed, resume=True)
                self.assertEqual(second[second.index("-s") :][: len(flags)], list(flags))
                self.assertEqual(
                    self.common.load(str(self.work), spec.sid).snapshot.flags, tuple(flags)
                )

    def test_separator_tail_follows_generated_options_on_fresh_and_resume(self):
        with self.subTest(mode=self.mode):
            supplied = ("--enable", "web", "--", "literal", "--last")
            spec = dataclasses.replace(self.spec, sid=f"separator-{self.mode}", flags=supplied)
            first = self.argv(self.client, spec)
            self.common.bind(str(self.work), spec.sid, NATIVE)
            self.rollout()
            changed = dataclasses.replace(spec, flags=("--", "changed"))
            second = self.argv(self.client, changed, resume=True)
            for argv in (first, second):
                boundary = argv.index("--")
                self.assertEqual(argv[boundary:], ["--", "literal", "--last", "hi"])
                self.assertIn("gpt-test", argv[:boundary])
                self.assertIn(str(self.extra), argv[:boundary])
                self.assertIn("project_doc_max_bytes=0", argv[:boundary])
                self.generated(argv[:boundary])
            self.assertEqual(
                self.common.load(str(self.work), spec.sid).snapshot.flags, tuple(supplied)
            )
