"""Standard-library clock control and production log configuration for tests."""

import contextlib

from ensemble.orchestrator.engine import log


@contextlib.contextmanager
def logs_at(path):
    previous = log.LOGS_DIR
    log.use_logs(path)
    try:
        yield
    finally:
        log.use_logs(previous)
