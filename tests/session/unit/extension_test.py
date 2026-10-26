"""ensemble.session.extension: the ABCs an outside Client or Host subclasses and every type their
signatures name, apart from the Session users' ensemble.session.
"""

import inspect
import typing

from ensemble import utils
from ensemble.session import extension
from ensemble.session.clients import base as clients_base
from ensemble.session.engine import types as engine_types
from ensemble.session.harness import types as harness_types
from ensemble.session.hosts import base as hosts_base
from ensemble.session.hosts import types as hosts_types
from ensemble.session.modes.base import BaseMode
from ensemble.session.modes.client_adapters import headless, in_process, tui
from tests.session.case import Case

ABCS = {
    "Client": clients_base,
    "Headless": headless,
    "JsonHeadless": headless,
    "Tui": tui,
    "InProcess": in_process,
    "Host": hosts_base,
    "Process": hosts_base,
    "Piped": hosts_base,
    "Interactive": hosts_base,
    "InteractiveProcess": hosts_base,
}
TYPES = {
    **dict.fromkeys(
        (
            "Spec",
            "Access",
            "Mode",
            "Effort",
            "EnvironmentSpec",
            "Launch",
            "HookKind",
            "HookCallback",
            "HookCall",
            "Paste",
            "Key",
            "KeyPress",
            "NamedKey",
            "ClientBound",
            "SessionEvent",
            "Activity",
            "StatusState",
            "Blocked",
            "Exited",
            "LimitHit",
            "Liveness",
            "Reply",
            "TurnEnd",
            "TurnFailed",
            "TurnStarted",
            "TuiSettings",
            "Stderr",
            "SessionInfo",
            "SessionRow",
            "SessionStatus",
            "Resumable",
            "Usage",
            "Window",
            "Hosts",
            "ConfigError",
            "SessionError",
            "SessionBusy",
            "NotShowable",
            "Unanswered",
            "WorkerMessage",
            "WorkerHook",
            "WorkerOutput",
            "WorkerExit",
        ),
        engine_types,
    ),
    "SessionView": hosts_types,
    "EventCall": harness_types,
    "CommandView": hosts_types,
    "Lines": hosts_base,
    "Record": utils,
    "Status": utils,
    "Display": utils,
}


def _named(hint) -> set:
    """The classes a type hint names, through unions and generics."""
    if isinstance(hint, type):
        return {hint}
    return {found for arg in typing.get_args(hint) for found in _named(arg)}


def _signature_types(cls: type) -> set:
    """The classes named by cls's public annotations and its public methods' and __init__'s
    signatures.
    """
    hints = [hint for name, hint in typing.get_type_hints(cls).items() if not name.startswith("_")]
    for name, member in vars(cls).items():
        function = getattr(member, "__func__", member)
        if inspect.isfunction(function) and (not name.startswith("_") or name == "__init__"):
            hints += typing.get_type_hints(function).values()
    return {found for hint in hints for found in _named(hint)}


class ExtensionTest(Case):
    def test_all_is_the_abcs_and_their_types_each_bound_to_its_home(self):
        self.assertEqual(sorted(extension.__all__), sorted({**ABCS, **TYPES}))
        for name, home in {**ABCS, **TYPES}.items():
            with self.subTest(name=name):
                self.assertIs(getattr(extension, name), getattr(home, name))

    def test_every_type_an_abc_signature_names_is_exported(self):
        exported = {getattr(extension, n) for n in extension.__all__}
        pending = [getattr(extension, n) for n in ABCS]
        seen = set()
        while pending:
            cls = pending.pop()
            if cls in seen:
                continue
            seen.add(cls)
            for found in _signature_types(cls):
                if found.__module__.startswith("ensemble."):
                    with self.subTest(cls=cls.__name__, found=found.__name__):
                        self.assertIn(found, exported)
                if found.__module__.startswith("ensemble.session."):
                    pending.append(found)

    def test_no_implementation_or_running_mode_is_exported(self):
        abcs = tuple(getattr(extension, n) for n in ABCS)
        for name in extension.__all__:
            value = getattr(extension, name)
            if inspect.isclass(value) and issubclass(value, abcs):
                with self.subTest(name=name):
                    self.assertIn(name, ABCS)
                    self.assertTrue(inspect.isabstract(value) or value is extension.Client)
        self.assertNotIn(BaseMode, {getattr(extension, n) for n in extension.__all__})
