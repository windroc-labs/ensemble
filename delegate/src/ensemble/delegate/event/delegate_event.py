"""DelegateEvent v1, the callback line: one JSON line per harness event on a callback's stdin,
holding its run's sid and attempt and its kind's fields. Outcome is parsed at this boundary;
limit remains a dict of a Limit's fields.
"""

import json
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from typing import Self

from ensemble.delegate.event.outcome import parse as parse_outcome
from ensemble.delegate.event.types import InvalidOutcome, Outcome


class DelegateEventKind(StrEnum):
    BEGUN = "begun"
    PROGRESS = "progress"
    FINISHED = "finished"


_FIELDS: dict[str, tuple[str, ...]] = {
    DelegateEventKind.BEGUN: ("sid", "attempt"),
    DelegateEventKind.PROGRESS: ("sid", "attempt", "name", "text"),
    DelegateEventKind.FINISHED: ("sid", "attempt", "returncode", "outcome", "error", "limit"),
}
_KIND = f"kind: want one of {', '.join(DelegateEventKind)}"


class DelegateEventError(ValueError):
    pass


def _check(*, ok: bool, message: str) -> None:
    if not ok:
        raise DelegateEventError(message)


def _number(value) -> bool:
    return isinstance(value, int | float) and (not isinstance(value, bool))


@dataclass(frozen=True)
class DelegateEvent:
    kind: str
    sid: str | None = None
    attempt: int | None = None
    name: str | None = None
    text: str | None = None
    returncode: int | None = None
    outcome: Outcome | None = None
    error: str = ""
    limit: dict | None = None

    def __post_init__(self):
        _check(ok=self.kind in tuple(DelegateEventKind), message=_KIND)
        for each in fields(self)[1:]:
            if each.name not in _FIELDS[self.kind]:
                _check(
                    ok=getattr(self, each.name) == each.default,
                    message=f"{each.name}: not a {self.kind} field",
                )
        _check(ok=isinstance(self.sid, str) and self.sid != "", message="sid: want a non-empty str")
        _check(
            ok=type(self.attempt) is int and self.attempt >= 1,
            message="attempt: want an int >= 1",
        )
        if self.kind == DelegateEventKind.PROGRESS:
            for field in ("name", "text"):
                _check(ok=isinstance(getattr(self, field), str), message=f"{field}: want a str")
        elif self.kind == DelegateEventKind.FINISHED:
            _check(ok=type(self.returncode) is int, message="returncode: want an int")
            outcome, limit = (self.outcome, self.limit)
            _check(
                ok=outcome is None or isinstance(outcome, Outcome),
                message="outcome: want null or an Outcome",
            )
            _check(ok=isinstance(self.error, str), message="error: want a str")
            _check(
                ok=limit is None
                or (
                    isinstance(limit, dict)
                    and limit.keys() == {"resets_at"}
                    and (limit["resets_at"] is None or _number(limit["resets_at"]))
                ),
                message='limit: want null or {"resets_at": a number or null}',
            )

    def to_line(self) -> str:
        values = asdict(self)
        data = {"v": 1, "kind": self.kind} | {f: values[f] for f in _FIELDS[self.kind]}
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def parse(cls, line: str) -> Self:
        _check(ok=isinstance(line, str), message="DelegateEvent line: want a str")
        try:
            data = json.loads(line.removesuffix("\n"))
        except (ValueError, RecursionError) as e:
            raise DelegateEventError(f"DelegateEvent line: not JSON ({e})") from None
        _check(ok=isinstance(data, dict), message="DelegateEvent line: want a JSON object")
        _check(ok=type(data.get("v")) is int and data["v"] == 1, message="v: want 1")
        kind = data.get("kind")
        _check(ok=kind in tuple(DelegateEventKind), message=_KIND)
        for key in data:
            _check(ok=key in ("v", "kind", *_FIELDS[kind]), message=f"{key}: not a {kind} field")
        if data.get("outcome") is not None:
            try:
                data["outcome"] = parse_outcome(data["outcome"])
            except InvalidOutcome as error:
                raise DelegateEventError(str(error)) from None
        return cls(**{k: v for k, v in data.items() if k != "v"})
