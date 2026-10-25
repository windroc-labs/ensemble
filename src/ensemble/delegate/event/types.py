"""Outcome data and validation failures shared by the delegate's callers."""

from dataclasses import dataclass, field
from enum import StrEnum

from ensemble.utils import OutcomeStatus

STATUSES = tuple(OutcomeStatus)


class UpdraftStatus(StrEnum):
    CONVERGED = "converged"
    CAPPED = "capped-without-pass"
    STOPPED = "stopped"


@dataclass(frozen=True)
class Updraft:
    """An outcome's `updraft`: the status and head of the run's last updraft result."""

    status: UpdraftStatus
    head: str


class InvalidOutcome(Exception):
    pass


@dataclass(frozen=True)
class Outcome:
    status: OutcomeStatus
    title: str
    summary: str
    questions: list[str] = field(default_factory=list)
    url: str = ""
    files: list[str] = field(default_factory=list)
    deliverable: str = ""
