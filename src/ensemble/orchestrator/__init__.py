"""Linear issue runs and board orchestration."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ensemble.orchestrator.engine.linear_run import LinearRun as LinearRun

__all__ = ["LinearRun"]


def __getattr__(name):
    if name == "LinearRun":
        from ensemble.orchestrator.engine.linear_run import LinearRun

        return LinearRun
    raise AttributeError(name)
