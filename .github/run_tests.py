"""Run suites in a supervised child, annotate failures and optionally record durations."""

import collections
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ci_scope
import isolation

from tests.utils.sandbox import clean_env, kill_processes, leaked_processes, stop_tmux_servers

STATE = "RUN_TESTS_STATE"
COVERAGE = "RUN_TESTS_COVERAGE"
COVERAGE_DATA = "coverage-data"
TAIL = 40
GATE = None


def escape(text, *, prop=False):
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def annotate(title, message):
    print(f"::error title={escape(title, prop=True)}::{escape(message)}", flush=True)


def append(variable, text):
    path = os.environ.get(variable)
    if path:
        with (
            GATE.reporting(path) if GATE else contextlib.nullcontext(),
            open(path, "a", encoding="utf-8") as file,
        ):
            file.write(text)


class AnnotatingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed = []

    def startTest(self, test):
        self.guard = GATE.test(test) if GATE else None
        if self.guard:
            self.guard.__enter__()
        self.started = time.monotonic()
        append(STATE, f"start {test.id()}\n")
        super().startTest(test)

    def stopTest(self, test):
        if GATE:
            for error in GATE.violations:
                self.addError(test, (type(error), error, error.__traceback__))
            GATE.violations.clear()
        append(
            "RUN_TESTS_DURATIONS",
            json.dumps({"test": test.id(), "seconds": time.monotonic() - self.started}) + "\n",
        )
        super().stopTest(test)
        if self.guard:
            self.guard.__exit__(None, None, None)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.annotate(test, err)

    def addError(self, test, err):
        super().addError(test, err)
        self.annotate(test, err)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.annotate(test, err, detail=subtest.id().removeprefix(test.id()).strip())

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.report(test.id(), "unexpected success")

    def annotate(self, test, err, *, detail=""):
        kind, value = err[0].__name__, str(err[1]).strip()
        line = f"{kind}: {value.splitlines()[0]}" if value else kind
        self.report(test.id(), f"{detail}: {line}" if detail else line)

    def report(self, test_id, line):
        self.failed.append((test_id, line))
        annotate(test_id, line)


def run(suite, title):
    result = unittest.TextTestRunner(verbosity=2, resultclass=AnnotatingResult).run(suite)
    if GATE:
        for error in GATE.violations:
            result.report(title, str(error))
        GATE.violations.clear()
    successful = result.wasSuccessful() and not result.failed
    if result.failed:
        cells = [(test, line.replace("|", "\\|")) for test, line in result.failed]
        rows = [f"| `{test}` | {line} |" for test, line in cells]
        table = "\n".join(["| Test | Error |", "| --- | --- |", *rows])
        append("GITHUB_STEP_SUMMARY", f"### {title}: {len(rows)} failed\n\n{table}\n\n")
    append(STATE, "passed\n" if successful else "failed\n")
    return 0 if successful else 1


def crash(code, lines):
    started = [line[6:] for line in lines if line.startswith("start ")]
    last = started[-1] if started else None
    if lines[-1:] == ["passed"]:
        return (
            f"interpreter crashed after all tests passed (exit {code}); last test started: {last}"
        )
    if lines[-1:] == ["failed"]:
        return f"interpreter crashed after test failures (exit {code}); last test started: {last}"
    if last:
        return f"interpreter crashed (exit {code}) in {last}"
    return f"exited {code} before any test started"


def stack(lines):
    """faulthandler's Python stack (without the C stack 3.14 adds), else the last TAIL lines."""
    fatal = [i for i, line in enumerate(lines) if "Fatal Python error" in line]
    if not fatal:
        return lines[-TAIL:]
    block = lines[fatal[-1] :]
    end = next((i for i, line in enumerate(block) if "C stack trace" in line), len(block))
    return block[:end]


def src_trees(repo):
    return [repo / "src"]


def start_coverage(sandbox, repo):
    import coverage

    cov = coverage.Coverage(
        data_file=os.path.join(sandbox, COVERAGE_DATA),
        branch=True,
        source=[str(tree) for tree in src_trees(repo)],
        config_file=False,
    )
    cov.set_option("report:include_namespace_packages", value=True)
    cov.start()
    return cov


def supervised_child():
    return STATE in os.environ and os.environ.get("RUN_TESTS_PARENT") == str(os.getppid())


def parallel(title, commands, directory=None):
    with tempfile.TemporaryDirectory(prefix="test-workers-") as temporary:
        directory = Path(directory or temporary)

        def worker(label):
            target = directory / f"shard-{label}"
            env = {
                **os.environ,
                "RUN_TESTS_DURATIONS": str(target / "durations.jsonl"),
            }
            if os.environ.get("INTEGRATION_TIMING_DIR"):
                env["INTEGRATION_TIMING_DIR"] = str(target)
            if os.environ.get(COVERAGE):
                env[COVERAGE] = str(target / f"coverage-data.{label}")
            try:
                target.mkdir(parents=True, exist_ok=True)
                with (target / "worker.log").open("w") as log:
                    result = subprocess.run(
                        commands[label], env=env, stdout=log, stderr=subprocess.STDOUT
                    )
            except OSError as error:
                annotate(
                    f"{title} / shard {label}",
                    f"worker launch failed before any test started: {error}",
                )
                return 1
            else:
                return result.returncode

        with ThreadPoolExecutor(max_workers=len(commands)) as executor:
            results = list(executor.map(worker, commands))
        for label, code in zip(commands, results, strict=True):
            print(f"::group::{title} / shard {label} (exit {code})", flush=True)
            path = directory / f"shard-{label}/worker.log"
            if path.is_file():
                print(path.read_text(errors="replace"), end="", flush=True)
            print("::endgroup::", flush=True)
            if code:
                annotate(f"{title} / shard {label}", f"worker exited {code}")
        return int(any(results))


def unit_parallel(title, value, workers):
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", value)
    if not match or workers < 1:
        raise ValueError("unit job must be N/TOTAL with a positive worker count")
    number, total = map(int, match.groups())
    if number > total:
        raise ValueError("unit job must be numbered 1 through TOTAL")
    commands = {
        str(label): [
            sys.executable,
            "-P",
            "-X",
            "faulthandler",
            __file__,
            f"{title} / shard {label}",
            "--unit",
            "--shard",
            f"{label}/{total * workers}",
        ]
        for label in range((number - 1) * workers + 1, number * workers + 1)
    }
    return parallel(title, commands, os.environ.get("RUN_TESTS_REPORT_DIR"))


def supervise(title):
    """None in the child; in the parent, the child's exit code (128 + signal for a signal), or 1 when
    it passed but left processes or tmux servers behind.
    """
    if supervised_child():
        return None
    tail = collections.deque(maxlen=5 * TAIL)
    repo = Path(__file__).resolve().parents[1]
    with (
        tempfile.TemporaryDirectory(
            dir=os.environ.get("ENSEMBLE_TEST_SANDBOX") or os.path.realpath("/tmp")
        ) as tmp,
        tempfile.TemporaryDirectory(prefix=".test-sandbox-", dir=repo) as workspace,
    ):
        state = os.path.join(tmp, "state")
        open(state, "w").close()
        roots = [
            repo,
            repo / ".github",
            *src_trees(repo),
            sysconfig.get_path("purelib"),
            sysconfig.get_path("platlib"),
        ]
        env = clean_env(
            {**os.environ, "HOME": workspace, "ENSEMBLE_TEST_WORKSPACE": workspace}, *roots
        )
        env.update(
            HOME=workspace,
            TMPDIR=tmp,
            TMUX_TMPDIR=tmp,
            TUI_ATTACH_PREFIX="",
            ENSEMBLE_TEST_SANDBOX=tmp,
            ENSEMBLE_CONTROL_ROOT=tmp,
            ENSEMBLE_TEST_TMP=workspace,
            ENSEMBLE_TEST_WORKSPACE=workspace,
        )
        isolation.blockers(Path(tmp) / "blocked-bin")
        if os.environ.get(COVERAGE):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(os.environ[COVERAGE])
        try:
            with subprocess.Popen(
                [sys.executable, "-P", "-S", "-X", "faulthandler", *sys.orig_argv[1:]],
                env={**env, STATE: state, "RUN_TESTS_PARENT": str(os.getpid())},
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            ) as child:
                for line in child.stderr:
                    sys.stderr.write(line)
                    tail.append(line)
        finally:
            servers = stop_tmux_servers(tmp, env)
        processes = leaked_processes((tmp, workspace))
        kill_processes(processes)
        with open(state, encoding="utf-8") as file:
            lines = file.read().splitlines()
        data = os.path.join(tmp, COVERAGE_DATA)
        if os.environ.get(COVERAGE) and os.path.exists(data):
            shutil.copyfile(data, os.environ[COVERAGE])
    code = 128 - child.returncode if child.returncode < 0 else child.returncode
    if code and not (code == 1 and lines[-1:] == ["failed"]):
        message = crash(code, lines)
        annotate(title, message + "\n" + "".join(stack(list(tail))))
        append("GITHUB_STEP_SUMMARY", f"### {title}\n\n{message}\n\n")
    leaks = [f"tmux server {socket}" for socket in servers]
    leaks += [f"pid {pid}: {args[:200]}" for pid, _, _, args in processes]
    if leaks:
        annotate(title, "leaked after the run: " + "; ".join(leaks))
        items = "".join(f"- {leak}\n" for leak in leaks)
        append("GITHUB_STEP_SUMMARY", f"### {title}: leaked processes\n\n{items}\n")
        code = code or 1
    return code


def discovery_top(root, start):
    root = root.resolve()
    start = Path(start).resolve()
    if start == root / ".github":
        return start
    return root


def discover(root, directories):
    suite = unittest.TestSuite()
    for directory in directories:
        start = root / directory
        pattern = start.name if start.is_file() else "*_test.py"
        start = start.parent if start.is_file() else start
        loader = unittest.TestLoader()
        suite.addTests(
            loader.discover(
                str(start), pattern=pattern, top_level_dir=str(discovery_top(root, start))
            )
        )
        if loader.errors:
            raise ValueError("\n".join(loader.errors))
    return suite


def tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from tests(test)
        else:
            yield test


def select(root, selectors):
    root = root.resolve()
    paths = [(root / value).resolve() for value in selectors]
    test_id = None
    if len(selectors) == 1 and not paths[0].exists() and "/" not in selectors[0]:
        test_id = selectors[0]
        paths = []
        for pattern in (".github/*_test.py", "tests/**/*_test.py"):
            for path in sorted(root.glob(pattern)):
                top = discovery_top(root, path.parent)
                module = ".".join(path.relative_to(top).with_suffix("").parts)
                if test_id.startswith(module + "."):
                    paths.append(path.resolve())
        if len(paths) > 1:
            raise ValueError(f"ambiguous test id: {test_id!r}")
    if not paths or any(
        not path.is_relative_to(root)
        or not path.exists()
        or (not path.is_dir() and not path.name.endswith("_test.py"))
        for path in paths
    ):
        raise ValueError(f"invalid test selection: {selectors!r}")
    if GATE:
        units = [
            "unit" in path.relative_to(root).parts
            or path.parent == root / ".github"
            or path == root / ".github"
            for path in paths
        ]
        GATE.unit_only, GATE.unit = all(units), any(units)
    suite = discover(root, paths)
    if test_id:
        matches = [test for test in tests(suite) if test.id() == test_id]
        if len(matches) != 1:
            raise ValueError(
                f"test id must match exactly one test: {test_id!r} ({len(matches)} matches)"
            )
        suite = unittest.TestSuite(matches)
    if not suite.countTestCases():
        raise ValueError(f"no tests match: {selectors!r}")
    return suite, paths


def unit_directories(root):
    return [
        "tests/unit",
        *sorted(
            str(path.relative_to(root))
            for pattern in ("tests/*/unit",)
            for path in root.glob(pattern)
            if (path / "__init__.py").is_file()
        ),
        ".github",
    ]


def shard(suite, value):
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", value)
    found = sorted(tests(suite), key=lambda test: test.id())
    if not match:
        raise ValueError("test shard must be N/TOTAL, numbered 1 through TOTAL")
    number, total = map(int, match.groups())
    if not 1 <= number <= total <= len(found):
        raise ValueError("test shards must be nonempty, numbered 1 through TOTAL")
    if len({test.id() for test in found}) != len(found):
        raise ValueError("test discovery contains duplicate test IDs")
    return unittest.TestSuite(found[number - 1 :: total])


def main(argv):
    global GATE
    if len(argv) < 2:
        sys.exit("usage: run_tests.py <title> <directory>... | <title> <test id|test file|--unit>")
    title, *directories = argv
    if (
        len(directories) == 5
        and directories[:2] == ["--unit", "--parallel"]
        and directories[3] == "--shard"
    ):
        return unit_parallel(title, directories[4], int(directories[2]))
    code = supervise(title)
    if code is not None:
        return code
    root = Path.cwd()
    cov = (
        start_coverage(os.environ["ENSEMBLE_TEST_SANDBOX"], Path(__file__).resolve().parents[1])
        if os.environ.get(COVERAGE)
        else None
    )
    GATE = isolation.install(root)
    part = None
    if len(directories) >= 3 and directories[-2] == "--shard":
        part = directories[-1]
        directories = directories[:-2]
    suite, _ = select(
        root,
        ci_scope.selected(root, unit_directories(root))
        if directories == ["--unit"]
        else directories,
    )
    if part is not None:
        suite = shard(suite, part)
    code = run(suite, title)
    if cov:
        cov.stop()
        cov.save()
    return code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ValueError as error:
        sys.exit(str(error))
