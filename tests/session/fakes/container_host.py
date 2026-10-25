"""A TmuxHost whose clients get a container context, as on the Docker Host."""

import sys
from collections.abc import Mapping
from dataclasses import replace

from ensemble.session.engine.types import ClientContext, Container, SessionError
from ensemble.session.hosts.tmux import TmuxHost

PATH = f"{__name__}:ContainerTmuxHost"
HOMED = f"{__name__}:HomedContainerHost"


class ContainerTmuxHost(TmuxHost):
    def _client_context(self) -> ClientContext:
        container = Container("/opt/bin", sys.executable, "/opt/src", "/run")
        return replace(super()._client_context(), container=container)


class HomedContainerHost(ContainerTmuxHost):
    """Gives a client its home env, as the Docker Host does; keeps each headless start's metadata
    in `piped`, then refuses the start.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.piped: list[dict[str, str]] = []

    def _client_env(self, name, client, home, *, resume) -> Mapping[str, str]:
        return {"FAKE_HOME": "/h"}

    def pipe(self, name, launch, *, metadata=None):
        self.piped.append(dict(metadata or {}))
        raise SessionError("refused by the test Host")
