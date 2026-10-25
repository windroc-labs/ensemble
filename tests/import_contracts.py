"""Import Linter contracts for nested repository units; literal imports use the same policy."""

from importlinter import Contract, ContractCheck, fields, output

from tests.import_policy import violation


class UnitsContract(Contract):
    container = fields.StringField()
    units = fields.ListField(subfield=fields.StringField())
    dependencies = fields.ListField(subfield=fields.StringField())
    unimported_units = fields.ListField(subfield=fields.StringField(), default=[])

    def check(self, graph, verbose):
        broken = []
        for importer in sorted(graph.modules):
            for imported in sorted(graph.find_modules_directly_imported_by(importer)):
                if violation(importer, imported, self.contract_options):
                    broken.extend(graph.get_import_details(importer=importer, imported=imported))
        return ContractCheck(kept=not broken, metadata={"broken": broken})

    def render_broken_contract(self, check):
        for edge in check.metadata["broken"]:
            output.print_error(
                f"{edge['importer']}:{edge['line_number']}: imports {edge['imported']}"
            )
