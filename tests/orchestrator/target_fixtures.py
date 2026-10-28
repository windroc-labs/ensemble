"""Shared orchestrator scenario fixtures for unit and sandboxed integration contracts."""

REMOTE = ("git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential")
LOCAL = ("git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C")
