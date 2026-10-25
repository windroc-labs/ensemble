"""Pure dependency policy shared by repository guards and Import Linter contracts."""

import sys


def _within(module, package):
    return module == package or module.startswith(package + ".")


def _scope(module, units):
    return max((unit for unit in units if _within(module, unit)), key=len, default="outside")


def violation(importer, imported, policy):
    if imported.split(".")[0] in sys.stdlib_module_names:
        return False
    container, units = policy["container"], policy["units"]
    if importer.startswith("ensemble.") and any(
        _within(imported, unit) and not _within(importer, unit)
        for unit in policy.get("unimported_units", ())
    ):
        return True
    if not _within(importer, container):
        return False
    own = _scope(importer, units)
    if _within(imported, container) and _scope(imported, units) == own:
        return False
    permitted = []
    for declaration in policy["dependencies"]:
        source, _, targets = declaration.partition(" -> ")
        if source == own or _within(source, own) and _within(importer, source):
            permitted.extend(targets.split())
    return not any(
        _scope(imported, units) == target
        if _within(target, container)
        else _within(imported, target)
        for target in permitted
    )
