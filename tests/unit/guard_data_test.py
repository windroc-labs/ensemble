import ast
from pathlib import Path
from types import SimpleNamespace

from tests import guard_data
from tests.case import REPO, Case
from tests.guard_data import merged
from tests.guards import PROJECTS


class Merged(Case):
    def test_combines_each_layers_dict_frozenset_and_tuple_entries_in_layer_order(self):
        first = SimpleNamespace(A={("x", 1): "a"}, B=frozenset({"p"}), C=("m",))
        second = SimpleNamespace(A={("y", 2): "b"}, B=frozenset({"q"}), C=("n",))
        self.assertEqual(
            list(merged("A", (first, second)).items()), [(("x", 1), "a"), (("y", 2), "b")]
        )
        self.assertEqual(merged("B", (first, second)), frozenset({"p", "q"}))
        self.assertEqual(merged("C", (first, second)), ("m", "n"))

    def test_a_table_no_layer_defines_is_empty(self):
        self.assertEqual(merged("A", (SimpleNamespace(), SimpleNamespace(B=("m",)))), {})

    def test_a_key_in_two_layers_raises(self):
        layers = (SimpleNamespace(A={("x", 1): "a"}), SimpleNamespace(A={("x", 1): "b"}))
        with self.assertRaisesRegex(ValueError, r"^A: in two layers: \('x', 1\)$"):
            merged("A", layers)


class LayerModules(Case):
    def test_layer_modules_then_repository_owned_test_data(self):
        self.assertEqual(
            [module.__name__ for module in guard_data.MODULES],
            [f"tests.guard_data.{package}" for package in (*PROJECTS.values(), "repository")],
        )

    def test_every_layer_name_is_merged_or_read(self):
        init = ast.parse(Path(guard_data.__file__).read_text(encoding="utf-8"))
        names = {
            node.args[0].value
            for node in ast.walk(init)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "merged"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        for path in Path(REPO, "tests").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    names.add(node.id)
                elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                    names.add(node.attr)
                elif isinstance(node, ast.alias):
                    names.add(node.name)
        self.assertEqual(
            sorted(
                f"{module.__name__}.{name}"
                for module in guard_data.MODULES
                for name in vars(module)
                if name.isupper() and name not in names
            ),
            [],
        )
