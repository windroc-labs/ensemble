"""The repo's import contracts, and the nested-unit policy applied to an import graph."""

import os
import tomllib

from tests.import_policy import violation


def contracts(repo):
    """Every import contract in pyproject.toml: import-linter's, then the nested-unit ones."""
    with open(os.path.join(repo, "pyproject.toml"), "rb") as file:
        tool = tomllib.load(file)["tool"]
    return [*tool["importlinter"]["contracts"], *tool.get("ensemble", {}).get("unit_contracts", ())]


def broken(graph, policy):
    """Import details of every direct import in graph that policy forbids."""
    return [
        detail
        for importer in sorted(graph.modules)
        for imported in sorted(graph.find_modules_directly_imported_by(importer))
        if violation(importer, imported, policy)
        for detail in graph.get_import_details(importer=importer, imported=imported)
    ]
