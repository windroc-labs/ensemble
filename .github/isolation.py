"""Interpreter gates for unit logic and sandboxed integration tools."""

import contextlib
import fcntl
import inspect
import os
import stat
import sys
import tempfile
import traceback
from pathlib import Path
from urllib.parse import unquote, urlsplit

DENIED = {
    "subprocess.Popen",
    "os.exec",
    "os.posix_spawn",
    "os.fork",
    "os.forkpty",
    "os.kill",
    "os.killpg",
    "os.system",
    "socket.connect",
    "socket.bind",
    "socket.sendto",
    "socket.getaddrinfo",
}
GIT_ALLOWED = {
    "GIT_CEILING_DIRECTORIES",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
}
WRITE_PATHS = {
    "os.mkdir": ((0, 2),),
    "os.rename": ((0, 2), (1, 3)),
    "os.remove": ((0, 1),),
    "os.rmdir": ((0, 1),),
    "os.chmod": ((0, 2),),
    "os.chown": ((0, 3),),
    "os.chflags": ((0, None),),
    "os.symlink": ((0, None), (1, 2)),
    "os.link": ((0, 2), (1, 3)),
    "os.truncate": ((0, None),),
    "os.utime": ((0, 3),),
    "os.setxattr": ((0, None),),
    "os.removexattr": ((0, None),),
    "shutil.rmtree": ((0, 1),),
    "shutil.move": ((0, None), (1, None)),
    "shutil.chown": ((0, None),),
    "shutil.copyfile": ((1, None),),
    "shutil.copymode": ((1, None),),
    "shutil.copystat": ((1, None),),
    "shutil.copytree": ((1, None),),
    "shutil.make_archive": ((0, None),),
    "shutil.unpack_archive": ((1, None),),
    "sqlite3.connect": ((0, None),),
}


class Gate:
    def __init__(self, root, sandbox, workspace=None):
        self.root = root.resolve()
        self.sandbox = Path(sandbox).resolve()
        self.workspace = Path(workspace).resolve() if workspace else self.sandbox
        self.current = "test discovery"
        self.unit = True
        self.unit_only = False
        self.reporting_file = None
        self.violations = []

    def fail(self, message):
        error = RuntimeError(
            f"test isolation: {self.current}: {message}\n{''.join(traceback.format_stack())}"
        )
        self.violations.append(error)
        raise error

    def path(self, path, dir_fd=None):
        if isinstance(path, int):
            if sys.platform == "darwin":
                path = fcntl.fcntl(path, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0]
            else:
                path = os.readlink(f"/proc/self/fd/{path}")
        path = Path(os.fsdecode(path))
        if dir_fd is not None and dir_fd != -1 and not path.is_absolute():
            path = self.path(dir_fd) / path
        return path

    def inside(self, path):
        actual = self.path(path).resolve()
        return actual.is_relative_to(self.sandbox) or actual.is_relative_to(self.workspace)

    def write_path(self, path, dir_fd=None):
        path = self.path(path, dir_fd)
        if not self.inside(path):
            self.fail(f"write outside temporary directory: {path}")

    def audit(self, event, args):
        if event in DENIED and self.unit:
            self.fail(f"forbidden {event}")
        if event == "subprocess.Popen":
            executable, argv, cwd, env = args
            self.command(executable, argv, cwd, os.environ if env is None else env)
        elif event in {"os.exec", "os.posix_spawn"}:
            executable, argv, env = args
            self.command(executable, argv, None, os.environ if env is None else env)
        elif event == "os.system":
            self.fail("shell launch cannot verify private tool arguments")
        elif event == "tempfile.mkdtemp":
            path = os.fsdecode(args[0])
            if os.path.basename(path).startswith("apm-group-") and not self.inside(path):
                self.fail(f"process controller outside test sandbox: {path}")
        elif event == "open" and self.unit:
            path, mode, flags = args
            writes = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if (
                writes
                and not isinstance(path, int)
                and path != os.devnull
                and not self.inside(path)
                and Path(path).resolve() != self.reporting_file
            ):
                self.fail(f"write outside temporary directory: {path}")
        elif self.unit and event in WRITE_PATHS:
            for index, fd_index in WRITE_PATHS[event]:
                path = args[index]
                if event == "sqlite3.connect":
                    if path == ":memory:":
                        continue
                    if os.fsdecode(path).startswith("file:"):
                        path = unquote(urlsplit(os.fsdecode(path)).path)
                if event == "shutil.unpack_archive" and path is None:
                    path = os.getcwd()
                if event == "os.symlink" and index == 0 and not os.path.isabs(path):
                    path = self.path(args[1], args[2]).parent / os.fsdecode(path)
                self.write_path(path, args[fd_index] if fd_index is not None else None)

    def command(self, executable, argv, cwd, env):
        name = os.path.basename(os.fsdecode(executable))
        if name == "tmux":
            if isinstance(argv, str):
                self.fail("tmux requires a private socket")
            socket = None
            label = None
            index = 1
            while index < len(argv):
                option = os.fsdecode(argv[index])
                if not option.startswith("-") or option in {"-", "--"}:
                    break
                for offset, flag in enumerate(option[1:], 2):
                    if flag not in "SLfTc":
                        continue
                    value = option[offset:]
                    if not value:
                        index += 1
                        value = os.fsdecode(argv[index]) if index < len(argv) else ""
                    if flag == "S":
                        socket = value
                    elif flag == "L":
                        label = value
                    break
                index += 1
            if socket is None:
                directory = env.get("TMUX_TMPDIR", "")
                if not directory or not self.inside(directory):
                    self.fail("tmux requires a private TMUX_TMPDIR or -S socket")
                try:
                    metadata = Path(directory).lstat()
                    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                        self.fail("tmux requires a real TMUX_TMPDIR directory owned by the user")
                except OSError:
                    self.fail("tmux requires an existing TMUX_TMPDIR directory")
                socket = (
                    env.get("TMUX", "").split(",")[0] if label is None else ""
                ) or os.path.join(directory, f"tmux-{os.getuid()}", label or "default")
            if not socket or not self.inside(socket):
                self.fail("tmux socket outside test sandbox")
        if name == "git":
            unsafe = sorted(key for key in env if key.startswith("GIT_") and key not in GIT_ALLOWED)
            if (
                unsafe
                or env.get("GIT_CONFIG_GLOBAL") != os.devnull
                or env.get("GIT_CONFIG_NOSYSTEM") != "1"
            ):
                self.fail(f"git requires clean environment: {unsafe}")

    @contextlib.contextmanager
    def reporting(self, path):
        previous = self.reporting_file
        self.reporting_file = Path(path).resolve()
        try:
            yield
        finally:
            self.reporting_file = previous

    @contextlib.contextmanager
    def test(self, test):
        path = Path(inspect.getfile(type(test))).resolve()
        previous = self.current, self.unit
        self.current = test.id()
        self.unit = self.unit_only or "unit" in path.parts or path.parent == self.root / ".github"
        path_before = os.environ.get("PATH", os.defpath)
        if self.unit:
            os.environ["PATH"] = str(self.sandbox / "blocked-bin") + os.pathsep + path_before
        try:
            yield
        finally:
            self.current, self.unit = previous
            os.environ["PATH"] = path_before


def blockers(directory):
    directory.mkdir()
    for name in ("git", "tmux", "claude", "codex"):
        path = directory / name
        path.write_text(f"#!/bin/sh\necho 'test isolation: forbidden {name}' >&2\nexit 126\n")
        path.chmod(0o700)


def install(root):
    sandbox = os.environ["ENSEMBLE_TEST_SANDBOX"]
    tempfile.tempdir = sandbox
    sys.dont_write_bytecode = True
    gate = Gate(root, sandbox, os.environ.get("ENSEMBLE_TEST_WORKSPACE"))
    sys.addaudithook(gate.audit)
    return gate
