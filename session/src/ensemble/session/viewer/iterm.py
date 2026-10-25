"""iTerm2 splits: the AppleScript and the call running it."""

from __future__ import annotations

import subprocess

from ensemble.session.engine.tmux_cli import (
    ITERM_ID,
    TuiError,
    error,
)

SHOW_TIMEOUT = 30


PGREP = ["pgrep", "-a", "-x", "iTerm2"]


APPLESCRIPT = """on run argv
  set {splitDir, anchorKind, tmuxPath, sessionName} to items 1 thru 4 of argv
  set paneCommand to (quoted form of tmuxPath) & " attach -t " & (quoted form of ("=" & sessionName))
  if application "iTerm2" is running then
    tell application "iTerm2"
      repeat with i from 5 to count of argv
        set anchorValue to item i of argv
        repeat with w in windows
          repeat with t in tabs of w
            repeat with s in sessions of t
              if (anchorKind is "id" and unique id of s is anchorValue) or (anchorKind is "tty" and tty of s is anchorValue) then
                if splitDir is "right" then
                  tell s to set newSession to split vertically with default profile command paneCommand
                else
                  tell s to set newSession to split horizontally with default profile command paneCommand
                end if
                return "ok " & (unique id of newSession)
              end if
            end repeat
          end repeat
        end repeat
      end repeat
    end tell
    return "no iTerm2 pane shows the anchor"
  end if
  return "iTerm2 is not running"
end run"""


class Iterm:
    def __init__(self, runner) -> None:
        self.runner = runner

    def split_pane(self, session: str, split: str, tmux: str, iterm: tuple[str, list[str]]) -> str:
        try:
            running = (
                self.runner(
                    PGREP, capture_output=True, text=True, stdin=subprocess.DEVNULL
                ).returncode
                == 0
            )
        except OSError:
            running = False
        if not running:
            raise TuiError("iTerm2 is not running")
        kind, anchors = iterm
        try:
            res = self.runner(
                ["osascript", "-e", APPLESCRIPT, split, kind, tmux, session, *anchors],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=SHOW_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise TuiError(f"osascript timed out after {SHOW_TIMEOUT} s") from None
        except OSError as e:
            raise TuiError(f"osascript: {e}") from e
        if res.returncode:
            raise TuiError(f"osascript: {error(res)}")
        out = res.stdout.strip()
        if out.startswith("ok ") and ITERM_ID.fullmatch(out[3:]):
            return out[3:]
        raise TuiError(out or "osascript printed nothing")
