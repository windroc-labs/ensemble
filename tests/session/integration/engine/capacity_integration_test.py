"""Admission races and launcher loss across real processes; no agent clients or credentials."""

import multiprocessing
import os

from ensemble.session.engine.capacity import Admission
from ensemble.session.engine.types import Liveness, Mode, SessionError
from tests.session.case import Case
from tests.session.fakes.terminal import MemoryHost


def compete(ready, release, output, name):
    ready.wait(10)
    try:
        with Admission("fake", 1, name=name, sid=name, mode=Mode.HEADLESS, hosts={}) as ticket:
            ticket.started(lambda: Liveness.LIVE)
            output.put("admitted")
            release.wait(10)
            ticket.close()
    except SessionError:
        output.put("refused")


def interrupted_tui():
    with Admission(
        "fake", 0, name="held", sid="s1", mode=Mode.TUI, hosts={"elsewhere": MemoryHost()}
    ) as ticket:
        assert os.path.isfile(ticket.path)
        os._exit(0)  # A crash after reservation cannot execute context cleanup.


class CapacityTest(Case):
    def test_simultaneous_processes_cannot_both_take_the_last_slot(self):
        context = multiprocessing.get_context("fork")
        ready, release, output = context.Event(), context.Event(), context.Queue()
        children = [
            context.Process(target=compete, args=(ready, release, output, f"n{i}"))
            for i in range(2)
        ]
        try:
            for child in children:
                child.start()
            ready.set()
            self.assertEqual(
                sorted(output.get(timeout=10) for _ in children), ["admitted", "refused"]
            )
        finally:
            release.set()
            for child in children:
                child.join(10)
                if child.is_alive():
                    child.kill()
                    child.join(10)
            output.close()
            output.join_thread()
        self.assertEqual([child.exitcode for child in children], [0, 0])

    def test_a_crashed_tui_start_stays_counted_outside_its_host_configuration(self):
        child = multiprocessing.get_context("fork").Process(target=interrupted_tui)
        child.start()
        child.join(10)
        if child.is_alive():
            child.kill()
            child.join(10)
        self.assertEqual(child.exitcode, 0)
        with (
            self.assertRaisesRegex(SessionError, "held"),
            Admission("fake", 1, name="other", sid="s2", mode=Mode.HEADLESS, hosts={}),
        ):
            self.fail("lost reservation")
        # Its matching Host can confirm that no session was ever created.
        with Admission(
            "fake", 1, name="other", sid="s2", mode=Mode.HEADLESS, hosts={"elsewhere": MemoryHost()}
        ):
            pass
