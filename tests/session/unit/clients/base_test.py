"""Session behavior tests with caller-owned dependencies."""

import dataclasses
import inspect
import operator
import typing
from types import MappingProxyType
from unittest import mock

from ensemble.session.clients.base import Client
from ensemble.session.clients.claude import ClaudeClient
from ensemble.session.clients.claude.tui import ClaudeTui
from ensemble.session.clients.codex import CodexClient
from ensemble.session.clients.codex.tui import CodexTui
from ensemble.session.engine import types as engine_types
from ensemble.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ClientBound,
    ConfigError,
    Effort,
    Exited,
    Launch,
    LimitHit,
    Reply,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
    Usage,
    Window,
)
from ensemble.session.modes.client_adapters.headless import Headless, JsonHeadless
from ensemble.session.modes.client_adapters.in_process import InProcess
from ensemble.session.modes.client_adapters.tui import Tui
from ensemble.utils import sid_error
from tests.session.case import Case
from tests.session.client_adapters_support import DetailedTui, StubHeadless, StubInProcess, StubTui

UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


class StubClient(Client):
    keys = frozenset({"model", "flags"})

    def headless(self):
        return StubHeadless()

    def tui(self):
        return StubTui()


class Refusals(Case):
    def refused(self, build, message):
        with self.assertRaises(ConfigError) as caught:
            build()
        self.assertEqual(str(caught.exception), message)


class EventsTest(Case):
    def test_the_frozen_set_is_the_six_events(self):
        self.assertEqual(
            typing.get_args(engine_types.SessionEvent),
            (Activity, Reply, TurnEnd, TurnFailed, Blocked, LimitHit, Exited),
        )
        for other in (Stderr, ClientBound):
            self.assertNotIn(other, typing.get_args(engine_types.SessionEvent))

    def test_defaults(self):
        self.assertEqual(
            (Activity(), TurnEnd(), Blocked(), LimitHit()),
            (Activity(""), TurnEnd("", 0), Blocked(""), LimitHit(None)),
        )
        self.assertEqual(LimitHit(1767225600.0).resets_at, 1767225600.0)
        for event, name in ((TurnEnd(), "reply"), (LimitHit(), "resets_at")):
            with self.subTest(name=name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(event, name, "x")


class ReportsTest(Case):
    def test_usage_window_and_session_status_are_frozen_records(self):
        window = Window(used_percent=120.5, resets_at=None)
        self.assertEqual((window.used_percent, window.resets_at), (120.5, None))
        usage = Usage(five_hour=window, weekly=None)
        self.assertEqual((usage.five_hour, usage.weekly), (Window(120.5, None), None))
        self.assertEqual(Usage(None, Window(3.0, 1.5)).weekly.resets_at, 1.5)
        status = SessionStatus(resumable="yes", last_active=1.5, resume_command="x --resume s")
        self.assertEqual(
            (status.resumable, status.last_active, status.resume_command),
            ("yes", 1.5, "x --resume s"),
        )
        for record, name in ((window, "used_percent"), (usage, "five_hour"), (status, "resumable")):
            with self.subTest(name=name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(record, name, "x")


class ClientTest(Refusals):
    def test_the_mode_interfaces_cannot_be_built(self):
        for interface in (Headless, JsonHeadless, Tui, InProcess):
            with self.subTest(interface=interface.__name__), self.assertRaises(TypeError):
                interface()

    def test_refuses_the_first_unknown_key_in_sorted_order(self):
        self.refused(
            lambda: StubClient({"zeta": 1, "alpha": 2, "model": "m"}),
            "unknown key 'alpha' in StubClient's config",
        )

    def test_selects_a_session_through_its_selection_syntax(self):
        client = StubClient({})
        self.assertEqual(client.select_session("n", resume=True), ())
        client.selection, client.selection_uuid = (("--new",), ("--pick",)), True
        self.assertEqual(client.select_session(UUID, resume=False), ("--new", UUID))
        self.assertEqual(client.select_session(UUID, resume=True), ("--pick", UUID))
        self.refused(
            lambda: client.select_session("n", resume=True), "session ids are UUIDs, got 'n'"
        )

    def test_keeps_a_copy_of_its_config(self):
        config = {"model": "m", "flags": ["-x"]}
        client = StubClient(config)
        self.assertEqual(client.config, config)
        self.assertIsNot(client.config, config)

    def test_two_clients_share_no_mutable_config_even_from_a_read_only_one(self):
        for config in (
            {"flags": ["-x"]},
            MappingProxyType({"flags": ("-x", ["-y"]), "model": MappingProxyType({"a": "b"})}),
        ):
            first, second = StubClient(config), StubClient(config)
            with self.subTest(config=config):
                flags = first.config["flags"]
                (flags if isinstance(flags, list) else flags[1]).append("-z")
                self.assertEqual(second.config, StubClient(config).config)
                self.assertNotIn("-z", str(config))
                self.assertIs(type(first.config), dict)

    def test_the_shared_default_maps_are_read_only(self):
        for name, mapping in (
            ("Client.child_defaults", Client.child_defaults),
            ("Tui.hooks", Tui.hooks),
            ("CodexClient.child_defaults", CodexClient.child_defaults),
            ("ClaudeClient.child_defaults", ClaudeClient.child_defaults),
            ("ClaudeTui.hooks", ClaudeTui.hooks),
            ("CodexTui.hooks", CodexTui.hooks),
        ):
            with self.subTest(name=name), self.assertRaises(TypeError):
                operator.setitem(mapping, "X", "1")

    def test_defaults(self):
        self.assertEqual((Client.keys, Client.client_note, Tui.dialogs), (frozenset(), "", ()))
        self.assertEqual(
            (Client.session_env, Client.identity_env, Client.child_defaults), ("", (), {})
        )
        client = StubClient({})
        self.assertIsNone(client.usage_probe())
        self.assertIsNone(client._accounts())
        self.assertIsNone(client._usage_probes())
        for output in ("", "garbage", '{"type": "rate_limit_event"}'):
            with self.subTest(output=output):
                self.assertIsNone(client.usage(output))
        self.assertEqual(
            client.session_status(
                engine_types.SessionInfo(sid="s", cwd="/c", project=False, transcript="", resume="")
            ),
            SessionStatus("unknown", None, ""),
        )

    def test_session_metadata_preserves_info_by_default(self):
        info = engine_types.SessionInfo("s", "/c", project=False, transcript="/t", resume="r")
        self.assertIs(StubClient({}).session_metadata(info), info)

    def test_writable_dirs_are_none_unless_overridden(self):
        for resume in (False, True):
            spec = Spec(workdir="/w", access=Access(dirs=("/d",), cwd="/c"), sid="s", resume=resume)
            with self.subTest(resume=resume):
                self.assertEqual(StubClient({}).writable_dirs(spec), ())

    def test_each_mode_factory_is_optional_and_none_unless_overridden(self):
        self.assertFalse(inspect.isabstract(Client))
        bare = Client({})
        self.assertEqual((bare.headless(), bare.tui(), bare.in_process()), (None, None, None))
        client = StubClient({})
        self.assertIsInstance(client.headless(), StubHeadless)
        self.assertIsInstance(client.tui(), StubTui)
        self.assertIsNone(client.in_process())

    def test_each_factory_call_is_a_fresh_mode_object(self):
        client = StubClient({})
        for factory in (client.headless, client.tui):
            with self.subTest(factory=factory.__name__):
                self.assertIsNot(factory(), factory())


class NoProcessTest(Case):
    def test_nothing_starts_a_process(self):
        with (
            mock.patch("subprocess.Popen", side_effect=AssertionError("Popen")) as popen,
            mock.patch("subprocess.run", side_effect=AssertionError("run")) as run,
        ):
            client = StubClient({"model": "m"})
            tui = client.tui()
            tui.submit("x")
            tui.hook_events("end", {"session_id": "n1"})
            labeled = DetailedTui()
            for label in labeled.hooks:
                labeled.hook_events(label, {"session_id": "n1", "doing": "Bash"})
            tui.limit("screen")
            list(client.headless().events(['{"texts": ["a"]}']))
            client.usage_probe()
            client.usage("output")
            client.session_status(
                engine_types.SessionInfo(sid="s", cwd="/c", project=False, transcript="", resume="")
            )
            client.in_process()
            StubInProcess().check(Spec(workdir="/w", access=Access(cwd="/w")))
        popen.assert_not_called()
        run.assert_not_called()


class AccessTest(Refusals):
    def test_keeps_the_given_fields_and_defaults_the_rest(self):
        for fields, expected in (
            ({}, ((), (), "/c", False, (), {})),
            (
                {"dirs": ("/a",), "commands": ("git",), "project": True, "reads": ("/r",)},
                (("/a",), ("git",), "/c", True, ("/r",), {}),
            ),
            ({"reads": ("/r",), "env": {"A": "b"}}, ((), (), "/c", False, ("/r",), {"A": "b"})),
        ):
            with self.subTest(fields=fields):
                access = Access(cwd="/c", **fields)
                self.assertEqual(
                    (
                        access.dirs,
                        access.commands,
                        access.cwd,
                        access.project,
                        access.reads,
                        access.env,
                    ),
                    expected,
                )

    def test_takes_keywords_only(self):
        with self.assertRaises(TypeError):
            Access(("/a",))

    def test_refuses_each_bad_field_naming_it(self):
        env = "access env must map variable names to strings without NUL"
        cases = (
            *(
                ({name: bad}, f"{name} must be a tuple of strings")
                for bad in (["/a"], ("/a", 1), "/a", None)
                for name in ("dirs", "commands", "reads")
            ),
            *(
                ({"env": bad}, env)
                for bad in (
                    {"A=B": "a"},
                    {"A\0": "a"},
                    {"": "a"},
                    {1: "a"},
                    {"A": 1},
                    {"A": None},
                    {"A": "b\0"},
                    [("A", "b")],
                    None,
                )
            ),
        )
        for fields, message in cases:
            with self.subTest(fields=fields):
                self.refused(lambda *, fields=fields: Access(cwd="/c", **fields), message)

    def test_accepts_an_env_of_exec_names_to_strings(self):
        env = {
            "PYTHONPATH": "/a b;c",
            "_x1": "",
            "A-B": "v",
            "1A": "v",
            "X;Y": "v",
            "A B": "v",
            "A\n": "v",
            "Ä": "v",
        }
        self.assertEqual(Access(cwd="/c", env=MappingProxyType(env)).env, env)


class SpecTest(Refusals):
    def test_defaults(self):
        spec = Spec(workdir="/w", access=Access(cwd="/w"))
        self.assertEqual(
            (spec.access, spec.tier, spec.effort, spec.sid), (Access(cwd="/w"), None, None, None)
        )
        self.assertEqual(
            (spec.flags, spec.env, spec.name, spec.reply_tags),
            ((), {}, None, None),
        )

    def plain(self, **fields):
        return Spec(
            workdir="/w",
            access=Access(cwd="/w"),
            flags=("--model", "x", "--", "-p"),
            env={"A": "1", "_b2": "", "PATH": "/bin:/usr/bin"},
            name="worker-1",
            **fields,
        )

    def test_accepts_the_plain_session_fields(self):
        spec = self.plain()
        self.assertEqual(
            (spec.flags, dict(spec.env), spec.name),
            (
                ("--model", "x", "--", "-p"),
                {"A": "1", "_b2": "", "PATH": "/bin:/usr/bin"},
                "worker-1",
            ),
        )
        self.assertEqual(
            Spec(workdir="/w", access=Access(cwd="/w"), env=MappingProxyType({"A": "1"})).env,
            {"A": "1"},
        )
        self.assertEqual(Spec(workdir="/w", access=Access(cwd="/w"), name="").name, "")

    def test_env_is_an_immutable_copy_and_specs_compare_by_value(self):
        source = {"A": "1"}
        spec = Spec(workdir="/w", access=Access(cwd="/w"), env=source)
        source["B"] = "2"
        self.assertEqual(dict(spec.env), {"A": "1"})
        with self.assertRaises(TypeError):
            spec.env["C"] = "3"
        self.assertEqual(spec, Spec(workdir="/w", access=Access(cwd="/w"), env={"A": "1"}))
        self.assertNotEqual(spec, Spec(workdir="/w", access=Access(cwd="/w"), env={"A": "2"}))
        self.assertEqual(self.plain(), self.plain())

    def test_replace_keeps_the_plain_session_fields(self):
        spec = dataclasses.replace(self.plain(), tier=2, sid="s")
        self.assertEqual(spec, self.plain(tier=2, sid="s"))
        self.assertEqual(dict(spec.env), {"A": "1", "_b2": "", "PATH": "/bin:/usr/bin"})

    def test_reply_tags_are_none_or_a_progress_and_an_outcome_tag(self):
        self.assertEqual(
            Spec(workdir="/w", access=Access(cwd="/w"), reply_tags=("p-1", "o_2")).reply_tags,
            ("p-1", "o_2"),
        )
        for bad in (
            ["p", "o"],
            ("p",),
            ("p", "o", "x"),
            ("p", ""),
            ("p", "o b"),
            ("p:", "o"),
            ("p", 1),
            "po",
        ):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", access=Access(cwd="/w"), reply_tags=bad),
                    "reply_tags must be None or (progress tag, outcome tag), each [A-Za-z0-9_-]+",
                )

    def test_keyword_only_and_frozen(self):
        with self.assertRaises(TypeError):
            Spec("/w")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Spec(workdir="/w", access=Access(cwd="/w")).sid = "s"

    def test_accepts_every_tier_and_effort(self):
        for tier in (1, 2, 3, 4):
            for effort in (*Effort, "max"):
                spec = Spec(
                    workdir="/w", access=Access(cwd="/c"), tier=tier, effort=effort, sid="s-1"
                )
                self.assertEqual((spec.tier, spec.effort), (tier, effort))

    def test_refuses_each_bad_field_naming_it(self):
        sid = 'sid must be a non-empty printable string without "/", got '
        cases = (
            *(
                ({"flags": bad}, "flags must be a tuple of strings without NUL")
                for bad in (["-x"], ("-x\0",), ("-x", 1), "-x", None)
            ),
            *(
                ({"env": bad}, "env must map variable names to strings without NUL")
                for bad in (
                    {"A": 1},
                    {"A": None},
                    {"A": "b\0"},
                    {1: "a"},
                    {"": "a"},
                    {"A=": "a"},
                    {"A\0": "a"},
                    [("A", "b")],
                    "A=b",
                    None,
                )
            ),
            *(({"name": bad}, "name must be None or a string") for bad in (5, b"w", ("w",))),
            *(
                ({"workdir": bad}, f"workdir must be a printable absolute path, got {shown}")
                for bad, shown in (
                    ("w", "'w'"),
                    ("", "''"),
                    (5, "5"),
                    (None, "None"),
                    ("/w\n", r"'/w\n'"),
                    ("/w\0", r"'/w\x00'"),
                    (b"/w", "b'/w'"),
                    ("/" + "x" * 200 + "\n", "'/" + "x" * 78),
                )
            ),
            *(
                ({"tier": bad}, f"tier must be an integer 1–4, got {shown}")
                for bad, shown in ((0, "0"), (5, "5"), (True, "True"), ("2", "'2'"), (2.0, "2.0"))
            ),
            *(
                (
                    {"effort": bad},
                    f"effort must be one of low, medium, high, xhigh, max, got {shown}",
                )
                for bad, shown in (
                    ("extreme", "'extreme'"),
                    ("", "''"),
                    ("LOW", "'LOW'"),
                    (3, "3"),
                    (["low"], "['low']"),
                )
            ),
            *(
                ({"access": Access(cwd=bad)}, f"cwd must be a printable absolute path, got {shown}")
                for bad, shown in (("rel", "'rel'"), ("/c\0", r"'/c\x00'"))
            ),
            *(
                (
                    {"access": Access(cwd="/x/deploy/work", dirs=(bad,)), "client": "claude"},
                    "dirs must contain only printable absolute paths",
                )
                for bad in ("../../other/ensemble", "", "/d\n", "/d\0")
            ),
            *(
                ({"sid": bad}, sid + shown)
                for bad, shown in (
                    ("", "''"),
                    ("a/b", "'a/b'"),
                    ("a\tb", r"'a\tb'"),
                    (5, "5"),
                    ("s" * 200 + "\n", "'" + "s" * 79),
                )
            ),
        )
        for fields, message in cases:
            with self.subTest(fields=fields):
                self.refused(
                    lambda *, fields=fields: Spec(
                        **{"workdir": "/w", "access": Access(cwd="/w"), **fields}
                    ),
                    message,
                )


class SidErrorTest(Case):
    def test_none_for_a_non_empty_printable_string_without_a_slash_else_the_error_text(self):
        error = 'sid must be a non-empty printable string without "/", got '
        for sid, want in (
            *(
                (sid, None)
                for sid in ("s", "s-1", "0b6c8f9e-2a1d-4c3e-9f7a-5b4d3c2e1f00", "a b", "ü")
            ),
            ("", error + "''"),
            ("a/b", error + "'a/b'"),
            ("a\tb", error + r"'a\tb'"),
            (5, error + "5"),
            (None, error + "None"),
            ("s" * 200 + "\n", error + "'" + "s" * 79),
        ):
            with self.subTest(sid=sid):
                self.assertEqual(sid_error(sid), want)


class LaunchTest(Refusals):
    def test_keeps_the_given_strings_and_defaults_the_rest(self):
        for fields, expected in (
            ({}, (("x",), {}, (), "", "", "")),
            (
                {
                    "argv": ("x", "-y"),
                    "env": MappingProxyType({"A": "b"}),
                    "unset": ("C",),
                    "cwd": "/c",
                    "transcript": "/t",
                    "resume": "x --resume s",
                },
                (("x", "-y"), {"A": "b"}, ("C",), "/c", "/t", "x --resume s"),
            ),
        ):
            with self.subTest(fields=fields):
                launch = Launch(**{"argv": ("x",), **fields})
                self.assertEqual(
                    (
                        launch.argv,
                        launch.env,
                        launch.unset,
                        launch.cwd,
                        launch.transcript,
                        launch.resume,
                    ),
                    expected,
                )

    def test_project_override_is_optional_and_boolean(self):
        self.assertIsNone(Launch(("x",)).project)
        for value in (False, True):
            self.assertIs(Launch(("x",), project=value).project, value)
        for value in (0, 1, "yes", []):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "launch: project"):
                Launch(("x",), project=value)

    def test_refuses_argv_env_or_unset_that_is_not_strings_without_nul(self):
        for field, bads in (
            ("argv", (["x"], ("x\0",), ("x", 1), "x")),
            ("env", ({"A": 1}, {1: "a"}, {"A\0": "b"}, {"A": "b\0"}, [("A", "b")])),
            ("unset", (["A"], ("A\0",), ("A", 2))),
        ):
            for bad in bads:
                with self.subTest(field=field, bad=bad):
                    fields = {"argv": ("x",), field: bad}
                    self.refused(
                        lambda *, fields=fields: Launch(**fields),
                        f"launch: {field} must be strings without NUL",
                    )
