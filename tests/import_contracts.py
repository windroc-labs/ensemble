"""The nested-unit policy applied to an import graph."""

from tests.import_policy import violation


def broken(graph, policy):
    """Import details of every direct import in graph that policy forbids."""
    return [
        detail
        for importer in sorted(graph.modules)
        for imported in sorted(graph.find_modules_directly_imported_by(importer))
        if violation(importer, imported, policy)
        for detail in graph.get_import_details(importer=importer, imported=imported)
    ]
