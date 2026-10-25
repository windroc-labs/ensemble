"""Claude claude behavior."""

import contextlib
import io
import os
from unittest import mock

from ensemble.session.clients.claude import ClaudeClient
from ensemble.session.clients.claude.common import ClaudeCommon as UnconfiguredCommon
from ensemble.session.clients.claude.headless import ClaudeHeadless
from ensemble.session.clients.claude.tui import ClaudeTui
from ensemble.session.engine.types import (
    Access,
    ConfigError,
    Launch,
    SessionInfo,
    SessionStatus,
)
from ensemble.utils import LOCAL_ENV_VARS

from session.tests.unit.clients.claude.support import (
    CONFIG,
    MODES,
    NOTE,
    PROBE,
    SID,
    UNSET,
    WHAT,
    Base,
    ClaudeCommon,
    Spec,
    fixture,
    reference,
)


class ConfigTest(Base):
    def test_every_key_is_optional_and_each_is_accepted(self):
        self.assertEqual(ClaudeClient({}).config, {})
        self.assertEqual(ClaudeClient(CONFIG).config, CONFIG)
        self.assertEqual(ClaudeClient({"modes": MODES}).config, {"modes": MODES})

    def test_a_refused_config_names_the_key_never_the_value(self):
        sources = (
            "--setting-sources is set by the Claude client: remove it from [clients.claude].flags"
        )
        rows = (
            ("flags", "SECRET"),
            ("flags", ["--x", 1]),
            ("flags", ("SECRET",)),
            ("flags", {"SECRET": "x"}),
            ("allow", "SECRET"),
            ("allow", [None]),
            ("tiers", "SECRET"),
            ("tiers", ["SECRET"]),
            ("tiers", {"1": 4}),
            ("tiers", {"5": "SECRET"}),
            ("tiers", {"0": "SECRET"}),
            ("tiers", {1: "SECRET"}),
            ("tiers", {"1": " "}),
            ("efforts", {"low": "a\nb"}),
            ("efforts", "SECRET"),
            ("efforts", {"low": 1}),
            ("efforts", {"turbo": "SECRET"}),
            ("efforts", {1: "SECRET"}),
            ("env", "SECRET"),
            ("env", {"K": 1}),
            ("env", {1: "SECRET"}),
            ("env", ["SECRET"]),
            ("modes", "SECRET"),
            ("modes", ["SECRET"]),
            ("modes", {"turbo": ["SECRET"]}),
            ("modes", {1: ["SECRET"]}),
            ("modes", {"auto": "SECRET"}),
            ("modes", {"yolo": ["--x", 1]}),
        )
        cases = (
            ({"model": "m"}, "unknown key 'model' in ClaudeClient's config"),
            *(({key: value}, f"[clients.claude].{key} must be {WHAT[key]}") for key, value in rows),
            *(
                ({"flags": flags}, sources)
                for flags in (
                    ["--setting-sources"],
                    ["--setting-sources=user"],
                    ["--verbose", "--setting-sources", "user"],
                )
            ),
            (
                {"modes": {"yolo": ["--setting-sources", "user"]}},
                sources.replace("].flags", "].modes.yolo"),
            ),
        )
        for config, message in cases:
            with self.subTest(config=config):
                self.refused(lambda *, config=config: ClaudeClient(config), message)

    def test_invalid_environment_names_and_values_fail_before_launch(self):
        for key, value in (
            ("", "SECRET"),
            ("A=B", "SECRET"),
            ("A\0B", "SECRET"),
            (1, "SECRET"),
            ("A", "SECRET\0"),
            ("A", 1),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigError) as caught:
                    ClaudeClient({"env": {key: value}})
                self.assertIn("[clients.claude].env", str(caught.exception))
                self.assertNotIn("SECRET", str(caught.exception))

    def test_accepts_exec_environment_names(self):
        env = {"A-B": "", "1A": "v", "X;Y": "v", "A B": "v", "Ä": "v"}
        self.assertEqual(ClaudeClient({"env": env}).config["env"], env)

    def test_the_env_attributes(self):
        client = ClaudeClient({})
        self.assertEqual(
            (client.session_env, client.identity_env, client.child_defaults),
            ("CLAUDECODE", tuple(key for key in UNSET if key not in LOCAL_ENV_VARS), {}),
        )


class ClientTest(Base):
    def test_its_parts(self):
        client = ClaudeClient(CONFIG)
        self.assertEqual(
            ClaudeClient.keys,
            frozenset({"flags", "allow", "tiers", "efforts", "env", "modes", "accounts"}),
        )
        self.assertEqual((ClaudeClient.client_note, ClaudeCommon.NOTE), (NOTE, NOTE))
        self.assertIsInstance(client.common, UnconfiguredCommon)
        for mode, kind in ((client.headless, ClaudeHeadless), (client.tui, ClaudeTui)):
            with self.subTest(kind=kind):
                self.assertIs(type(mode()), kind)
                self.assertIsNot(mode(), mode())
                self.assertIs(mode().common, client.common)

    def test_the_usage_probe_and_its_reading(self):
        self.assertEqual(ClaudeCommon.PROBE, PROBE)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                ClaudeClient(CONFIG).usage_probe(),
                Launch(
                    PROBE,
                    env={"K": "v", "DISABLE_AUTOUPDATER": "1"},
                    unset=(*UNSET, "CLAUDE_CODE_OAUTH_TOKEN"),
                ),
            )
        found = ClaudeClient({}).usage(fixture("usage-default.txt"))
        self.assertEqual((found.five_hour.used_percent, found.weekly.used_percent), (17.0, 1.0))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(ClaudeClient({}).usage("garbage"))

    def test_status_uses_an_in_store_recorded_transcript_and_rebuilds_resume(self):
        client = ClaudeClient({})
        folder = os.path.dirname(client.common.transcript(self.work, SID))
        os.makedirs(folder)
        path = os.path.join(folder, "historical.jsonl")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("recorded")
        info = SessionInfo(
            sid=SID, cwd=self.work, project=False, transcript=path, resume="rm -rf ~"
        )
        self.assertEqual(
            client.session_status(info),
            SessionStatus(
                "yes",
                os.stat(path).st_mtime,
                reference(self.work),
            ),
        )

    def test_status_looks_under_the_config_dir_of_the_env_it_is_given(self):
        client, env = ClaudeClient({}), {"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "account")}
        path = client.common.transcript(self.work, SID, env)
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("recorded")
        info = SessionInfo(sid=SID, cwd=self.work, project=False, transcript=path, resume="")
        self.assertEqual(client.session_status(info, env=env).resumable, "yes")
        self.assertEqual(client.session_status(info).resumable, "no")

    def test_session_status(self):
        self.assertEqual(
            ClaudeClient({}).session_status(
                SessionInfo(sid=SID, cwd=self.work, project=False, transcript="", resume="")
            ),
            SessionStatus("no", None, reference(self.work)),
        )
        self.assertEqual(
            ClaudeClient({}).session_status(
                SessionInfo(sid="x", cwd=self.work, project=False, transcript="", resume="")
            ),
            SessionStatus("no", None, ""),
        )


class WritableDirsTest(Base):
    def setUp(self):
        super().setUp()
        self.cwd, self.added = (os.path.join(self.tmp, name) for name in ("cwd", "added"))
        for path in (self.cwd, self.added):
            os.mkdir(path)
        self.link = os.path.join(self.tmp, "link")
        os.symlink(self.added, self.link)

    def dirs(self, *, resume=False, workdir=None, **access) -> tuple[str, ...]:
        spec = Spec(
            workdir=workdir or self.work,
            sid=SID,
            access=Access(**{"cwd": workdir or self.work, **access}),
            resume=resume,
        )
        return ClaudeClient(CONFIG).writable_dirs(spec)

    def test_the_cwd_first_then_the_added_dirs_real_paths_each_once_in_order(self):
        work_link = os.path.join(self.tmp, "work-link")
        os.symlink(self.work, work_link)
        for access, dirs in (
            ({}, (self.work,)),
            ({"dirs": (self.added, self.cwd)}, (self.work, self.added, self.cwd)),
            ({"cwd": self.cwd}, (self.cwd,)),
            ({"cwd": self.cwd, "dirs": (self.added,)}, (self.cwd, self.added)),
            ({"workdir": work_link}, (self.work,)),
            (
                {"cwd": self.link, "dirs": (self.added, self.work, work_link, self.cwd + "/")},
                (self.added, self.work, self.cwd),
            ),
        ):
            with self.subTest(access=access):
                self.assertEqual(self.dirs(**access), dirs)

    def test_a_resume_the_same(self):
        for access, dirs in (
            ({}, (self.work,)),
            ({"cwd": self.cwd}, (self.cwd,)),
            ({"dirs": (self.link, self.work)}, (self.work, self.added)),
        ):
            with self.subTest(access=access):
                self.assertEqual(self.dirs(resume=True, **access), dirs)

    def test_read_dirs_never(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                self.assertEqual(
                    self.dirs(resume=resume, dirs=(self.added,), reads=(self.cwd, self.link)),
                    (self.work, self.added),
                )

    def test_access_and_flag_dirs_are_combined_for_every_session(self):
        os.mkdir(os.path.join(self.cwd, "sub"))
        flags = (
            "--verbose",
            "--add-dir",
            "sub",
            "--add-dir=../added",
            "--add-dir",
            self.link,
            "--",
            "--add-dir",
            "/x",
        )
        for resume in (False, True):
            for cwd, given, dirs in (
                (self.work, (), (self.work, self.tmp)),
                (self.cwd, (), (self.cwd, self.tmp)),
                (self.cwd, flags, (self.cwd, self.tmp, os.path.join(self.cwd, "sub"), self.added)),
            ):
                with self.subTest(resume=resume, cwd=cwd, flags=given):
                    spec = Spec(
                        workdir=self.work,
                        sid=SID,
                        access=Access(cwd=cwd, dirs=(self.tmp,), reads=(self.tmp,)),
                        flags=given,
                        resume=resume,
                    )
                    self.assertEqual(ClaudeClient(CONFIG).writable_dirs(spec), dirs)
