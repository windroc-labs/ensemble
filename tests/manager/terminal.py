"""Run a test client or holder on an isolated controlling terminal."""

import contextlib
import fcntl
import os
import subprocess
import sys
import termios


@contextlib.contextmanager
def process(argv, *, holder="inherit", **options):
    master, slave = os.openpty()
    try:
        with subprocess.Popen(
            (sys.executable, "-I", __file__, str(slave), holder, *argv),
            start_new_session=True,
            pass_fds=(slave,),
            **options,
        ) as child:
            yield child
    finally:
        os.close(slave)
        os.close(master)


def main():
    fd, holder, *argv = sys.argv[1:]
    fcntl.ioctl(int(fd), termios.TIOCSCTTY, 0)
    os.close(int(fd))
    if holder == "parent":
        os.environ["ENSEMBLE_HOLDER_PID"] = str(os.getpid())
    if holder in {"parent", "shell"}:
        return subprocess.call(argv)
    os.execvpe(argv[0], argv, os.environ)


if __name__ == "__main__":
    sys.exit(main())
