"""Help text, and published examples through real parsers stopped before dispatch."""

import argparse
import contextlib
import io
import shlex
from unittest import mock


def examples(text):
    active = False
    for line in text.replace("\\\n", "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Example"):
            active = True
        elif stripped.startswith(("Recovery", "Next help", "Prerequisites")):
            active = False
        elif (
            active
            and stripped.startswith("ensemble-")
            and not any(marker in stripped for marker in ("[", "<", "..."))
        ):
            yield shlex.split(stripped)


class Parsed(Exception):
    def __init__(self, namespace):
        super().__init__()
        self.namespace = namespace


def parse_only(main, argv):
    parse = argparse.ArgumentParser.parse_args

    def capture(parser, args=None, namespace=None):
        raise Parsed(parse(parser, args, namespace))

    with mock.patch("argparse.ArgumentParser.parse_args", new=capture):
        try:
            main(argv)
        except Parsed as result:
            return result.namespace
        except SystemExit as result:
            if result.code != 0:
                raise
    return None


def help_text(case, main, argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), case.assertRaises(SystemExit) as raised:
        main(argv)
    case.assertEqual(raised.exception.code, 0)
    return out.getvalue()


def check_examples(case, text_of, names, *, check, optional=()):
    for name in names:
        published = list(examples(text_of(name)))
        if name not in optional:
            case.assertTrue(published, name)
        for command in published:
            with (
                case.subTest(name=name, command=command),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                check(command)
