"""Exercise irreversible audit hooks in disposable interpreters before real state is reached."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.case import REPO, Case
from tests.session import live_tmux
from tests.utils.sandbox import clean_env, gone

SCRIPT = """import json, os, subprocess, sys, tempfile
from pathlib import Path
from unittest import mock
import isolation

root, sandbox, marker = map(Path, sys.argv[1:])
workspace = marker.parent / "workspace"
workspace.mkdir()
gate = isolation.Gate(root, sandbox, workspace)
gate.current = "sentinel.test"
sys.addaudithook(gate.audit)
blocked = []
for event in isolation.DENIED:
    try:
        sys.audit(event)
    except RuntimeError as error:
        assert "sentinel.test" in str(error) and "forbidden " + event in str(error)
        assert "File " in str(error)
        blocked.append(event)
try:
    subprocess.Popen([sys.executable, "-I", "-c", "raise SystemExit(99)"])
except RuntimeError as error:
    assert "forbidden subprocess.Popen" in str(error)
else:
    raise AssertionError("unit process escaped")
try:
    marker.write_text("escape")
except RuntimeError as error:
    assert "write outside temporary directory" in str(error)
else:
    raise AssertionError("unit write escaped")
(sandbox / "owned").write_text("allowed")
(workspace / "owned").write_text("allowed")
summary = marker.parent / "summary"
with gate.reporting(summary):
    summary.write_text("reported")
    try:
        sys.audit("os.kill")
    except RuntimeError:
        pass
    else:
        raise AssertionError("reporting bypassed the process gate")
try:
    marker.write_text("escape")
except RuntimeError:
    pass
else:
    raise AssertionError("reporting bypassed the write gate")
gate.unit = False
(sandbox / "linked").symlink_to(sandbox, target_is_directory=True)
bad = [
    ("tmux", ["tmux", "ls"], {}),
    ("tmux", ["tmux", "ls"], {"TMUX_TMPDIR": str(sandbox / "missing")}),
    ("tmux", ["tmux", "ls"], {"TMUX_TMPDIR": str(sandbox / "linked")}),
    ("tmux", ["tmux", "ls"], {"TMUX_TMPDIR": str(sandbox / "owned")}),
    ("tmux", ["tmux", "-S", str(marker), "ls"], {}),
    ("tmux", ["tmux", "-S" + str(marker), "ls"], {"TMUX_TMPDIR": str(sandbox)}),
    ("tmux", ["tmux", "-uS", str(marker), "ls"], {"TMUX_TMPDIR": str(sandbox)}),
    ("tmux", ["tmux", "-uS" + str(marker), "ls"], {"TMUX_TMPDIR": str(sandbox)}),
    ("tmux", ["tmux", "-uL../../outside", "ls"], {"TMUX_TMPDIR": str(sandbox)}),
    ("tmux", ["tmux", "ls"], {"TMUX_TMPDIR": str(sandbox), "TMUX": str(marker) + ",1,0"}),
    ("git", ["git", "status"], {"GIT_DIR": str(marker)}),
]
for exe, argv, env in bad:
    try:
        sys.audit("subprocess.Popen", exe, argv, None, env)
    except RuntimeError:
        pass
    else:
        raise AssertionError("integration tool escaped")
with mock.patch("os.getuid", return_value=os.getuid() + 1):
    try:
        sys.audit("subprocess.Popen", "tmux", ["tmux", "ls"], None, {"TMUX_TMPDIR": str(sandbox)})
    except RuntimeError:
        pass
    else:
        raise AssertionError("tmux directory with a foreign owner escaped")
sys.audit("subprocess.Popen", "tmux", ["tmux", "-S", str(sandbox / "socket"), "ls"], None, {})
sys.audit("subprocess.Popen", "tmux", ["tmux", "-uS", str(sandbox / "socket"), "ls"], None, {})
sys.audit("subprocess.Popen", "tmux", ["tmux", "-uS" + str(sandbox / "socket"), "ls"], None, {})
sys.audit("subprocess.Popen", "tmux", ["tmux", "-uLprivate", "ls"], None, {"TMUX_TMPDIR": str(sandbox)})
sys.audit("subprocess.Popen", "tmux", ["tmux", "ls"], None, {"TMUX_TMPDIR": str(sandbox)})
sys.audit("subprocess.Popen", "tmux", ["tmux", "capture-pane", "-p", "-S", "-40"], None, {"TMUX_TMPDIR": str(sandbox)})
sys.audit("subprocess.Popen", "git", ["git", "status"], None, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
sys.audit("os.exec", "tmux", ["tmux", "-S", str(sandbox / "socket"), "ls"], None)
sys.audit("os.exec", "git", ["git", "status"], None)
os.environ["GIT_DIR"] = str(marker)
try:
    sys.audit("os.exec", "git", ["git", "status"], None)
except RuntimeError as error:
    assert "git requires clean environment" in str(error)
else:
    raise AssertionError("exec inherited unsafe Git environment")
del os.environ["GIT_DIR"]
for owned in (sandbox, workspace):
    tempfile.mkdtemp(prefix="apm-group-", dir=owned)
try:
    tempfile.mkdtemp(prefix="apm-group-", dir=marker.parent)
except RuntimeError as error:
    assert "process controller outside test sandbox: " + str(marker.parent) in str(error)
else:
    raise AssertionError("process controller directory escaped the sandbox")
print(json.dumps(sorted(blocked)))
"""

CLEAN_ENV_SCRIPT = """import json, tempfile, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tests.utils.sandbox import clean_env

barrier = threading.Barrier(8)
def allocate():
    barrier.wait(timeout=5)
    env = clean_env({"TMUX": "/outside,1,0", "TMUX_TMPDIR": "/outside",
                     "ENSEMBLE_TEST_SANDBOX": "/outside"})
    assert "TMUX" not in env
    return env["TMUX_TMPDIR"]
with ThreadPoolExecutor(max_workers=8) as executor:
    futures = [executor.submit(allocate) for _ in range(8)]
    directories = [future.result() for future in futures]
assert len(set(directories)) == 1, directories
directory = Path(directories[0])
assert clean_env()["TMUX_TMPDIR"] == str(directory)
assert directory.is_dir()
assert list(directory.iterdir()) == []
assert directory.stat().st_mode & 0o777 == 0o700
print(json.dumps({"directory": str(directory), "system": tempfile.gettempdir()}))
"""

REPORTS = ("RUN_TESTS_STATE", "GITHUB_STEP_SUMMARY", "RUN_TESTS_DURATIONS", "RUN_TESTS_COVERAGE")


def run_suite(root, title, *directories, roots=(), timeout=10, **variables):
    env = clean_env(None, REPO, str(Path(REPO, ".github")), *roots)
    for name in REPORTS:
        env.pop(name, None)
    env.update(variables)
    return subprocess.run(
        [sys.executable, "-P", "-S", str(Path(REPO, ".github/run_tests.py")), title, *directories],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def errors(stdout):
    """(title, first message line) of each ::error annotation run_tests.py printed."""
    found = []
    for line in stdout.splitlines():
        if line.startswith("::error title="):
            title, message = line.removeprefix("::error title=").split("::", 1)
            found.append((title, message.split("%0A")[0]))
    return found


class SandboxGates(Case):
    def assert_gone(self, pids, what):
        for pid in pids:
            deadline = time.monotonic() + 3
            while not gone(pid, timeout=5):
                if time.monotonic() >= deadline:
                    self.fail(f"{what} tmux server {pid} remains live")
                time.sleep(0.01)

    def test_clean_env_children_cannot_reach_the_parents_private_default_tmux_server(self):
        server = live_tmux.Server(self, self.home)
        server.start("parent")
        before = server.tmux("ls")
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertIn("parent:", before.stdout)
        env = clean_env({**server.env(), **server.inside("parent")})
        directory = Path(env["TMUX_TMPDIR"])
        self.assertNotIn("TMUX", env)
        self.assertNotIn("TMUX_PANE", env)
        self.assertNotEqual(directory, Path(server.root))
        self.assertEqual(
            directory.parent.resolve(),
            Path(os.environ.get("ENSEMBLE_TEST_SANDBOX") or tempfile.gettempdir()).resolve(),
        )
        self.assertTrue(directory.is_dir())
        self.assertEqual(list(directory.iterdir()), [])
        child = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import subprocess, sys; raise SystemExit(subprocess.call([sys.argv[1], 'ls']))",
                shutil.which("tmux"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout, "")
        after = server.tmux("ls")
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(after.stdout, before.stdout)

    def test_clean_env_reuses_a_process_directory_and_respects_its_cleanup_owner(self):
        sandbox = Path(self.home, "owned")
        sandbox.mkdir()
        for mode in ("sandbox", "fallback"):
            with self.subTest(mode=mode):
                env = clean_env(None, REPO)
                if mode == "sandbox":
                    env["ENSEMBLE_TEST_SANDBOX"] = str(sandbox)
                else:
                    env.pop("ENSEMBLE_TEST_SANDBOX", None)
                directories = []
                for index in range(2):
                    proc = subprocess.run(
                        [sys.executable, "-P", "-S", "-c", CLEAN_ENV_SCRIPT],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    found = json.loads(proc.stdout)
                    directory = Path(found["directory"])
                    self.assertNotIn(directory, directories)
                    directories.append(directory)
                    if mode == "sandbox":
                        self.assertEqual(directory.parent.resolve(), sandbox.resolve())
                        self.assertTrue(directory.is_dir())
                        self.assertEqual(list(directory.iterdir()), [])
                        self.assertEqual(len(list(sandbox.iterdir())), index + 1)
                    else:
                        self.assertEqual(
                            directory.parent.resolve(), Path(found["system"]).resolve()
                        )
                        self.assertFalse(directory.exists())

    def test_forked_clean_env_children_cannot_remove_the_parents_fallback_directory(self):
        script = """import json, os, sys
from pathlib import Path
from tests.utils.sandbox import clean_env

parent = Path(clean_env()["TMUX_TMPDIR"])
reader, writer = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(reader)
    child = Path(clean_env()["TMUX_TMPDIR"])
    assert child != parent
    assert child.is_dir()
    os.write(writer, str(child).encode())
    os.close(writer)
    sys.exit(0)
os.close(writer)
with os.fdopen(reader) as stream:
    child = Path(stream.read())
_, status = os.waitpid(pid, 0)
assert os.waitstatus_to_exitcode(status) == 0, status
assert not child.exists(), child
assert parent.is_dir(), parent
assert clean_env()["TMUX_TMPDIR"] == str(parent)
print(json.dumps(str(parent)))
"""
        env = clean_env(None, REPO)
        env.pop("ENSEMBLE_TEST_SANDBOX", None)
        proc = subprocess.run(
            [sys.executable, "-P", "-S", "-c", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(Path(json.loads(proc.stdout)).exists())

    def test_clean_env_recreates_a_cached_directory_when_it_is_removed(self):
        script = """import json, shutil
from pathlib import Path
from tests.utils.sandbox import clean_env

first = Path(clean_env()["TMUX_TMPDIR"])
shutil.rmtree(first)
second = Path(clean_env()["TMUX_TMPDIR"])
assert second != first
assert not first.exists()
assert second.is_dir()
assert list(second.iterdir()) == []
assert second.stat().st_mode & 0o777 == 0o700
assert clean_env()["TMUX_TMPDIR"] == str(second)
print(json.dumps(str(second)))
"""
        for mode in ("sandbox", "fallback"):
            with self.subTest(mode=mode):
                env = clean_env(None, REPO)
                if mode == "fallback":
                    env.pop("ENSEMBLE_TEST_SANDBOX", None)
                proc = subprocess.run(
                    [sys.executable, "-P", "-S", "-c", script],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                directory = Path(json.loads(proc.stdout))
                self.assertEqual(directory.exists(), mode == "sandbox")

    def test_fallback_cleanup_stops_owned_servers_and_skips_linked_servers(self):
        kept = live_tmux.Server(self, self.home)
        kept.start("kept")
        script = """import json, os, subprocess, sys
from pathlib import Path
from tests.utils.sandbox import clean_env

env = clean_env()
directory = Path(env["TMUX_TMPDIR"])
pids = []
for label in ("default", "named"):
    command = ["tmux"] + ([] if label == "default" else ["-L", label])
    subprocess.run([*command, "-f", os.devnull, "new-session", "-d", "sleep 300"], env=env, check=True)
    pids.append(subprocess.check_output([*command, "display-message", "-p", "#{pid}"], env=env, text=True).strip())
socket = Path(sys.argv[1])
(directory / "linked-directory").symlink_to(socket.parent, target_is_directory=True)
sockets = directory / f"tmux-{os.getuid()}"
(sockets / "linked-socket").symlink_to(socket)
(sockets / "regular-file").write_text("not a socket")
print(json.dumps({"directory": str(directory), "pids": pids}))
"""
        env = clean_env(None, REPO)
        env.pop("ENSEMBLE_TEST_SANDBOX", None)
        env["TMPDIR"] = os.environ["ENSEMBLE_TEST_SANDBOX"]
        proc = subprocess.run(
            [sys.executable, "-P", "-S", "-c", script, kept.socket],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        found = json.loads(proc.stdout)
        self.assertEqual(Path(found["directory"]).parent, Path(env["TMPDIR"]))
        self.assertFalse(Path(found["directory"]).exists())
        self.assertEqual(len(found["pids"]), 2)
        self.assert_gone(found["pids"], "fallback")
        after = kept.tmux("has-session", "-t", "=kept")
        self.assertEqual(after.returncode, 0, after.stderr)

    def test_fallback_exit_cleanup_ignores_a_daemon_held_allocation_lock(self):
        script = """import json, threading
from tests.utils import sandbox

directory = sandbox.clean_env()["TMUX_TMPDIR"]
locked = threading.Event()
def hold_lock():
    with sandbox._TMUX_LOCK:
        locked.set()
        threading.Event().wait()
threading.Thread(target=hold_lock, daemon=True).start()
assert locked.wait(timeout=5)
print(json.dumps(directory))
"""
        env = clean_env(None, REPO)
        env.pop("ENSEMBLE_TEST_SANDBOX", None)
        env["TMPDIR"] = os.environ["ENSEMBLE_TEST_SANDBOX"]
        proc = subprocess.run(
            [sys.executable, "-P", "-S", "-c", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        directory = Path(json.loads(proc.stdout))
        self.assertEqual(directory.parent, Path(env["TMPDIR"]))
        self.assertFalse(directory.exists())

    def test_supervisor_removes_owned_temporary_roots_after_child_sigkill(self):
        root = Path(self.home)
        package = root / "integration"
        package.mkdir()
        (package / "__init__.py").touch()
        marker = root / "fixture-paths"
        (package / "kill_test.py").write_text(
            "import json, os, signal, tempfile, unittest\nfrom pathlib import Path\n"
            "class Killed(unittest.TestCase):\n"
            "    def test_kill(self):\n"
            "        sandbox = Path(os.environ['ENSEMBLE_TEST_SANDBOX'])\n"
            "        fixtures = [tempfile.mkdtemp(prefix=prefix) for prefix in ('fallback-', 'supervisor-')]\n"
            f"        Path({str(marker)!r}).write_text(json.dumps([str(sandbox), *fixtures]))\n"
            "        os.kill(os.getpid(), signal.SIGKILL)\n"
        )
        proc = run_suite(root, "Killed fixture", "integration")
        self.assertEqual(proc.returncode, 137, proc.stderr)
        sandbox, *fixtures = map(Path, json.loads(marker.read_text()))
        self.assertEqual(sandbox.parent, Path(os.environ["ENSEMBLE_TEST_SANDBOX"]))
        self.assertEqual(len(fixtures), 2)
        for fixture in fixtures:
            self.assertEqual(fixture.parent, sandbox)
            self.assertFalse(fixture.exists())
        self.assertFalse(sandbox.exists())

    def test_violations_after_the_last_stop_test_fail_the_run_with_their_origin(self):
        fixture = """import sys, threading, unittest


def swallowed_violation():
    try:
        sys.audit("socket.bind")
    except RuntimeError:
        pass


class Tail(unittest.TestCase):
    def test_pass(self):
        pass

    @classmethod
    def tearDownClass(cls):
        if SCOPE == "class":
            swallowed_violation()


def tearDownModule():
    if SCOPE == "module":
        swallowed_violation()


def late_thread():
    swallowed_violation()


class LateSuite(unittest.TestSuite):
    def run(self, result, debug=False):
        result = super().run(result, debug)
        if SCOPE == "thread":
            thread = threading.Thread(target=late_thread)
            thread.start()
            thread.join()
        return result


def load_tests(loader, tests, pattern):
    return LateSuite(tests)
"""
        for scope, origin in (
            ("class", "tearDownClass"),
            ("module", "tearDownModule"),
            ("thread", "late_thread"),
        ):
            with self.subTest(scope=scope):
                root = Path(self.home, scope)
                package = root / "unit"
                package.mkdir(parents=True)
                (package / "__init__.py").touch()
                (package / "tail_test.py").write_text(f"SCOPE = {scope!r}\n{fixture}")
                proc = run_suite(root, "Tail fixture", "unit")
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(
                    errors(proc.stdout),
                    [("Tail fixture", "test isolation: test discovery: forbidden socket.bind")],
                )
                self.assertIn(origin, proc.stdout)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is unavailable")
    def test_supervisor_stops_all_of_its_private_tmux_servers(self):
        root = Path(self.home)
        package = root / "integration"
        package.mkdir()
        (package / "__init__.py").touch()
        marker = root / "server-pids"
        (package / "server_test.py").write_text(
            "import json, os, subprocess, unittest\nfrom pathlib import Path\n"
            "from tests.utils.sandbox import clean_env\n"
            "class Server(unittest.TestCase):\n"
            "    def test_private(self):\n"
            "        pids = {}\n"
            "        for label in ('default', 'named', 'other'):\n"
            "            command = ['tmux'] + ([] if label == 'default' else ['-L', label])\n"
            "            subprocess.run([*command, '-f', os.devnull, 'new-session', '-d', 'sleep 300'], check=True)\n"
            "            pids[label] = subprocess.check_output([*command, 'display-message', '-p', '#{pid}'], text=True).strip()\n"
            "        env = clean_env()\n"
            "        subprocess.run(['tmux', '-f', os.devnull, 'new-session', '-d', 'sleep 300'], env=env, check=True)\n"
            "        pids['clean_env'] = subprocess.check_output(['tmux', 'display-message', '-p', '#{pid}'], env=env, text=True).strip()\n"
            f"        Path({str(marker)!r}).write_text(json.dumps(pids))\n"
        )
        proc = run_suite(
            root,
            "Private tmux",
            "integration",
            timeout=15,
            TMPDIR=os.environ["ENSEMBLE_TEST_SANDBOX"],
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        leaks = [line for line in proc.stdout.splitlines() if "leaked after the run: " in line]
        self.assertEqual(len(leaks), 1, proc.stdout)
        self.assertEqual(leaks[0].count("tmux server "), 4, leaks[0])
        pids = json.loads(marker.read_text())
        self.assertEqual(set(pids), {"default", "named", "other", "clean_env"})
        self.assert_gone(pids.values(), "private")

    def test_caught_and_background_violations_still_fail_at_stop_test(self):
        package = Path(self.home, "unit")
        package.mkdir()
        (package / "__init__.py").touch()
        (package / "swallowed_test.py").write_text(
            """import sys, threading, unittest


def swallow(event):
    try:
        sys.audit(event)
    except Exception:
        pass


swallow("socket.getaddrinfo")


class Discovery(unittest.TestCase):
    def test_discovery(self):
        pass


class Swallowed(unittest.TestCase):
    def test_caught(self):
        swallow("os.kill")

    def test_background(self):
        thread = threading.Thread(target=swallow, args=("socket.bind",))
        thread.start()
        thread.join()
"""
        )
        proc = run_suite(self.home, "Swallowed fixture", "unit")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        discovery = "unit.swallowed_test.Discovery.test_discovery"
        background = "unit.swallowed_test.Swallowed.test_background"
        caught = "unit.swallowed_test.Swallowed.test_caught"
        self.assertEqual(
            errors(proc.stdout),
            [
                (
                    discovery,
                    "RuntimeError: test isolation: test discovery: forbidden socket.getaddrinfo",
                ),
                (background, f"RuntimeError: test isolation: {background}: forbidden socket.bind"),
                (caught, f"RuntimeError: test isolation: {caught}: forbidden os.kill"),
            ],
        )
        self.assertIn("test discovery: forbidden socket.getaddrinfo", proc.stderr)
        self.assertIn("Swallowed.test_caught: forbidden os.kill", proc.stderr)
        self.assertIn("Swallowed.test_background: forbidden socket.bind", proc.stderr)

    def test_unit_selection_keeps_external_testcase_classes_under_the_unit_gate(self):
        root = Path(self.home)
        (root / ".github").mkdir()
        for package in (root / "tests", root / "tests/unit"):
            package.mkdir()
            (package / "__init__.py").touch()
        marker = root / "escaped"
        helper = root / "helper.py"
        helper.write_text(
            "import os, unittest\n"
            "class External(unittest.TestCase):\n"
            "    def test_write(self):\n"
            f"        os.mkdir({str(marker)!r})\n"
        )
        (root / ".github/external_test.py").write_text(
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('helper', {str(helper)!r})\n"
            "helper = importlib.util.module_from_spec(spec)\n"
            "sys.modules['helper'] = helper\n"
            "spec.loader.exec_module(helper)\n"
            "External = helper.External\n"
        )
        proc = run_suite(root, "Unit", "--unit")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("helper.External.test_write", proc.stderr)
        self.assertIn("write outside temporary directory", proc.stderr)
        self.assertFalse(marker.exists())

    def test_every_filesystem_mutation_is_confined_to_owned_roots(self):
        root = Path(self.home)
        outside = root / "outside"
        outside.mkdir()
        source = outside / "source"
        source.write_text("original")
        (outside / "directory").mkdir()
        package = root / "integration/unit"
        package.mkdir(parents=True)
        (package.parent / "__init__.py").touch()
        (package / "__init__.py").touch()
        # The fds open at import: discovering a non-unit dir is ungated, while the test under
        # integration/unit runs unit-gated, as the old child opened them before its Gate.
        (package / "fs_test.py").write_text(f"""import os, shutil, sqlite3, tempfile, unittest
from pathlib import Path

outside = Path({str(outside)!r})
source = outside / "source"
directory = outside / "directory"
fd = os.open(source, os.O_RDWR)
dir_fd = os.open(outside, os.O_RDONLY)


class Filesystem(unittest.TestCase):
    def test_confined(self):
        sandbox = Path(tempfile.mkdtemp())
        owned = sandbox / "owned"
        owned.write_text("owned")
        operations = [
            ("mkdir", lambda: os.mkdir(outside / "new")),
            ("rename source", lambda: os.rename(source, sandbox / "renamed")),
            ("rename destination", lambda: os.rename(owned, outside / "renamed")),
            ("replace", lambda: os.replace(owned, source)),
            ("move", lambda: shutil.move(owned, outside / "moved")),
            ("remove", lambda: os.remove(source)),
            ("rmdir", lambda: os.rmdir(directory)),
            ("rmtree", lambda: shutil.rmtree(directory)),
            ("chmod", lambda: os.chmod(source, 0o777)),
            ("chown", lambda: os.chown(source, os.getuid(), os.getgid())),
            ("symlink destination", lambda: os.symlink(owned, outside / "link")),
            ("symlink source", lambda: os.symlink(source, sandbox / "link")),
            ("link source", lambda: os.link(source, sandbox / "hardlink")),
            ("link destination", lambda: os.link(owned, outside / "hardlink")),
            ("truncate", lambda: os.truncate(source, 0)),
            ("ftruncate", lambda: os.ftruncate(fd, 0)),
            ("utime", lambda: os.utime(source, (0, 0))),
            ("sqlite", lambda: sqlite3.connect(str(outside / "db"))),
            ("sqlite URI", lambda: sqlite3.connect((outside / "db").as_uri(), uri=True)),
            ("mkdir dir_fd", lambda: os.mkdir("new", dir_fd=dir_fd)),
            ("remove dir_fd", lambda: os.remove("source", dir_fd=dir_fd)),
            ("chmod fd", lambda: os.fchmod(fd, 0o777)),
        ]
        for name, operation in operations:
            try:
                operation()
            except RuntimeError as error:
                assert self.id() in str(error), (name, error)
                assert "write outside temporary directory" in str(error), (name, error)
            else:
                raise AssertionError("escaped " + name)
        assert source.read_text() == "original"
        assert owned.read_text() == "owned"
        assert sorted(p.name for p in outside.iterdir()) == ["directory", "source"]
        allowed = sandbox / "allowed"
        allowed.mkdir()
        file = allowed / "file"
        file.write_text("ok")
        os.chmod(file, 0o600)
        os.chown(file, os.getuid(), os.getgid())
        os.utime(file, (1, 1))
        os.truncate(file, 1)
        os.symlink(file, allowed / "link")
        os.link(file, allowed / "hardlink")
        os.rename(file, allowed / "renamed")
        os.replace(allowed / "renamed", file)
        os.remove(allowed / "link")
        sqlite3.connect(str(allowed / "db")).close()
        sqlite3.connect(":memory:").close()
        shutil.rmtree(allowed)
        os.close(fd)
        os.close(dir_fd)
""")
        proc = run_suite(root, "Filesystem fixture", "integration")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        test = "integration.unit.fs_test.Filesystem.test_confined"
        real = outside.resolve()
        paths = [
            *(outside / "new", source, outside / "renamed", source, outside / "moved", source),
            *(outside / "directory", outside / "directory", source, source, outside / "link"),
            *(source, source, outside / "hardlink", source, real / "source", source),
            *(outside / "db", outside / "db", real / "new", real / "source", real / "source"),
        ]
        self.assertEqual(
            errors(proc.stdout),
            [
                (
                    test,
                    f"RuntimeError: test isolation: {test}: write outside temporary directory: {path}",
                )
                for path in paths
            ],
        )

    def test_hooks_reject_unit_effects_and_unisolated_integration_tools(self):
        sandbox = Path(self.home, "sandbox")
        sandbox.mkdir()
        marker = Path(self.home, "outside")
        proc = subprocess.run(
            [sys.executable, "-P", "-S", "-c", SCRIPT, REPO, str(sandbox), str(marker)],
            env=clean_env(None, str(Path(REPO, ".github"))),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout),
            [
                "os.exec",
                "os.fork",
                "os.forkpty",
                "os.kill",
                "os.killpg",
                "os.posix_spawn",
                "os.system",
                "socket.bind",
                "socket.connect",
                "socket.getaddrinfo",
                "socket.sendto",
                "subprocess.Popen",
            ],
        )
        self.assertFalse(marker.exists())
        self.assertEqual((sandbox / "owned").read_text(), "allowed")
        self.assertEqual(Path(self.home, "workspace/owned").read_text(), "allowed")
        self.assertEqual(Path(self.home, "summary").read_text(), "reported")

    def test_the_launcher_cleans_inherited_environment_and_python_startup(self):
        root = Path(self.home)
        package = root / "unit"
        package.mkdir()
        (package / "__init__.py").touch()
        marker = root / "startup-ran"
        ambient = root / "ambient"
        ambient.mkdir()
        (ambient / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('escaped')\n"
        )
        (package / "environment_test.py").write_text(
            "import os, sys, unittest\n"
            "class Environment(unittest.TestCase):\n"
            "    def test_clean(self):\n"
            "        self.assertNotIn('TMUX', os.environ)\n"
            "        self.assertNotIn('GIT_DIR', os.environ)\n"
            "        self.assertNotIn('GIT_CONFIG_COUNT', os.environ)\n"
            "        self.assertEqual(os.environ['GIT_CONFIG_GLOBAL'], os.devnull)\n"
            "        self.assertEqual(os.environ['GIT_CONFIG_NOSYSTEM'], '1')\n"
            "        self.assertEqual(os.environ['TUI_ATTACH_PREFIX'], '')\n"
            f"        self.assertNotIn({str(ambient)!r}, sys.path)\n"
            "        self.assertTrue(os.environ['PATH'].split(os.pathsep)[0].endswith('blocked-bin'))\n"
            "        self.assertNotEqual(os.environ['HOME'], os.environ['TMPDIR'])\n"
            "        self.assertEqual(os.environ['ENSEMBLE_CONTROL_ROOT'], os.environ['ENSEMBLE_TEST_SANDBOX'])\n"
        )
        proc = run_suite(
            root,
            "Unit fixture",
            "unit",
            roots=(str(ambient),),
            TMUX=str(marker) + ",1,0",
            GIT_DIR=str(marker),
            GIT_CONFIG_COUNT="1",
            TUI_ATTACH_PREFIX="ambient",
            RUN_TESTS_DURATIONS=str(root / "durations"),
            GITHUB_STEP_SUMMARY=str(root / "step-summary"),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(
            json.loads((root / "durations").read_text())["test"],
            "unit.environment_test.Environment.test_clean",
        )
