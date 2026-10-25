"""Published examples through real parsers, stopped before operational dispatch."""

import argparse
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
