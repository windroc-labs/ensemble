"""delegate/engine/harness.py: the harness entry's exit codes, its one announce, its signal mask;
the old detach path is gone.
"""

import ast
import importlib.util
import io
import os
import signal
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import ensemble.delegate
from ensemble.delegate import ConfigError, Outcome
from ensemble.delegate.engine import harness
from ensemble.delegate.engine.types import Begun, Result
from ensemble.session import SIGNALS, SessionError
from ensemble.utils import Stopped

from delegate.tests.case import Case

SID = "11111111-2222-3333-4444-555555555555"
DONE = Result(0, Outcome("done", "Title", "Summary"))
ASKING = Result(0, Outcome("needs_input", "Title", "Summary", questions=["Which?"]))
NO_OUTCOME = Result(1, None, "no outcome after 3 turn ends")


class Owner:
    """A run whose `run` returns or raises `outcome`, noting the signal mask it ran under."""

    def __init__(self, outcome, masks):
        self.outcome, self.masks = outcome, masks
        self.announced = []

    def run(self, on_event):
        self.masks.append("run")
        on_event(Begun(SID))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def announce(self, result):
        self.masks.append("announce")
        self.announced.append(result)


class Entry(Case):
    def setUp(self):
        self.masks = []
        self.handlers = {}
        self.enterContext(
            mock.patch(
                "signal.signal",
                side_effect=lambda signum, handler: self.handlers.update({signum: handler}),
            )
        )
        self.enterContext(
            mock.patch(
                "signal.pthread_sigmask",
                side_effect=lambda how, signals: self.masks.append(
                    ("unblock" if how == signal.SIG_UNBLOCK else "block", tuple(signals))
                ),
            )
        )
        self.attached = []
        self.refusal = None

    def main(self, outcome=DONE, argv=("/work",)):
        self.owner = Owner(outcome, self.masks)
        case = self

        class Delegates:
            @classmethod
            def attach(cls, workdir):
                case.attached.append(workdir)
                if case.refusal is not None:
                    raise case.refusal
                return case.owner

        err = io.StringIO()
        with redirect_stderr(err):
            code = harness.main(list(argv), delegate_cls=Delegates)
        return code, err.getvalue()

    def test_anything_but_one_workdir_exits_2_with_its_usage(self):
        for argv in ((), ("/a", "/b")):
            with self.subTest(argv=argv):
                code, err = self.main(argv=argv)
                self.assertEqual(code, 2)
                self.assertIn("usage: python3 -m ensemble.delegate.engine.harness WORKDIR", err)
        self.assertEqual((self.attached, self.handlers, self.masks), ([], {}, []))

    def test_a_refused_attach_exits_2_before_signals_or_an_outcome_line(self):
        self.refusal = ConfigError("/work: invalid delegate.json: missing metadata")
        code, err = self.main()
        self.assertEqual(
            (code, err), (2, "delegate: /work: invalid delegate.json: missing metadata\n")
        )
        self.assertEqual((self.handlers, self.masks, self.owner.announced), ({}, [], []))

    def test_exit_codes_and_the_one_announce(self):
        for result, code in ((DONE, 0), (ASKING, 0), (NO_OUTCOME, 1)):
            with self.subTest(result=result):
                self.masks.clear()
                got, err = self.main(result)
                self.assertEqual(got, code)
                self.assertEqual(self.owner.announced, [result])
                self.assertIn(f"delegate: session {SID}\n", err)

    def test_a_refused_run_exits_2_announcing_no_result(self):
        code, err = self.main(ConfigError("/work: delegate attempt was replaced"))
        self.assertEqual(code, 2)
        self.assertTrue(err.endswith("delegate: /work: delegate attempt was replaced\n"), err)
        self.assertEqual(self.owner.announced, [None])

    def test_a_signal_exits_128_plus_its_number_announcing_no_result(self):
        self.assertEqual(self.main(Stopped(128 + signal.SIGTERM))[0], 128 + signal.SIGTERM)
        self.assertEqual(self.owner.announced, [None])

    def test_another_exception_propagates_announcing_no_result(self):
        with self.assertRaises(SessionError):
            self.main(SessionError("host gone"))
        self.assertEqual(self.owner.announced, [None])

    def test_signals_are_unblocked_only_while_the_run_executes(self):
        self.main()
        self.assertEqual(self.masks, [("unblock", SIGNALS), "run", ("block", SIGNALS), "announce"])
        self.assertEqual(set(self.handlers), set(SIGNALS))
        self.masks.clear()
        with self.assertRaises(Stopped) as caught:
            self.handlers[signal.SIGHUP](signal.SIGHUP, None)
        self.assertEqual((caught.exception.code, self.masks), (128 + signal.SIGHUP, []))

    def test_a_stop_signal_leaves_signals_unblocked_and_ignores_later_ones(self):
        self.main()
        stop = self.handlers[signal.SIGTERM]
        self.masks.clear()
        with self.assertRaises(Stopped):
            stop(signal.SIGTERM, None)
        self.assertEqual(self.masks, [])
        self.assertEqual(set(self.handlers), set(SIGNALS))
        for signum in SIGNALS:
            self.assertIsNot(self.handlers[signum], stop)
            self.assertNotIn(self.handlers[signum], (signal.SIG_IGN, signal.SIG_DFL))
            self.assertIsNone(self.handlers[signum](signum, None))

    def test_the_harness_reads_no_stdin(self):
        with mock.patch("sys.stdin", mock.Mock(read=mock.Mock(side_effect=AssertionError("read")))):
            self.assertEqual(self.main()[0], 0)


class OldPath(Case):
    def test_the_old_detach_path_is_gone(self):
        for name in ("ensemble.delegate.engine.application", "ensemble.delegate.engine.detach"):
            with self.subTest(module=name):
                self.assertIsNone(importlib.util.find_spec(name))
        root = Path(os.path.dirname(ensemble.delegate.__file__))
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            texts = [
                n.value
                for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            with self.subTest(path=str(path.relative_to(root))):
                self.assertFalse([t for t in texts if "ENSEMBLE_HARNESS_" in t])
                self.assertNotIn("ensemble.delegate.engine.harness_delegate", texts)
                if path.parent.name == "engine":
                    imported = {
                        alias.name
                        for n in ast.walk(tree)
                        if isinstance(n, (ast.Import, ast.ImportFrom))
                        for alias in n.names
                    } | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
                    self.assertFalse(
                        imported & {"argparse", "Namespace", "SimpleNamespace", "types"}
                    )
