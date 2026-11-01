"""A TmuxHost whose clients get a container context, as on the Docker Host; Hosts that record a
headless start and refuse it.
"""

import sys
from collections.abc import Mapping
from dataclasses import replace

from ensemble.session.engine.types import ClientContext, Container, Launch, SessionError
from ensemble.session.hosts.tmux import TmuxHost

PATH = f"{__name__}:ContainerTmuxHost"
HOMED = f"{__name__}:HomedContainerHost"
RECORDING = f"{__name__}:RecordingTmuxHost"
NAMED = f"{__name__}:NamedHeadlessHost"


class ContainerTmuxHost(TmuxHost):
    def _client_context(self) -> ClientContext:
        container = Container("/opt/bin", sys.executable, "/opt/src", "/run")
        return replace(super()._client_context(), container=container)


class NamedHeadlessHost(TmuxHost):
    """Refuses a nameless headless run, as the Docker Host does."""

    _named_headless = True


class _Recording(TmuxHost):
    """Keeps each headless start's metadata in `headless` and launch in `launches`, then refuses
    the start.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.headless: list[dict[str, str]] = []
        self.launches: list[Launch] = []

    def worker(
        self,
        name: str,
        launch: Launch,
        *,
        terminal: bool,
        display=None,
        events: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ):
        if terminal:
            return super().worker(
                name, launch, terminal=True, display=display, events=events, metadata=metadata
            )
        self.headless.append(dict(metadata or {}))
        self.launches.append(launch)
        raise SessionError("refused by the test Host")


class RecordingTmuxHost(_Recording):
    """A local Host recording its headless starts."""


class HomedContainerHost(_Recording, ContainerTmuxHost):
    """Gives a client its home env, as the Docker Host does, and records its headless starts."""

    def _client_env(self, name, client, home, *, resume) -> Mapping[str, str]:
        return {"FAKE_HOME": "/h"}
