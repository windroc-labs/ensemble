"""Host callbacks bind their saved server and launch generation before marking."""

import os
from unittest import mock

from ensemble.session.engine.types import HookCall
from ensemble.session.hosts.tmux import hook
from tests.session.case import Case


class CallbackTest(Case):
    def setUp(self):
        self.executable = os.path.join(self.home, "executable")
        with open(self.executable, "w"):
            pass
        os.chmod(self.executable, 0o700)
        self.state = {
            "executable": self.executable,
            "target": "w1",
            "generation": "generation",
            "environment": {"TMUX_TMPDIR": self.home},
        }

    def run_callback(self, identity, process="4242 $3 %3 4343"):
        response = "" if identity is None else f"{process}\t{identity}\n"
        with (
            mock.patch.dict(
                os.environ,
                {"TMUX": "foreign", "TMUX_TMPDIR": "/foreign", "KEEP": "value"},
                clear=True,
            ),
            mock.patch(
                "subprocess.run",
                side_effect=[
                    mock.Mock(returncode=0, stdout=response, stderr=""),
                    mock.Mock(returncode=0, stdout="", stderr=""),
                ],
            ) as run,
        ):
            hook.on_hook("working", HookCall("tool", {}, "sid", self.home, None, self.state))
        return run

    def test_callback_looks_up_with_the_saved_selector_and_marks_the_pane_its_launch_recorded(
        self,
    ):
        for process, pane in (("4242 $3 %3 4343", "%3"), ("1 $1 %9 42", "%9")):
            with self.subTest(process=process):
                run = self.run_callback("sid\tgeneration", process=process)
                self.assertEqual(
                    [call.args[0] for call in run.call_args_list],
                    [
                        [
                            self.executable,
                            "display-message",
                            "-p",
                            "-t",
                            "=w1:",
                            "#{@process}\t#{@sid}\t#{@generation}",
                        ],
                        [self.executable, "set-option", "-t", pane, "@state", "working"],
                    ],
                )
                for call in run.call_args_list:
                    self.assertEqual(
                        call.kwargs["env"], {"TMUX_TMPDIR": self.home, "KEEP": "value"}
                    )

    def test_a_malformed_process_or_a_reused_name_even_with_the_same_sid_marks_nothing(self):
        cases = (
            *(("sid\tgeneration", process) for process in ("", "%9", "1 $1 %9", "1 $1 %9 42;")),
            *(
                (identity, "4242 $3 %3 4343")
                for identity in ("sid\tnext-generation", "other\tgeneration", "", None)
            ),
        )
        for identity, process in cases:
            with self.subTest(identity=identity, process=process):
                self.assertEqual(self.run_callback(identity, process=process).call_count, 1)
