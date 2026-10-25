"""The Delegate: runs one role as an agent session, its prompt and access from the Composer, its
session from the Driver, read from its replies' progress marks and outcome block.
"""

from ensemble.delegate.composer import RunConfig
from ensemble.delegate.engine.base import Delegate
from ensemble.delegate.engine.harness_delegate import HarnessDelegate
from ensemble.delegate.engine.types import ConfigError, Progress, Spec
from ensemble.delegate.event import DelegateEvent
from ensemble.delegate.event.types import Outcome
from ensemble.session import Resumable, SessionError, Window

__all__ = [
    "ConfigError",
    "Spec",
    "SessionError",
    "Resumable",
    "Window",
    "Outcome",
    "Progress",
    "Delegate",
    "HarnessDelegate",
    "RunConfig",
    "DelegateEvent",
]
