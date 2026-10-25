"""SessionStatusLog, an events file's writer and the status log line's only home
(tests/guard_checks/ownership.py status_log_format): one line per event,
`<HH:MM:SS> <name> <word>[ <field>…]`. Each write reopens the file by path, so a rotation never
loses a line; a Host's hook command builds its clock from CLOCK.
"""

import time

from ensemble.utils import append_line


def _text(label: str, value, *, one_word: bool = False) -> str:
    if not (
        isinstance(value, str)
        and value != ""
        and value.isprintable()
        and (not one_word or value.split() == [value])
    ):
        want = "printable text without whitespace" if one_word else "non-empty printable text"
        raise ValueError(f"event {label}: want {want}, not {value!r}")
    return value


class SessionStatusLog:
    CLOCK = "%H:%M:%S"

    def __init__(self, path: str) -> None:
        self.path = path

    @staticmethod
    def line(name: str, word: str, *fields, at: str | None = None) -> str:
        head = [_text("name", name, one_word=True), _text("word", word, one_word=True)]
        texts = [_text("field", str(f)) for f in fields]
        clock = time.strftime(SessionStatusLog.CLOCK) if at is None else _text("at", at)
        return " ".join([clock, *head, *texts])

    def write(self, name: str, word: str, *fields) -> None:
        """OSError (not a regular file) propagates."""
        append_line(self.path, self.line(name, word, *fields) + "\n")
