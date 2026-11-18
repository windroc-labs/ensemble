"""pytest runs the tests in a supervised child: sandboxed, behind the interpreter gate."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import isolation
import pytest

from tests.utils.sandbox import supervise

REPO = Path(__file__).resolve().parent
RUNNER = "ENSEMBLE_TEST_RUNNER"
COVERAGE = "ENSEMBLE_TEST_COVERAGE"
DATA = "coverage-data"
GATE = None
MEASURED = None


def _child():
    return os.environ.get(RUNNER) == str(os.getppid())


def pytest_cmdline_main(config):
    """The parent runs no test: it hands its arguments to the child and returns the child's code."""
    if _child():
        return None
    measuring = sys.modules["coverage"].Coverage.current() if "coverage" in sys.modules else None

    def run(env, _tmp):
        child = {**env, RUNNER: str(os.getpid())}
        if measuring:
            options = ("run:source", "run:branch")
            child[COVERAGE] = json.dumps(
                [measuring.config.config_file, *map(measuring.get_option, options)]
            )
        argv = [sys.executable, "-P", "-X", "faulthandler", "-m", "pytest"]
        return subprocess.run([*argv, *config.invocation_params.args], env=child).returncode

    def collect(tmp):
        # The child saved inside the sandbox, the only place the gate lets it write.
        if measuring and (data := tmp / DATA).exists():
            measuring.combine([str(data)])

    code, leaks = supervise(
        run, REPO, prepare=lambda tmp: isolation.blockers(tmp / "blocked-bin"), collect=collect
    )
    if leaks:
        print("leaked after the run: " + "; ".join(leaks), file=sys.stderr)
    return code or int(bool(leaks))


def pytest_configure():
    global GATE, MEASURED
    if not _child():
        return
    GATE = isolation.install(REPO)
    if COVERAGE in os.environ:
        import coverage

        config_file, source, branch = json.loads(os.environ[COVERAGE])
        MEASURED = coverage.Coverage(
            data_file=os.path.join(os.environ["ENSEMBLE_TEST_SANDBOX"], DATA),
            config_file=config_file or True,
            source=source,
            branch=branch,
        )
        MEASURED.start()


def pytest_sessionfinish(session):
    if GATE and GATE.violations:
        print("\n".join(map(str, GATE.violations)), file=sys.stderr)
        session.exitstatus = session.exitstatus or 1


def pytest_unconfigure():
    if MEASURED:
        MEASURED.stop()
        MEASURED.save()


@pytest.fixture(autouse=True)
def _gated(request):
    """Each test runs as the gate's current test and fails on what the gate refused."""
    if not isinstance(request.instance, unittest.TestCase):
        raise TypeError(f"{request.node.nodeid}: the gate needs a unittest.TestCase")
    with GATE.test(request.instance):
        yield
        refused = [str(error) for error in GATE.violations]
        GATE.violations.clear()
    if refused:
        pytest.fail("\n".join(refused), pytrace=False)
