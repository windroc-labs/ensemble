"""Shared orchestrator scenario fixtures for unit and sandboxed integration contracts."""

LOCAL = ("git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C")
