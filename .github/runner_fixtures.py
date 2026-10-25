"""Child script for runner supervision assertions."""

from pathlib import Path

HERE = Path(__file__).resolve().parent

# A real os.abort() makes macOS file a crash report per run: print faulthandler's report and exit
# 134 instead.
SCRIPT = """import faulthandler, os, subprocess, sys, unittest
sys.path.insert(0, {github!r})
import run_tests

def abort():
    sys.stderr.write("Fatal Python error: Aborted\\n\\n")
    sys.stderr.flush()
    faulthandler.dump_traceback()
    os._exit(134)

class T(unittest.TestCase):
    def test_a(self):
        if {mode!r} == "mid":
            abort()
        if {mode!r} in ("leak", "brief"):
            seconds = "60" if {mode!r} == "leak" else "1"
            marker = os.path.join(os.environ["ENSEMBLE_TEST_SANDBOX"], "marker")
            sleeper = [sys.executable, "-c", "import sys, time; time.sleep(float(sys.argv[1]))"]
            child = subprocess.Popen(
                [*sleeper, seconds, marker],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print("started", child.pid, flush=True)
        if {mode!r} == "tmux":
            subprocess.run(["tmux", "-f", os.devnull, "new-session", "-d", "sleep 60"], check=True)
        self.assertNotEqual({mode!r}, "fail")

code = run_tests.supervise("Unit x")
if code is None:
    if {mode!r} == "early":
        sys.exit(2)
    code = run_tests.run(unittest.TestLoader().loadTestsFromTestCase(T), "Unit x")
    if {mode!r} == "after":
        abort()
sys.exit(code)
"""
