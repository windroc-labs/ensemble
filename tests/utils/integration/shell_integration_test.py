"""Human resume commands preserve exec environment names through the shell."""

import json
import os
import signal
import subprocess
import sys
import time
from unittest import mock

from ensemble.utils import probe, resume_command
from tests.utils.case import Case
from tests.utils.sandbox import clean_env, gone, kill, scratch


class ResumeEnvironment(Case):
    def test_exec_names_and_shell_metacharacters_reach_the_child_unchanged(self):
        values = {"A-B": "a b", "X;Y": "$(false); 'x'", "-x": "", "A B": "v"}
        command = resume_command(
            os.getcwd(),
            values,
            (
                sys.executable,
                "-I",
                "-c",
                "import json, os, sys; print(json.dumps({k: os.environ[k] for k in sys.argv[1:]}))",
                *values,
            ),
        )
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            env=clean_env(),
        )
        self.assertEqual(json.loads(result.stdout), values)


WAIT = 10
PROBE_TIMEOUT = 2


def until(check) -> bool:
    """Whether check() holds within WAIT s."""
    deadline = time.monotonic() + WAIT
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


class Probe(Case):
    def setUp(self):
        self.dir = scratch(self, real=False)

    def probe(self, argv, timeout=WAIT, env=None):
        """probe(argv) in self.dir, the Popen call's mock and the processes it made."""
        made, real = [], subprocess.Popen

        def popen(*args, **kwargs):
            made.append(real(*args, **kwargs))
            return made[-1]

        with mock.patch("subprocess.Popen", side_effect=popen) as spy:
            out = probe(argv, cwd=self.dir, env={**os.environ, **(env or {})}, timeout=timeout)
        return out, spy, made

    def test_its_stdout_alone_run_in_cwd_with_env_leading_its_own_group(self):
        out, spy, _ = self.probe(
            (
                sys.executable,
                "-I",
                "-c",
                "import os, sys\n"
                "print(os.getcwd())\n"
                'print(os.environ["HM_PROBE"])\n'
                "print(os.getpgrp() == os.getpid())\n"
                'sys.stderr.write("noise\\n")\n',
            ),
            env={"HM_PROBE": "probe"},
        )
        self.assertEqual(out, f"{os.path.realpath(self.dir)}\nprobe\nTrue\n")
        self.assertIs(spy.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(spy.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_a_spawn_error_is_none(self):
        for argv in ((os.path.join(self.dir, "missing"),), (sys.executable, "-c", "pass", "\0")):
            with self.subTest(argv=argv):
                self.assertIsNone(self.probe(argv)[0])

    def test_after_timeout_its_group_is_killed_it_is_reaped_and_the_result_none(self):
        pids = os.path.join(self.dir, "grandchild.json")
        out, _, made = self.probe(
            (
                sys.executable,
                "-I",
                "-c",
                "import json, os, subprocess, sys, time\n"
                "pids = sys.argv[1]\n"
                'grandchild = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])\n'
                'with open(pids + ".tmp", "w") as f:\n'
                "    json.dump(grandchild.pid, f)\n"
                'os.replace(pids + ".tmp", pids)\n'
                'print("partial", flush=True)\n'
                "time.sleep(60)\n",
                pids,
            ),
            timeout=PROBE_TIMEOUT,
        )
        with open(pids, encoding="utf-8") as f:
            grandchild = json.load(f)
        self.addCleanup(kill, grandchild)
        self.assertIsNone(out)
        self.assertEqual([proc.returncode for proc in made], [-signal.SIGKILL])
        self.assertTrue(until(lambda: gone(grandchild)), "grandchild still runs")
