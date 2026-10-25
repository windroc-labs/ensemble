"""Load clients from a caller-owned fake module table."""

import importlib
from unittest import mock

from ensemble.utils import load_class


def client_loader(modules):
    def load(path, base):
        original = importlib.import_module

        def imported(name, package=None):
            return modules[name] if name in modules else original(name, package)

        with mock.patch("importlib.import_module", new=imported):
            return load_class(path, base)

    return load
