"""The process probes on a real live, zombie and reaped child."""

import os
import subprocess

from tests.utils.case import Case
from tests.utils.sandbox import gone, kill, pid_exists, reap, running


class Probes(Case):
    def probes(self, pid):
        return pid_exists(pid), running(pid), gone(pid)

    def test_a_live_child_runs(self):
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(reap, child)
        self.assertEqual(self.probes(child.pid), (True, True, False))

    def test_a_zombie_exists_but_is_gone_and_a_reaped_pid_does_not_exist(self):
        child = subprocess.Popen(["true"])
        self.addCleanup(reap, child)
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        self.assertEqual(self.probes(child.pid), (True, False, True))
        child.wait()
        kill(child.pid)
        self.assertEqual(self.probes(child.pid), (False, False, True))
