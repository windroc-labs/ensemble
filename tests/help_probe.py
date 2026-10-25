"""Run a command module with observable, forbidden operational boundaries."""

import os
import runpy
import sys


class Unreadable:
    @property
    def buffer(self):
        return self

    def read(self, *args):
        raise AssertionError("help consumed stdin")

    readline = read
    readlines = read
    __iter__ = read

    def isatty(self):
        return False


def audit(event, args):
    if event == "open" and args[2] & (
        os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    ):
        raise AssertionError(f"help opened a writable file: {args[0]}")
    if event in {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.fork",
        "os.posix_spawn",
        "socket.connect",
        "socket.bind",
        "socket.getaddrinfo",
        "os.mkdir",
        "os.remove",
        "os.rmdir",
        "os.rename",
        "os.link",
        "os.symlink",
        "os.chmod",
        "os.chown",
        "os.utime",
    }:
        raise AssertionError(f"help crossed an operational boundary: {event}")


def main():
    if sys.argv[1:3] == ["-I", "-"]:
        os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
    at = sys.argv.index("-m")
    module, *arguments = sys.argv[at + 1 :]
    sys.argv = [module, *arguments]
    sys.stdin = Unreadable()
    sys.addaudithook(audit)
    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
