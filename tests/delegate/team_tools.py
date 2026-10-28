"""tool(name): the team tool script TEAM/tools/<name>.py, loaded as module `name`."""

import importlib.util
import os

from ensemble.delegate.composer.base import TEAM


def tool(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TEAM, "tools", f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
