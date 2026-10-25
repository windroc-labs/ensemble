"""The Host callback that runs TmuxCli.mark for tui hooks, using the launch's trusted executable."""

import os
import stat
import subprocess

from ensemble.session.engine import tmux_cli
from ensemble.session.engine.tmux_cli import TuiError
from ensemble.session.engine.types import HookCall, HookError


def on_hook(word: str, call: HookCall) -> None:
    saved = call.state
    executable = saved.get("executable")
    if not isinstance(executable, str) or not os.path.isabs(executable):
        raise HookError("host state: bad executable")
    try:
        info = os.stat(executable)
    except OSError as error:
        raise HookError(f"tmux {executable}: {error.strerror}") from None
    if not stat.S_ISREG(info.st_mode) or not os.access(executable, os.X_OK):
        raise HookError(f"tmux {executable}: not a regular executable file")
    target, generation = saved.get("target"), saved.get("generation")
    if not isinstance(target, str) or not tmux_cli.NAME.fullmatch(target):
        raise HookError("host state: bad target")
    if not isinstance(generation, str) or not generation:
        raise HookError("host state: bad generation")
    selectors = saved.get("environment")
    if not isinstance(selectors, dict) or any(
        key not in ("TMUX", "TMUX_TMPDIR") or not isinstance(value, str) or "\0" in value
        for key, value in selectors.items()
    ):
        raise HookError("host state: bad environment")
    environment = {
        key: value for key, value in os.environ.items() if key not in ("TMUX", "TMUX_TMPDIR")
    }
    environment.update(selectors)

    def run(argv, **options):
        return subprocess.run(argv, env=environment, **options)

    mark_launched(run, word, call, target=target, generation=generation, tmux=executable)


def mark_launched(
    run, word: str, call: HookCall, *, target: str, generation: str, tmux: str, session=None
) -> None:
    """Marks target's launched pane while its @sid and @generation are the call's."""
    try:
        result = run(
            [
                tmux,
                "display-message",
                "-p",
                "-t",
                f"={target}:",
                "#{@process}\t#{@sid}\t#{@generation}",
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=tmux_cli.MARK_TIMEOUT,
        )
        if result.returncode:
            if tmux_cli.absent(result):
                return
            raise HookError(f"tmux: {tmux_cli.error(result)}")
        fields = result.stdout.strip().split("\t")
        if len(fields) != 3:
            return
        process, sid, actual_generation = fields
        if not tmux_cli.PROCESS_ID.fullmatch(process):
            return
        if sid != call.sid or actual_generation != generation:
            return
        tmux_cli.TmuxCli(run).mark(
            word, call.events, pane=process.split(" ")[2], tmux=tmux, session=session
        )
    except subprocess.TimeoutExpired:
        raise HookError(f"tmux: timed out after {tmux_cli.MARK_TIMEOUT} s") from None
    except OSError as error:
        raise HookError(f"tmux: {error}") from error
    except TuiError as error:
        raise HookError(str(error)) from None
