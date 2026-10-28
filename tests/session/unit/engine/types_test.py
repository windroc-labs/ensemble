import dataclasses
import inspect
import operator
import signal
import typing
from types import MappingProxyType

from ensemble import session
from ensemble.session import (
    Access,
    ConfigError,
    Effort,
    Mode,
    NotShowable,
    Resumable,
    SessionError,
    SessionStatus,
    Spec,
)
from ensemble.session.engine import types as engine_types
from ensemble.session.engine.types import (
    ClientBound,
    HookCallback,
    HookError,
    HookKind,
    Hosts,
    KeyPress,
    Launch,
    Liveness,
    NamedKey,
    Prepared,
    ProcessOptions,
    SessionInfo,
    SessionRow,
    Tier,
    TurnStarted,
)
from ensemble.session.harness.types import EventCall
from ensemble.utils import StatusState
from tests.session.case import Case


class EnvironmentSources(Case):
    def test_in_process_refuses_any_present_credential_declaration(self):
        for field in ("credentials", "environment_credentials"):
            for value in ("", "{}", "null", "{"):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(ConfigError, field + ": in_process"),
                ):
                    Spec(workdir="/w", access=Access(cwd="/w"), mode="in_process", **{field: value})

    def test_spec_accepts_both_environment_sources(self):
        spec = Spec(workdir="/w", access=Access(cwd="/w", env={"A": "1"}), env={"B": "2"})
        self.assertEqual((spec.access.env, dict(spec.env)), ({"A": "1"}, {"B": "2"}))

    def test_every_input_accepts_exec_environment_names(self):
        env = {"A-B": "a b", "1A": "", "X;Y": "v", "Ä": "v", "A B": "v", "A\n": "v", "_x1": ""}
        for build in (
            lambda **fields: Access(cwd="/c", **fields),
            ProcessOptions,
            lambda **fields: Spec(workdir="/w", access=Access(cwd="/w"), **fields),
            lambda **fields: Launch(("x",), **fields),
        ):
            with self.subTest(build=build):
                self.assertEqual(dict(build(env=env).env), env)

    def test_every_input_rejects_invalid_exec_names_and_values(self):
        for env in (
            {"": "secret"},
            {"A=B": "secret"},
            {"A\0": "secret"},
            {"A": "secret\0"},
            {1: "secret"},
            {"A": 1},
            None,
        ):
            for build in (
                lambda **fields: Access(cwd="/c", **fields),
                ProcessOptions,
                lambda **fields: Spec(workdir="/w", access=Access(cwd="/w"), **fields),
                lambda **fields: Launch(("x",), **fields),
            ):
                with self.subTest(env=env, build=build), self.assertRaises(ConfigError) as caught:
                    build(env=env)
                self.assertNotIn("secret", str(caught.exception))


class ProcessOptionsTest(Case):
    def test_options_own_an_immutable_copy_of_the_environment(self):
        source = {"TOKEN": "original"}
        options = ProcessOptions(flags=("--flag",), env=source)
        source["TOKEN"] = "changed"
        self.assertEqual((options.flags, dict(options.env)), (("--flag",), {"TOKEN": "original"}))
        with self.assertRaises(TypeError):
            options.env["TOKEN"] = "changed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            options.flags = ()


INFO = SessionInfo(sid="s", cwd="/w", project=False, transcript="", resume="")


class FrozenLaunchData(Case):
    def test_signal_mask_is_explicit_and_immutable(self):
        self.assertEqual(Launch(("x",)).blocked_signals, ())
        signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
        launch = Launch(("x",), blocked_signals=signals)
        self.assertEqual(launch.blocked_signals, signals)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            launch.blocked_signals = ()
        for invalid in (("TERM",), (True,), (0,), (-1,), [signal.SIGTERM]):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigError):
                Launch(("x",), blocked_signals=invalid)

    def test_access_launch_and_prepared_keep_a_copy_of_the_callers_env(self):
        for build in (
            lambda env: Access(cwd="/c", env=env),
            lambda env: Launch(("x",), env=env),
            lambda env: Prepared(Spec(workdir="/w", access=Access(cwd="/w")), INFO, ("x",), env),
        ):
            source = {"A": "1"}
            held = build(source)
            source["B"] = "2"
            del source["A"]
            self.assertEqual(dict(held.env), {"A": "1"})

    def test_access_launch_and_prepared_env_are_read_only(self):
        for held in (
            Access(cwd="/c"),
            Access(cwd="/c", env={"A": "1"}),
            Launch(("x",)),
            Launch(("x",), env={"A": "1"}),
            Prepared(Spec(workdir="/w", access=Access(cwd="/w")), INFO),
            Prepared(Spec(workdir="/w", access=Access(cwd="/w")), INFO, ("x",), {"A": "1"}),
        ):
            with self.subTest(held=held), self.assertRaises(TypeError):
                held.env["C"] = "3"

    def test_frozen_copies_compare_by_value(self):
        self.assertEqual(
            Access(cwd="/c", env={"A": "1"}), Access(cwd="/c", env=MappingProxyType({"A": "1"}))
        )
        self.assertNotEqual(Access(cwd="/c", env={"A": "1"}), Access(cwd="/c", env={"A": "2"}))
        self.assertEqual(Launch(("x",), env={"A": "1"}), Launch(("x",), env={"A": "1"}))
        self.assertNotEqual(Launch(("x",), env={"A": "1"}), Launch(("x",), env={"A": "2"}))
        self.assertEqual(
            Spec(workdir="/w", access=Access(cwd="/w", env={"A": "1"})),
            Spec(workdir="/w", access=Access(cwd="/w", env={"A": "1"})),
        )


class FrozenConfig(Case):
    def test_hosts_hold_a_read_only_deep_copy_of_their_tables(self):
        source = {"x": {"implementation": "p.m:X", "sizes": [1]}}
        hosts = Hosts("x", source)
        source["x"]["sizes"].append(2)
        source["y"] = {}
        self.assertEqual(hosts.tables, {"x": {"implementation": "p.m:X", "sizes": (1,)}})
        for table, key in ((hosts.tables, "y"), (hosts.tables["x"], "sizes")):
            with self.subTest(key=key), self.assertRaises(TypeError):
                operator.setitem(table, key, ())
        self.assertEqual(Hosts("x", hosts.tables), hosts)


class WhatToStart(Case):
    def test_access_cwd_is_required(self):
        with self.assertRaises(TypeError):
            Access()
        with self.assertRaises(TypeError):
            Spec(workdir="/w")
        for cwd in ("", "rel", "rel/dir"):
            with self.subTest(cwd=cwd), self.assertRaises(ConfigError) as caught:
                Spec(workdir="/w", access=Access(cwd=cwd))
            self.assertEqual(
                str(caught.exception), f"cwd must be a printable absolute path, got {cwd!r}"
            )
        self.assertEqual(Spec(workdir="/w", access=Access(cwd="/c")).access.cwd, "/c")

    def test_a_full_spec_keeps_its_fields_its_mode_a_mode(self):
        spec = Spec(
            workdir="/w",
            access=Access(cwd="/w"),
            sid="s1",
            client="fake",
            mode="tui",
            prompt="hi",
            resume=True,
            events="/w/events",
        )
        self.assertIs(spec.mode, Mode.TUI)
        self.assertEqual(
            (spec.client, spec.prompt, spec.resume, spec.events),
            ("fake", "hi", True, "/w/events"),
        )

    def test_bad_start_fields_are_config_errors(self):
        modes = ", ".join(Mode)
        for fields, message in (
            ({"resume": True}, "resume: needs spec.sid, the session to resume"),
            ({"sid": "s1", "resume": "yes"}, "resume must be a bool, got 'yes'"),
            ({"mode": "pane"}, f"mode must be one of {modes}, got 'pane'"),
            ({"mode": ""}, f"mode must be one of {modes}, got ''"),
            ({"mode": 5}, f"mode must be one of {modes}, got 5"),
            ({"client": 5}, "client must be None or a string"),
            ({"prompt": None}, "prompt must be a string"),
            ({"events": 5}, "events must be a printable path, got 5"),
            ({"events": "/a\nb"}, r"events must be a printable path, got '/a\nb'"),
            ({"handlers": [HookCallback("m")]}, "handlers must be a tuple of HookCallback"),
            ({"handlers": ("m",)}, "handlers must be a tuple of HookCallback"),
        ):
            with self.subTest(fields=fields), self.assertRaises(ConfigError) as caught:
                Spec(workdir="/w", access=Access(cwd="/w"), **fields)
            self.assertEqual(str(caught.exception), message)


class Handlers(Case):
    def test_a_spec_keeps_its_handlers_in_order(self):
        first, second = HookCallback("a.first", {"k": 1}), HookCallback("b", roots=("/r",))
        spec = Spec(workdir="/w", access=Access(cwd="/w"), handlers=(first, second))
        self.assertEqual(spec.handlers, (first, second))

    def test_an_event_call_carries_what_a_handler_needs(self):
        call = EventCall(
            session=None,
            event=TurnStarted(),
            sid="s1",
            generation="g1",
            event_id="g1:1",
            turn="g1:t1",
            state={"k": 1},
        )
        self.assertEqual(
            (call.event, call.sid, call.generation, call.event_id, call.turn, call.state),
            (TurnStarted(), "s1", "g1", "g1:1", "g1:t1", {"k": 1}),
        )
        for name in ("turn", "event_id"):
            with self.subTest(name=name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(call, name, "x")


class ReservedName(Case):
    def test_none_alone_is_no_session_name_but_a_tag(self):
        self.assertIsNone(session.SESSION_NAME.fullmatch("none"))
        for name in ("none-1", "None", "nonex", "a_none"):
            with self.subTest(name=name):
                self.assertIsNotNone(session.SESSION_NAME.fullmatch(name))
        self.assertEqual(session.SESSION_NAME_RULE, "[A-Za-z0-9_-]+ but not none")
        self.assertIsNotNone(engine_types.TAG.fullmatch("none"))
        spec = Spec(workdir="/w", access=Access(cwd="/w"), reply_tags=("none", "x"))
        self.assertEqual(spec.reply_tags, ("none", "x"))


class DisplayError(Case):
    def test_not_showable_is_a_driver_error(self):
        self.assertTrue(issubclass(NotShowable, SessionError))
        self.assertEqual(str(NotShowable("host fake cannot show")), "host fake cannot show")


class OptionSets(Case):
    def test_each_option_set_is_a_str_enum_of_its_values(self):
        for enum, values in (
            (Mode, ["headless", "tui", "in_process"]),
            (Liveness, ["live", "absent", "unknown"]),
            (Effort, ["low", "medium", "high", "xhigh", "max"]),
            (Resumable, ["yes", "no", "unknown"]),
        ):
            with self.subTest(enum=enum.__name__):
                self.assertEqual([m.value for m in enum], values)
                self.assertEqual(list(enum), values)

    def test_hook_kinds_tiers_and_hook_words_are_session_internal_str_enums(self):
        for enum, values in (
            (HookKind, ["turn_end", "turn_failed", "blocked", "activity", "turn_started"]),
            (Tier, ["1", "2", "3", "4"]),
        ):
            with self.subTest(enum=enum.__name__):
                self.assertEqual([m.value for m in enum], values)
                self.assertEqual(list(enum), values)


class CarriedEnums(Case):
    """Text is normalised once, where the value is built; the enum is what is carried."""

    def test_a_spec_carries_its_mode_and_effort_as_members(self):
        spec = Spec(workdir="/w", access=Access(cwd="/w"), mode="headless", effort="high")
        self.assertIs(spec.mode, Mode.HEADLESS)
        self.assertIs(spec.effort, Effort.HIGH)
        self.assertIs(
            Spec(workdir="/w", access=Access(cwd="/w"), effort=Effort.LOW).effort, Effort.LOW
        )
        self.assertIsNone(Spec(workdir="/w", access=Access(cwd="/w")).effort)

    def test_a_status_carries_a_resumable_given_it_or_its_plain_value(self):
        for resumable in (Resumable.YES, "yes"):
            with self.subTest(resumable=resumable):
                status = SessionStatus(resumable, None, "")
                self.assertIs(status.resumable, Resumable.YES)

    def test_a_row_carries_an_activity_state_given_it_or_its_plain_value(self):
        for state in (StatusState.DEAD, "dead"):
            with self.subTest(state=state):
                row = SessionRow("w1", None, None, state, None, None, None, own=False)
                self.assertIs(row.state, StatusState.DEAD)

    def test_an_unknown_value_names_the_allowed_ones(self):
        for build, message in (
            (
                lambda: SessionStatus("maybe", None, ""),
                "resumable must be one of yes, no, unknown, got 'maybe'",
            ),
            (
                lambda: SessionStatus(None, None, ""),
                "resumable must be one of yes, no, unknown, got None",
            ),
            (
                lambda: SessionRow("w1", None, None, "idle", None, None, None, own=False),
                "state must be one of starting, working, done, blocked, dead, gone, unknown, got 'idle'",
            ),
        ):
            with self.subTest(message=message), self.assertRaises(ConfigError) as caught:
                build()
            self.assertEqual(str(caught.exception), message)

    def test_a_tier_is_still_an_integer_one_of_the_tiers(self):
        self.assertEqual(
            [Spec(workdir="/w", access=Access(cwd="/w"), tier=int(t)).tier for t in Tier],
            [1, 2, 3, 4],
        )


class SharedTypes(Case):
    def test_the_hook_and_display_types_live_in_engine_types(self):
        for kind in (HookError, NotShowable, Tier):
            with self.subTest(kind=kind.__name__):
                self.assertEqual(kind.__module__, engine_types.__name__)
                self.assertIn(kind.__name__, engine_types.__all__)
        self.assertEqual(HookError.__bases__, (Exception,))


SID = "4f1c2b9e-0d6a-4c1e-9b7a-2e5f8a3d6c10"

FIELDS = {"sid": SID, "cwd": "/a", "project": True, "transcript": "/t/x.jsonl", "resume": "r"}


def bad_field(fields) -> str | None:
    try:
        SessionInfo.from_fields(fields, strict=True)
    except ValueError as e:
        return str(e)
    return None


class Identity(Case):
    def test_persisted_is_its_five_fields_and_no_native_id(self):
        info = SessionInfo(**FIELDS)
        self.assertEqual(info.persisted(), FIELDS)
        self.assertEqual([f.name for f in dataclasses.fields(SessionInfo)], list(FIELDS))
        self.assertNotIn("native_id", inspect.signature(SessionInfo.from_fields).parameters)

    def test_from_fields_round_trips_and_ignores_other_keys(self):
        info = SessionInfo(**FIELDS)
        self.assertEqual(SessionInfo.from_fields(info.persisted()), info)
        extra = {"ts": "t", "kind": "session", "client": "fake", "native_id": "n1", **FIELDS}
        self.assertEqual(SessionInfo.from_fields(extra), info)

    def test_missing_transcript_or_resume_is_empty(self):
        fields = {"sid": SID, "cwd": "/a", "project": False}
        self.assertEqual(
            SessionInfo.from_fields(fields),
            SessionInfo(sid=SID, cwd="/a", project=False, transcript="", resume=""),
        )

    def test_invalid_fields_are_none_never_an_error(self):
        for key, bad in (
            ("sid", ""),
            ("sid", "a/b"),
            ("sid", "a\nb"),
            ("sid", 3),
            ("sid", None),
            ("cwd", ""),
            ("cwd", "rel"),
            ("cwd", "/x\x1b[2J"),
            ("cwd", 3),
            ("cwd", None),
            ("project", 1),
            ("project", "yes"),
            ("project", None),
            ("transcript", "rel.jsonl"),
            ("transcript", "/t\n"),
            ("transcript", 3),
            ("resume", 3),
            ("resume", None),
        ):
            with self.subTest(key=key, bad=bad):
                self.assertIsNone(SessionInfo.from_fields({**FIELDS, key: bad}))
                self.assertEqual(bad_field({**FIELDS, key: bad}), key)
        for key in ("sid", "cwd", "project"):
            with self.subTest(missing=key):
                fields = {k: v for k, v in FIELDS.items() if k != key}
                self.assertIsNone(SessionInfo.from_fields(fields))
                self.assertEqual(bad_field(fields), key)
        for bad in (None, [], "x"):
            with self.subTest(fields=bad):
                self.assertIsNone(SessionInfo.from_fields(bad))

    def test_strict_names_the_first_bad_key_in_sid_cwd_transcript_project_resume_order(self):
        self.assertEqual(SessionInfo.from_fields(FIELDS, strict=True), SessionInfo(**FIELDS))
        bad = {"sid": "", "cwd": "", "transcript": "rel", "project": None, "resume": None}
        for key in bad:
            with self.subTest(first=key):
                self.assertEqual(bad_field({**FIELDS, **bad}), key)
                bad = {k: v for k, v in bad.items() if k != key}
        self.assertEqual(bad_field(None), "sid")


class Binding(Case):
    def test_a_client_binding_is_a_client_notice_never_a_session_event(self):
        self.assertEqual(ClientBound("n1").client_id, "n1")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ClientBound("n1").client_id = "n2"
        self.assertNotIn(ClientBound, typing.get_args(engine_types.SessionEvent))


class KeyNotation(Case):
    def test_a_character_or_a_named_key_after_any_modifiers_parses(self):
        cases = {
            "y": KeyPress("y"),
            "3": KeyPress("3"),
            "/": KeyPress("/"),
            "-": KeyPress("-"),
            ";": KeyPress(";"),
            " ": KeyPress(" "),
            "é": KeyPress("é"),
            "Escape": KeyPress("Escape"),
            "BTab": KeyPress("BTab"),
            "PageDown": KeyPress("PageDown"),
            "F12": KeyPress("F12"),
            "C-c": KeyPress("c", ctrl=True),
            "S-Up": KeyPress("Up", shift=True),
            "M-C-x": KeyPress("x", ctrl=True, meta=True),
            "C--": KeyPress("-", ctrl=True),
            "C-M-S-Tab": KeyPress("Tab", ctrl=True, meta=True, shift=True),
        }
        for text, press in cases.items():
            with self.subTest(text=text):
                self.assertEqual(KeyPress.parse(text), press)
        self.assertEqual(str(KeyPress.parse("M-C-x")), "C-M-x")

    def test_anything_else_is_a_config_error_naming_it(self):
        for text in ("", "C-", "S-", "C-C-c", "Esc", "escape", "F13", "c-c", "ab", "\n", "C-\t"):
            with self.subTest(text=text), self.assertRaises(ConfigError) as caught:
                KeyPress.parse(text)
            self.assertIn(repr(text)[:80], str(caught.exception))
        for value in (None, 3, b"y"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                KeyPress.parse(value)

    def test_every_named_key_is_its_own_notation(self):
        for named in NamedKey:
            with self.subTest(named=named):
                self.assertEqual(KeyPress.parse(named.value), KeyPress(named.value))


class HookCallbackTest(Case):
    def test_its_fields_are_the_session_json_descriptor(self):
        callback = engine_types.HookCallback("pkg.hooks", {"store": "/s"}, roots=("/opt/ext",))
        self.assertEqual(
            callback.fields(),
            {"module": "pkg.hooks", "state": {"store": "/s"}, "roots": ["/opt/ext"]},
        )
        self.assertEqual(
            engine_types.HookCallback("pkg").fields(), {"module": "pkg", "state": {}, "roots": []}
        )

    def test_a_bad_module_state_or_root_is_refused(self):
        for kwargs, message in (
            ({"module": "pkg/hooks"}, "hook callback module 'pkg/hooks': want a dotted name"),
            ({"module": ""}, "hook callback module '': want a dotted name"),
            ({"module": "p", "state": []}, "hook callback state: want a mapping"),
            ({"module": "p", "roots": ("rel",)}, ROOTS),
            ({"module": "p", "roots": ["/a"]}, ROOTS),
            ({"module": "p", "roots": ("/a", "/a")}, ROOTS),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigError) as caught:
                engine_types.HookCallback(**kwargs)
            self.assertEqual(str(caught.exception), message)


ROOTS = "hook callback roots: want distinct printable absolute paths"


SOURCE = "dummy@owner/repo#" + "a" * 40
ENVIRONMENT = {
    "image": "dummy-env:test",
    "gateway": "workers",
    "secrets": [{"item": "ensemble-broker-env/dummy", "file": "dummy.txt"}],
    "plugins": ["dummy-plugin", SOURCE],
    "lock": "dummy-lock",
    "prefetch": {"repo": "o/r", "ref": "main"},
    "github_repo": "o/out",
}


def environment(**changes):
    return session.EnvironmentSpec.from_fields({**ENVIRONMENT, **changes})


class EnvironmentSpecTest(Case):
    def test_both_entry_modules_export_it_and_session_busy(self):
        from ensemble.session import extension

        for module in (session, extension):
            self.assertIs(module.EnvironmentSpec, engine_types.EnvironmentSpec)
            self.assertIs(module.SessionBusy, engine_types.SessionBusy)

    def test_unknown_or_role_field_is_refused(self):
        for key in ("role", "ports", "name"):
            with self.subTest(key), self.assertRaisesRegex(ConfigError, key):
                environment(**{key: "x"})

    def test_missing_image_or_gateway_is_refused(self):
        for key in ("image", "gateway"):
            fields = {k: v for k, v in ENVIRONMENT.items() if k != key}
            with self.subTest(key), self.assertRaisesRegex(ConfigError, key):
                session.EnvironmentSpec.from_fields(fields)

    def test_a_non_mapping_is_refused(self):
        with self.assertRaises(ConfigError):
            session.EnvironmentSpec.from_fields(["image"])

    def test_bad_secret_files_and_items_are_refused(self):
        item = "ensemble-broker-env/dummy"
        for secrets in (
            [{"item": item, "file": "../x"}],
            [{"item": item, "file": ""}],
            [{"item": item, "file": "a\nb"}],
            [{"item": item, "file": "a\x85b"}],
            [{"item": item, "file": "."}],
            [{"item": item, "file": ".."}],
            [{"item": item, "file": "x" * 256}],
            [
                {"item": item, "file": "dummy.txt"},
                {"item": "ensemble-broker-env/other", "file": "dummy.txt"},
            ],
            [{"item": "ensemble-broker-bot", "file": "dummy.txt"}],
            [{"item": "ensemble-broker-env/A", "file": "dummy.txt"}],
            [{"item": item}],
            [{"item": item, "file": "dummy.txt", "value": "s3cret"}],
            [{"item": item, "file": f"f{i}"} for i in range(17)],
            ["dummy.txt"],
        ):
            with self.subTest(secrets), self.assertRaisesRegex(ConfigError, "secrets"):
                environment(secrets=secrets)

    def test_each_field_has_an_accepted_value(self):
        found = environment()
        self.assertEqual(found.image, "dummy-env:test")
        self.assertEqual(found.gateway, "workers")
        self.assertEqual(
            [dict(s) for s in found.secrets],
            [{"item": "ensemble-broker-env/dummy", "file": "dummy.txt"}],
        )
        self.assertEqual(found.plugins, ("dummy-plugin", SOURCE))
        self.assertEqual(found.lock, "dummy-lock")
        self.assertEqual(dict(found.prefetch), {"repo": "o/r", "ref": "main"})
        self.assertEqual(found.github_repo, "o/out")
        for changes in (
            {"image": session.EnvironmentSpec.WORKER},
            {"image": "sha256:" + "b" * 64},
            {"image": "registry/team/mini-op"},
            {"gateway": "mini-op_1"},
            {"secrets": []},
            {"plugins": []},
            {"lock": None},
            {"prefetch": None},
            {"prefetch": {"repo": "o/r"}},
            {"prefetch": {"repo": "o/r", "ref": "release/1.2"}},
            {"github_repo": None},
            {"github_repo": "windroc-labs/private_docs.v2"},
        ):
            with self.subTest(changes):
                environment(**changes)
        self.assertIsNone(session.EnvironmentSpec(image="x", gateway="workers").lock)

    def test_bad_values_are_refused(self):
        for key, value in (
            ("image", "Dummy:Test"),
            ("image", "localhost:5000/x"),
            ("image", "x@sha256:" + "a" * 64),
            ("image", ""),
            ("image", 3),
            ("gateway", "Workers"),
            ("gateway", ""),
            ("gateway", "a" * 64),
            ("plugins", ["dummy plugin"]),
            ("plugins", ["dummy-plugin", "dummy-plugin"]),
            ("plugins", "dummy-plugin"),
            ("lock", "A B"),
            ("lock", ""),
            ("lock", "a" * 64),
            ("prefetch", {"repo": "o"}),
            ("prefetch", {"repo": "o/.."}),
            ("prefetch", {"repo": "o/r", "ref": "--upload-pack=x"}),
            ("prefetch", {"repo": "o/r", "ref": "refs/pull/1/head"}),
            ("prefetch", {"repo": "o/r", "ref": "a" * 40}),
            ("prefetch", {"repo": "o/r", "ref": "a b"}),
            ("prefetch", {"ref": "main"}),
            ("prefetch", {"repo": "o/r", "branch": "main"}),
            ("prefetch", "o/r"),
            ("github_repo", "o"),
            ("github_repo", "o/.."),
            ("github_repo", "o/r/x"),
            ("github_repo", ""),
            ("github_repo", {"repo": "o/r"}),
        ):
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ConfigError, key):
                environment(**{key: value})

    def test_the_constructor_wants_tuples_and_freezes_mappings(self):
        with self.assertRaisesRegex(ConfigError, "plugins"):
            session.EnvironmentSpec(image="x", gateway="workers", plugins=["a"])
        secret = {"item": "ensemble-broker-env/dummy", "file": "dummy.txt"}
        prefetch = {"repo": "o/r"}
        found = session.EnvironmentSpec(
            image="x", gateway="workers", secrets=(secret,), prefetch=prefetch
        )
        secret["file"], prefetch["repo"] = "other", "o/other"
        self.assertEqual(found.secrets[0]["file"], "dummy.txt")
        self.assertEqual(found.prefetch["repo"], "o/r")
        self.assertIsInstance(found.secrets[0], MappingProxyType)
        self.assertIsInstance(found.prefetch, MappingProxyType)

    def test_persisted_round_trips(self):
        for found in (
            environment(),
            environment(lock=None, prefetch=None, secrets=[], github_repo=None),
        ):
            with self.subTest(found):
                data = found.persisted()
                self.assertEqual(set(data), set(ENVIRONMENT))
                self.assertEqual(session.EnvironmentSpec.from_fields(data), found)
        self.assertEqual(environment().persisted(), ENVIRONMENT)


class SpecEnvironmentTest(Case):
    def test_spec_keeps_or_refuses_an_environment(self):
        access = Access(cwd="/tmp")
        self.assertIsNone(Spec(workdir="/tmp", access=access).environment)
        found = environment()
        self.assertIs(Spec(workdir="/tmp", access=access, environment=found).environment, found)
        with self.assertRaisesRegex(ConfigError, "environment"):
            Spec(workdir="/tmp", access=access, environment=ENVIRONMENT)


class SessionBusyTest(Case):
    def test_it_is_a_session_error_naming_lock_and_holder(self):
        busy = session.SessionBusy("dummy-lock", "ensemble-dummy-1-ab (owner dummy-1)")
        self.assertIsInstance(busy, SessionError)
        self.assertEqual(
            (busy.lock, busy.holder), ("dummy-lock", "ensemble-dummy-1-ab (owner dummy-1)")
        )
        self.assertEqual(
            str(busy), "busy: lock dummy-lock is held by ensemble-dummy-1-ab (owner dummy-1)"
        )

    def test_its_pattern_finds_its_message_after_a_prefix(self):
        busy = session.SessionBusy("dummy-lock", "c (owner o)")
        for text in (str(busy), f"tui: {busy}", f"headless: {busy}"):
            with self.subTest(text):
                found = session.SessionBusy.PATTERN.search(text)
                self.assertEqual(found.groups(), ("dummy-lock", "c (owner o)"))
        for text in (
            "",
            "tui: did not start",
            "tui: xbusy: lock x is held by c",
            "busy: lock x is held by ",
        ):
            with self.subTest(text):
                self.assertIsNone(session.SessionBusy.PATTERN.search(text))
