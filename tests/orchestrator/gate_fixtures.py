"""Process-free Delegate quota providers shared by Gate boundary tests."""

from dataclasses import replace
from types import SimpleNamespace

from ensemble.session import Usage, Window
from tests.delegate.fakes.delegate import FakeDelegate


def quota_provider(client, percent):
    class QuotaDelegate(FakeDelegate):
        ROLES = {"engineer": {"build": "Build"}, "pm": {"design": "Design"}}
        queried = []
        overlays = []

        @classmethod
        def resolve(cls, role, task=None, *, overlays=()):
            return replace(super().resolve(role, task, overlays=overlays), client=client)

        @classmethod
        def usage(cls, role, *, overlays=()):
            cls.queried.append(role)
            cls.overlays.append(overlays)
            return Usage(Window(percent, None), None)

    return QuotaDelegate


def quota_config(provider, kind, *, core=None, clients=None):
    """A Gate's config: each provider role run as config.load resolves it, or with `clients`."""
    overlays = (core or {},)
    roles = {
        role: SimpleNamespace(run=provider.resolve(role, overlays=overlays))
        for role in provider.ROLES
    }
    for role, client in (clients or {}).items():
        roles[role] = SimpleNamespace(run=SimpleNamespace(client=client))
    return SimpleNamespace(
        delegate_kind=kind,
        delegate_cls=provider,
        delegates={kind: provider},
        roles=roles,
        core=core or {},
        usage_limit={},
    )
