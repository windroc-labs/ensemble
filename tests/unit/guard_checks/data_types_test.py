from tests.case import REPO
from tests.guard_checks import data_types
from tests.guard_data import OPTION_TUPLES_ALLOWED, STANDALONE
from tests.guard_data.orchestrator import ORCH_PROJECT
from tests.guard_data.session import SESSION_SRC
from tests.guard_samples import PKG, Sample

OPKG = "src/ensemble/orchestrator"


class SharedTypes(Sample):
    DATA = "from dataclasses import dataclass\n\n\n@dataclass\nclass A:\n    x: int\n"
    UTILS = f"utils/{PKG}/utils"

    def found(self, home: str, user: str = "b.py", extra=None) -> list[str]:
        user_text = f"from ensemble.utils.{home[:-3]} import A\n"
        return data_types.shared_types(
            self.tree(
                {
                    f"{self.UTILS}/{home}": self.DATA,
                    f"{self.UTILS}/{user}": user_text,
                    **(extra or {}),
                }
            )
        )

    def test_a_dataclass_another_module_imports_lives_in_a_types_py_or_is_re_exported(self):
        for home, user, expected in (
            (
                "a.py",
                "b.py",
                [
                    f"{self.UTILS}/b.py:1: ensemble.utils.a.A: a shared data type belongs in a types.py"
                ],
            ),
            ("types.py", "b.py", []),
            ("a.py", "__init__.py", []),
        ):
            with self.subTest(home=home, user=user):
                self.assertEqual(self.found(home, user), expected)

    def test_orchestrator_issue_data_types_have_no_exemption(self):
        home = "orchestrator/src/ensemble/orchestrator/engine"
        for name in ("Issue", "Handoff", "Note", "Link", "Linked"):
            with self.subTest(name=name):
                root = self.tree(
                    {
                        f"{home}/issues.py": self.DATA.replace("class A:", f"class {name}:"),
                        f"{home}/writeback.py": (
                            f"from ensemble.orchestrator.engine.issues import {name}\n"
                        ),
                    }
                )
                self.assertEqual(
                    data_types.shared_types(root),
                    [
                        f"{home}/writeback.py:1: ensemble.orchestrator.engine.issues.{name}: a shared data type belongs in a types.py"
                    ],
                )

    def test_a_relative_import_and_a_module_attribute_count(self):
        for user in ("from .a import A\n", "from ensemble.utils import a\n\nx = a.A\n"):
            with self.subTest(user):
                root = self.tree({f"{self.UTILS}/a.py": self.DATA, f"{self.UTILS}/b.py": user})
                ((found,),) = [data_types.shared_types(root)]
                self.assertIn(": ensemble.utils.a.A: a shared data type", found)

    def test_package_data_types_stay_with_their_own_consumers(self):
        root = self.tree(
            {
                f"{self.UTILS}/owner/__init__.py": self.DATA,
                f"{self.UTILS}/owner/start.py": "from ensemble.utils.owner import A\n",
                f"{self.UTILS}/owner/child/start.py": "from ensemble.utils.owner import A\n",
                f"{self.UTILS}/outside.py": "from ensemble.utils.owner import A\n",
            }
        )
        self.assertEqual(
            data_types.shared_types(root),
            [
                f"{self.UTILS}/outside.py:1: ensemble.utils.owner.A: a shared data type belongs in a types.py",
                f"{self.UTILS}/owner/child/start.py:1: ensemble.utils.owner.A: a shared data type belongs in a types.py",
            ],
        )

    def test_data_type_kinds_aliases_and_qualified_names_count_but_behavior_classes_do_not(self):
        shared = [
            f"{self.UTILS}/b.py:1: ensemble.utils.a.A: a shared data type belongs in a types.py"
        ]
        for source, expected in (
            *(
                (source, shared)
                for source in (
                    "from typing import NamedTuple\nclass A(NamedTuple):\n    value: int\n",
                    "from enum import Enum\nclass A(Enum):\n    value = 1\n",
                    "from enum import StrEnum\nclass A(StrEnum):\n    VALUE = 'value'\n",
                    "from typing import TypedDict\nclass A(TypedDict):\n    value: int\n",
                    "from dataclasses import dataclass as dc\n@dc(frozen=True)\nclass A:\n    value: int\n",
                    "import dataclasses as dc\n@dc.dataclass\nclass A:\n    value: int\n",
                    "from typing import NamedTuple as NT\nclass A(NT):\n    value: int\n",
                    "import typing as t\nclass A(t.NamedTuple):\n    value: int\n",
                    "from enum import Enum as E\nclass A(E):\n    value = 1\n",
                    "import enum as e\nclass A(e.Enum):\n    value = 1\n",
                    "from typing import TypedDict as TD\nclass A(TD):\n    value: int\n",
                    "import typing as t\nclass A(t.TypedDict):\n    value: int\n",
                )
            ),
            *(
                (source, [])
                for source in (
                    "class A:\n    def run(self):\n        return 1\n",
                    "from abc import ABC, abstractmethod\nclass A(ABC):\n    @abstractmethod\n    def run(self):\n        pass\n",
                    "from typing import Protocol\nclass A(Protocol):\n    def run(self): ...\n",
                    "class A(Exception):\n    pass\n",
                )
            ),
        ):
            with self.subTest(source=source):
                root = self.tree(
                    {
                        f"{self.UTILS}/a.py": source,
                        f"{self.UTILS}/b.py": "from ensemble.utils.a import A\n",
                    }
                )
                self.assertEqual(data_types.shared_types(root), expected)

    def test_standalone_definitions_and_consumers_keep_local_data_types(self):
        for path in STANDALONE:
            with self.subTest(path=path):
                module = path.split("/src/")[-1].lstrip(".").removesuffix(".py").replace("/", ".")
                root = self.tree(
                    {
                        path: self.DATA,
                        f"{SESSION_SRC}engine/ordinary.py": f"from {module} import A\n",
                    }
                )
                self.assertEqual(data_types.shared_types(root), [])
                root = self.tree(
                    {
                        path: "from ensemble.session.engine.ordinary import A\n",
                        f"{SESSION_SRC}engine/ordinary.py": self.DATA,
                        f"{SESSION_SRC}engine/consumer.py": (
                            "from ensemble.session.engine.ordinary import A\n"
                        ),
                    }
                )
                self.assertEqual(
                    data_types.shared_types(root),
                    [
                        f"{SESSION_SRC}engine/consumer.py:1: ensemble.session.engine.ordinary.A: a shared data type belongs in a types.py"
                    ],
                )


class TypesOnly(Sample):
    HOME = f"utils/{PKG}/utils"

    def found(self, source, name="types.py"):
        return data_types.types_only(self.tree({f"{self.HOME}/{name}": source}))

    def test_types_modules_hold_only_type_classes_and_private_helpers(self):
        def business(*items):
            return [
                f"{self.HOME}/types.py:{line}: {name}: business method in types module"
                for line, name in items
            ]

        for source, name, expected in (
            (
                "from enum import StrEnum\nclass State(StrEnum):\n    LIVE = 'live'\n",
                "types.py",
                [],
            ),
            ("from enum import StrEnum\nclass Choice(StrEnum):\n    ONE = 'one'\n", "types.py", []),
            ("from enum import IntEnum\nclass Rank(IntEnum):\n    ONE = 1\n", "types.py", []),
            ("import enum as e\nclass Choice(e.StrEnum):\n    ONE = 'one'\n", "types.py", []),
            (
                "def one(): pass\nasync def two(): pass\n",
                "types.py",
                [
                    f"{self.HOME}/types.py:1: types module function one",
                    f"{self.HOME}/types.py:2: types module function two",
                ],
            ),
            (
                "if True:\n    def helper(): pass\n",
                "types/helpers.py",
                [f"{self.HOME}/types/helpers.py:2: types module function helper"],
            ),
            ("def helper(): pass\n", "logic.py", []),
            (
                "from dataclasses import dataclass\ndef _valid(value): return value > 0\nasync def _async(value): return _valid(value)\n@dataclass\nclass Data:\n    value: int\n    def __post_init__(self): assert _valid(self.value)\n",
                "types.py",
                [],
            ),
            (
                "class Worker:\n    pass\n",
                "types.py",
                [f"{self.HOME}/types.py:1: Worker: not a type class"],
            ),
            (
                "from dataclasses import dataclass as dc\nfrom typing import NamedTuple, TypedDict, Protocol\nfrom enum import Enum\nfrom abc import ABC\n@dc\nclass Data:\n    value: int\nclass Tuple(NamedTuple):\n    value: int\nclass Table(TypedDict):\n    value: int\nclass Choice(Enum):\n    value = 1\nclass Interface(Protocol):\n    def run(self): ...\nclass Abstract(ABC):\n    pass\nclass Error(Exception):\n    pass\nclass Child(Error):\n    pass\n",
                "types.py",
                [],
            ),
            (
                "from abc import ABC, abstractmethod\nclass Interface(ABC):\n    def __init__(self): self.value = 1\n    @property\n    def value(self): return 1\n    @abstractmethod\n    def run(self): pass\n",
                "types.py",
                [],
            ),
            (
                "from dataclasses import dataclass\n@dataclass\nclass Team:\n    root: str\n    def custom(self, path): return path != self.root\n    def source(self, path): return path if self.custom(path) else 'built-in'\n",
                "types.py",
                [],
            ),
            (
                "from dataclasses import dataclass\n@dataclass\nclass Data:\n    value: int\n    @classmethod\n    def parse(cls, fields): return cls(fields['v']) if 'v' in fields else None\n    @classmethod\n    def other(cls, value): return value\n",
                "types.py",
                business((8, "Data.other")),
            ),
            (
                "from dataclasses import dataclass\nfrom abc import ABC\nclass Interface(ABC):\n    def run(self): return 1\n@dataclass\nclass Data:\n    value: int\n    def run(self): return 1\n    def other(self): return self.unknown\n    @staticmethod\n    def factory(value): return value\n",
                "types.py",
                business(
                    (11, "Data.factory"), (4, "Interface.run"), (8, "Data.run"), (9, "Data.other")
                ),
            ),
        ):
            with self.subTest(source=source, name=name):
                self.assertEqual(self.found(source, name), expected)

    def test_private_helpers_cannot_be_imported_or_used_in_other_files(self):
        for source, line in (
            ("from ensemble.utils.types import _valid as valid\n", 1),
            ("from ensemble.utils import types as data\ndata._valid(1)\n", 2),
            ("import ensemble.utils.types as data\ndata._valid(1)\n", 2),
        ):
            with self.subTest(source=source):
                root = self.tree(
                    {
                        f"{self.HOME}/types.py": "def _valid(value): return value > 0\n",
                        f"{self.HOME}/consumer.py": source,
                    }
                )
                self.assertEqual(
                    data_types.types_only(root),
                    [
                        f"{self.HOME}/consumer.py:{line}: private types helper ensemble.utils.types._valid used outside its module"
                    ],
                )


class RunSpecSerialization(Sample):
    def test_serialization_belongs_to_the_run_spec_alone(self):
        spec = f"{ORCH_PROJECT}/{OPKG}/engine/types.py"
        for path, source, expected in (
            (
                spec,
                "from dataclasses import dataclass\n@dataclass\nclass Spec:\n    def to_argv(self): return ['run']\n    @classmethod\n    def from_argv(cls, argv): return argv\n    def run(self): return 1\n",
                [
                    f"{spec}:6: Spec.from_argv: business method in types module",
                    f"{spec}:7: Spec.run: business method in types module",
                ],
            ),
            *(
                (
                    path,
                    f"from dataclasses import dataclass\n@dataclass\nclass {name}:\n    def to_argv(self): return []\n",
                    [f"{path}:4: {name}.to_argv: business method in types module"],
                )
                for path, name in ((spec, "Other"), ("utils/src/ensemble/utils/types.py", "Spec"))
            ),
        ):
            with self.subTest(source=source):
                self.assertEqual(data_types.types_only(self.tree({path: source})), expected)


class OptionTuples(Sample):
    FAILS = "a fixed option set is a StrEnum (FR-49)"
    STALE = "allowlisted but not an option tuple"

    def found(self, extra=None, *, instead=None):
        files = {}
        for key in OPTION_TUPLES_ALLOWED:
            rel, name = key
            files[rel] = files.get(rel, "") + (instead or {}).get(key, f'{name} = ("a", "b")\n')
        for rel, text in (extra or {}).items():
            files[rel] = files.get(rel, "") + text
        return data_types.option_tuples(self.tree(files))

    def test_the_real_tree_is_clean(self):
        self.assertEqual(data_types.option_tuples(REPO), [])

    def test_allowlisted_tuples_pass_each_with_a_one_line_reason(self):
        self.assertEqual(self.found(), [])
        for key, reason in OPTION_TUPLES_ALLOWED.items():
            with self.subTest(key=key):
                self.assertTrue(reason.strip())
                self.assertNotIn("\n", reason)

    def test_module_level_tuples_of_option_words_fail_in_src_and_standalone_scripts(self):
        rel = f"delegate/{PKG}/delegate/x.py"
        hook = f"session/{PKG}/session/modes/tui_hook.py"
        for extra, expected in (
            (
                {
                    rel: (
                        'X = ("a", "b")\n'
                        '_HOOKS: tuple = ("pane-exited", "window-resized")\n'
                        'Y = Z = ("a_b", "c2", "d")\n'
                        'TIERS = ("1", "2", "3")\n'
                    )
                },
                [
                    f"{rel}:1: X: {self.FAILS}",
                    f"{rel}:2: _HOOKS: {self.FAILS}",
                    f"{rel}:3: Y: {self.FAILS}",
                    f"{rel}:3: Z: {self.FAILS}",
                    f"{rel}:4: TIERS: {self.FAILS}",
                ],
            ),
            (
                {
                    hook: (
                        'if True:\n    A = ("a", "b")\nelse:\n    B = ("a", "b")\n'
                        'try:\n    C = ("a", "b")\nexcept ImportError:\n    D = ("a", "b")\n'
                        'finally:\n    E = ("a", "b")\n'
                    )
                },
                sorted(
                    f"{hook}:{line}: {name}: {self.FAILS}"
                    for line, name in ((2, "A"), (4, "B"), (6, "C"), (8, "D"), (10, "E"))
                ),
            ),
            (
                {
                    rel: (
                        'ONE = ("a",)\n'
                        'MIXED = ("a", 1)\n'
                        'PATHS = ("crew.py", "drive.py")\n'
                        'FLAGS = ("-P", "-m")\n'
                        'UPPER = ("A", "B")\n'
                        'SPACED = ("a b", "c")\n'
                        'VERSIONS = ("1.0", "2.0")\n'
                        'SIGNED = ("-1", "2")\n'
                        'LISTED = ["a", "b"]\n'
                        'NESTED = (("a", "b"), ("c", "d"))\n'
                        'lower = ("a", "b")\n'
                        'Mixed = ("a", "b")\n'
                        "BARE: tuple[str, ...]\n"
                        'A, B = ("a", "b")\n'
                        'def f():\n    LOCAL = ("a", "b")\n'
                        'class C:\n    FIELD = ("a", "b")\n'
                        'with open("x") as f:\n    WITH = ("a", "b")\n'
                    ),
                    "delegate/tests/unit/x_test.py": 'X = ("a", "b")\n',
                    "delegate/skills/tool.py": 'X = ("a", "b")\n',
                    "tests/x.py": 'X = ("a", "b")\n',
                },
                [],
            ),
        ):
            with self.subTest(files=sorted(extra)):
                self.assertEqual(self.found(extra), expected)

    def test_an_allowlisted_name_that_is_no_longer_an_option_tuple_is_stale(self):
        key = rel, name = next(iter(OPTION_TUPLES_ALLOWED))
        for source in (
            "",
            f"{name} = tuple(range(2))\n",
            f'{name} = ("a",)\n',
            f'def f():\n    {name} = ("a", "b")\n',
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    self.found(instead={key: source}), [f"{rel}: {name}: {self.STALE}"]
                )

    def test_every_entry_is_stale_in_an_empty_tree(self):
        self.assertEqual(
            data_types.option_tuples(self.tree({})),
            sorted(f"{rel}: {name}: {self.STALE}" for rel, name in OPTION_TUPLES_ALLOWED),
        )
