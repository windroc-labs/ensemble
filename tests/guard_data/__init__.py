"""Each layer's guard data, one module per layer package: its constants and its entries of the tables
below. An entry goes in the layer owning the path or package it names; the checks read the tables.
"""

import importlib

from tests.guards import PROJECTS

MODULES = tuple(
    importlib.import_module(f"{__name__}.{package}")
    for package in (*PROJECTS.values(), "repository")
)


def merged(name, modules=MODULES):
    """`name`'s entries from each module defining it, in layer order; a key in two raises."""
    tables = [vars(module)[name] for module in modules if name in vars(module)]
    keys = [key for table in tables for key in table]
    if repeated := sorted({repr(key) for key in keys if keys.count(key) > 1}):
        raise ValueError(f"{name}: in two layers: {', '.join(repeated)}")
    if not tables or isinstance(tables[0], dict):
        return {key: value for table in tables for key, value in table.items()}
    return type(tables[0])(key for table in tables for key in table)


NOQA_ALLOWED = merged("NOQA_ALLOWED")


NO_COVER_ALLOWED = merged("NO_COVER_ALLOWED")


OBJECT_EXPORT_MODULES = merged("OBJECT_EXPORT_MODULES")


OBJECT_EXPORTS_ALLOWED = merged("OBJECT_EXPORTS_ALLOWED")


OPTION_TUPLES_ALLOWED = merged("OPTION_TUPLES_ALLOWED")


STANDALONE = merged("STANDALONE")


SUBPACKAGES = merged("SUBPACKAGES")


ENTRY_MODULES = merged("ENTRY_MODULES")


COMMAND_SRC = merged("COMMAND_SRC")


COMMAND_FILES = merged("COMMAND_FILES")


COMMAND_DOCS = merged("COMMAND_DOCS")


PYTHON_CODE_ALLOWED = merged("PYTHON_CODE_ALLOWED")


EMBEDDED_PROGRAMS_ALLOWED = merged("EMBEDDED_PROGRAMS_ALLOWED")


EMBEDDED_PROGRAM_ARGV_RETURNS = merged("EMBEDDED_PROGRAM_ARGV_RETURNS")
