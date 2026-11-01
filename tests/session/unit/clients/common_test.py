import os
import re

from ensemble.session.clients.codex import CodexClient
from ensemble.session.clients.common import (
    check_selection,
    docker_config,
    scan,
    select,
    split_flags,
)
from ensemble.session.engine.types import ConfigError
from tests.session.case import SID, Case
from tests.utils.sandbox import scratch


class FlagsTest(Case):
    def test_unvalued_short_options_retain_their_raw_spelling(self):
        for arg in ("-x", "-xattached", "-x=value", "-x="):
            with self.subTest(arg=arg):
                flag = next(scan((arg,), {"-c": "-c"}))
                self.assertEqual(flag.word, arg[:2])
                self.assertIsNone(flag.value)
                self.assertEqual((flag.arg, flag.text), (arg, arg))

    def test_aliases_and_attached_or_following_values(self):
        valued = {"-c": "-c", "--config": "-c", "--add-dir": "--add-dir"}
        flags = ("-c", "one=1", "-ctwo=2", "-c=three=3", "--config=four=4", "--add-dir", "/x")
        self.assertEqual(
            [(flag.word, flag.value, flag.text) for flag in scan(flags, valued)],
            [
                ("-c", "one=1", "-c one=1"),
                ("-c", "two=2", "-ctwo=2"),
                ("-c", "three=3", "-c=three=3"),
                ("-c", "four=4", "--config=four=4"),
                ("--add-dir", "/x", "--add-dir /x"),
            ],
        )

    def test_consumed_spans_remove_only_the_selected_option(self):
        for settings in (("--settings", "{}"), ("--settings={}",)):
            flags = ("--verbose", *settings, "--other", "--", "literal")
            with self.subTest(settings=settings):
                option = next(
                    flag
                    for flag in scan(flags, {"--settings": "--settings"})
                    if flag.word == "--settings"
                )
                self.assertEqual(option.value, "{}")
                self.assertEqual(
                    (*flags[: option.start], *flags[option.end :]),
                    ("--verbose", "--other", "--", "literal"),
                )

    def test_empty_values_do_not_consume_another_option(self):
        valued = {"-c": "-c", "--config": "-c"}
        for flags in (("-c=", "--other"), ("--config=", "--other")):
            with self.subTest(flags=flags):
                self.assertEqual(
                    [(flag.word, flag.value) for flag in scan(flags, valued)],
                    [("-c", ""), ("--other", None)],
                )
        self.assertEqual(next(scan(("-c",), valued)).text, "-c ")

    def test_a_next_value_may_be_a_flag_or_the_separator(self):
        valued = {"--add-dir": "--add-dir"}
        for value in ("--verbose", "--"):
            flags = ("--add-dir", value, "--other")
            with self.subTest(value=value):
                self.assertEqual(
                    [(flag.word, flag.value) for flag in scan(flags, valued)],
                    [("--add-dir", value), ("--other", None)],
                )
        self.assertEqual(
            split_flags(("--add-dir", "--", "--other")), (["--add-dir"], ["--", "--other"])
        )

    def test_the_literal_tail_starts_at_the_first_separator(self):
        flags = ("--verbose", "--", "--resume", "--", "prompt")
        self.assertEqual([flag.arg for flag in scan(flags)], ["--verbose"])
        self.assertEqual(split_flags(flags), (["--verbose"], ["--", "--resume", "--", "prompt"]))
        for words, expected in (
            ((), ([], [])),
            (("--", "literal"), ([], ["--", "literal"])),
            (("--verbose",), (["--verbose"], [])),
            (("--verbose", "--"), (["--verbose"], ["--"])),
        ):
            with self.subTest(words=words):
                self.assertEqual(split_flags(words), expected)

    def test_selection_retains_short_names_and_skips_option_values(self):
        for flag in ("-r", "-rsession", "-r=session", "--resume=session"):
            with self.subTest(flag=flag), self.assertRaisesRegex(ConfigError, "picks the session"):
                check_selection((flag,), ("-r", "--resume"))
        check_selection(("--model", "--resume", "--", "-r"), ("-r", "--resume"), ("--model",))
        forbidden = ("--resume", "-r", "resume", "fork", "--last")
        for flags in (("--", *forbidden), ("--resumes", "--continued", "--teleport-x")):
            with self.subTest(flags=flags):
                check_selection(flags, forbidden)

    def test_selection_words_used_as_option_values_are_not_selectors(self):
        codex = (CodexClient.forbidden_selection, CodexClient.valued_flags)
        for flags in (
            ("--add-dir", "fork"),
            ("--add-dir=resume",),
            ("--enable", "hooks"),
            ("-c", "model=x"),
            ("-cmodel=x",),
        ):
            with self.subTest(flags=flags):
                check_selection(flags, *codex)
        with self.assertRaisesRegex(ConfigError, "fork: picks the session"):
            check_selection(("--add-dir", "safe", "fork"), *codex)

    def test_selection_uses_declared_syntax_and_uuid_validation(self):
        syntax = (("--session-id",), ("--resume",))
        self.assertEqual(select(SID, syntax, uuid=True, resume=False), ("--session-id", SID))
        self.assertEqual(select(SID, syntax, uuid=True, resume=True), ("--resume", SID))
        self.assertEqual(select("native", ((), ("resume",)), resume=False), ())
        self.assertEqual(select("native", ((), ("resume",)), resume=True), ("resume", "native"))
        for native in ("abc", SID.upper(), SID + "\n"):
            with (
                self.subTest(native=native),
                self.assertRaisesRegex(ConfigError, "session ids are UUIDs"),
            ):
                select(native, syntax, uuid=True, resume=False)


class DockerConfigTest(Case):
    def setUp(self):
        self.default = os.path.join(scratch(self), "default.json")
        with open(self.default, "w") as f:
            f.write("default")
        self.path = os.path.join(os.environ["HOME"], ".ensemble", "docker-config", "x", "c.json")

    def test_the_users_file_else_the_packaged_default(self):
        self.assertEqual(docker_config("x", "c.json", self.default), (self.default, "default"))
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("mine")
        self.assertEqual(docker_config("x", "c.json", self.default), (self.path, "mine"))

    def test_a_dangling_link_is_refused_not_replaced_by_the_default(self):
        os.makedirs(os.path.dirname(self.path))
        os.symlink(os.path.join(os.path.dirname(self.path), "gone"), self.path)
        with self.assertRaisesRegex(ConfigError, f"^Docker config {re.escape(self.path)}: want"):
            docker_config("x", "c.json", self.default)
