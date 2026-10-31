import json
import os
import sys
from dataclasses import dataclass

from tests.case import REPO
from tests.guard_checks import exports
from tests.guard_data import OBJECT_EXPORTS_ALLOWED
from tests.guard_data.delegate import DELEGATE_INTERFACE, DELEGATE_INTERFACE_MODULES
from tests.guard_data.session import SESSION_INTERFACE, SESSION_INTERFACE_MODULES
from tests.guard_samples import PKG, Sample


class EventObjectExports(Sample):
    EVENT = "src/ensemble/delegate/event/"

    def exports(self, root):
        return [entry for entry in exports.object_exports(root) if entry.startswith(self.EVENT)]

    def test_seven_callable_or_mutated_export_bypasses_fail(self):
        for sample in (
            "import functools\ndef f(): pass\naction = functools.partial(f)\n__all__ = ['action']\n",
            "action = staticmethod(lambda: None)\n__all__ = ['action']\n",
            "def f(): pass\naction = f if True else f\n__all__ = ['action']\n",
            "import os\naction = getattr(os, 'getcwd')\n__all__ = ['action']\n",
            "def f(): pass\n__all__ = []\n__all__ += ['f']\n",
            "def f(): pass\n__all__ = []\n__all__.append('f')\n",
            "def f(): pass\n__all__ = []\nif True:\n    __all__ = ['f']\n",
        ):
            with self.subTest(sample=sample):
                self.assertTrue(self.exports(self.tree({self.EVENT + "__init__.py": sample})))

    def test_a_literal_all_exporting_no_function(self):
        init = self.EVENT + "__init__.py"
        no_all = [f"{init}: no literal __all__"]

        def function(name):
            return [f"{init}: __all__ exports function {name}"]

        writes = (
            "import sys, builtins\nfrom builtins import setattr as write\n"
            "def f(): pass\nCONSTANT = 1\n__all__ = ['CONSTANT']\n"
        )
        for files, expected in (
            *(
                ({init: "def f(): pass\nCONSTANT = 1\n__all__ = ['CONSTANT']\n" + mutation}, no_all)
                for mutation in (
                    "__all__[0] = 'f'",
                    "__all__[:] = ['f']",
                    "__all__[0] += 'f'",
                    "del __all__[0]",
                )
            ),
            *(
                ({init: writes + source}, no_all)
                for mutation in (
                    "globals()['__all__'] = ['f']",
                    "globals()['__all__'] += ['f']",
                    "globals()['__all__'].append('f')",
                    "del globals()['__all__']",
                    "setattr(sys.modules[__name__], '__all__', ['f'])",
                    "builtins.setattr(sys.modules[__name__], '__all__', ['f'])",
                    "write(sys.modules[__name__], '__all__', ['f'])",
                    "delattr(sys.modules[__name__], '__all__')",
                    "sys.modules[__name__].__all__ = ['f']",
                    "sys.modules[__name__].__all__ += ['f']",
                    "sys.modules[__name__].__all__.append('f')",
                    "del sys.modules[__name__].__all__",
                )
                for source in (mutation, "def mutate():\n    " + mutation)
            ),
            (
                {
                    init: (
                        "import sys\nCONSTANT = 1\n__all__ = ['CONSTANT']\n"
                        "copy = globals()['__all__']\n"
                        "same = sys.modules[__name__].__all__\n"
                        "name = getattr(sys.modules[__name__], '__all__')\n"
                    )
                },
                [],
            ),
            ({init: "class Event: pass\n"}, no_all),
            ({}, no_all),
            *(({init: sample}, no_all) for sample in ("__all__ = names\n", "__all__ = [123]\n")),
            *(
                ({init: definition + "__all__ = ['action']\n"}, function("action"))
                for definition in (
                    "def action(): pass\n",
                    "async def action(): pass\n",
                    "action = lambda: None\n",
                    "action = 'constant'\ndef action(): pass\n",
                    "action = ValueError\ndef action(): pass\n",
                )
            ),
            (
                {
                    init: (
                        "from .bridge import action as public\n"
                        "renamed = public\n__all__ = ['renamed']\n"
                    ),
                    self.EVENT + "bridge.py": "from .reply import parse as action\n",
                    self.EVENT + "reply.py": "def parse(): pass\n",
                },
                function("renamed"),
            ),
            (
                {
                    init: (
                        "import ensemble.delegate.event.reply as replies\n"
                        "public = replies.parse\n__all__ = ['public']\n"
                    ),
                    self.EVENT + "reply.py": "def parse(): pass\n",
                },
                function("public"),
            ),
            (
                {
                    init: (
                        "class Event:\n    @classmethod\n    def parse(cls): pass\n"
                        "public = Event.parse\n__all__ = ['public']\n"
                    )
                },
                function("public"),
            ),
            (
                {
                    init: (
                        "from .reply import Event, EventError, TAG\n"
                        "def helper(): pass\n__all__: list[str] = ['Event', 'EventError', 'TAG']\n"
                    ),
                    self.EVENT + "reply.py": (
                        "class Event:\n    def parse(self): pass\n"
                        "class EventError(ValueError): pass\nTAG = 'event'\n"
                    ),
                },
                [],
            ),
            (
                {
                    init: (
                        "from pathlib import Path\nfrom builtins import ValueError, Ellipsis\n"
                        "from string import ascii_letters as LETTERS\n"
                        "__all__ = ['Path', 'ValueError', 'Ellipsis', 'LETTERS']\n"
                    )
                },
                [],
            ),
            ({init: "id = 'event'\n__all__ = ['id']\n"}, []),
            (
                {init: "class Item: pass\nAlias = Item\ndef Item(): pass\n__all__ = ['Alias']\n"},
                [],
            ),
            (
                {init: "def action(): pass\nAlias = action\naction = 1\n__all__ = ['Alias']\n"},
                function("Alias"),
            ),
            (
                {init: "from unknown_package import ACTION\n__all__ = ['ACTION']\n"},
                function("ACTION"),
            ),
            (
                {
                    init: "__all__ = []\n",
                    **{
                        f"src/ensemble/{package}/__init__.py": "def run(): pass\n__all__ = ['run']\n"
                        for project, package in (
                            ("utils", "utils"),
                            ("session", "session"),
                            ("delegate", "delegate"),
                            ("delegate", "delegate/composer"),
                            ("manager", "manager"),
                        )
                    },
                },
                [],
            ),
        ):
            with self.subTest(files=files):
                self.assertEqual(self.exports(self.tree(files)), expected)

    def test_standard_library_functions_and_uppercase_aliases_are_rejected(self):
        for source in ("json.loads", "os.getcwd", "builtins.len"):
            with self.subTest(source=source):
                module, _, name = source.rpartition(".")
                root = self.tree(
                    {
                        self.EVENT
                        + "__init__.py": f"from {module} import {name} as ACTION\n__all__ = ['ACTION']\n"
                    }
                )
                found = self.exports(root)
                self.assertEqual(len(found), 1)
                self.assertIn("__init__.py:", found[0])
                self.assertIn("ACTION", found[0])

    def test_builtin_aliases_and_bare_builtin_functions_are_rejected(self):
        for sample in ("ACTION = len\n__all__ = ['ACTION']\n", "__all__ = ['len']\n"):
            with self.subTest(sample=sample):
                found = self.exports(self.tree({self.EVENT + "__init__.py": sample}))
                self.assertEqual(len(found), 1)
                self.assertIn("exports function", found[0])

    def test_standard_library_source_is_read_without_importing_it(self):
        before = sys.modules.get("antigravity")
        root = self.tree(
            {self.EVENT + "__init__.py": "from antigravity import geohash\n__all__ = ['geohash']\n"}
        )
        self.assertEqual(
            self.exports(root),
            [self.EVENT + "__init__.py: __all__ exports function geohash"],
        )
        self.assertIs(sys.modules.get("antigravity"), before)


class DelegateObjectExports(Sample):
    PACKAGES = ("ensemble.delegate", "ensemble.delegate.composer")
    UNRECOGNISED = {
        "PATH": "PATH: str = os.path.join('a', 'b')\n",
        "COUNT": "COUNT: int = int('1')\n",
        "CHOICES": "class Choice(StrEnum):\n    A = 'a'\nCHOICES = tuple(Choice)\n",
        "FROZEN": "FROZEN = frozenset(('a', 'b'))\n",
        "HOME": "HOME = os.path.dirname(os.path.abspath(__file__))\n",
        "ROOTS": "ROOTS = os.pathsep.join(('a', 'b'))\n",
    }

    def exports(self, package, extra="", names=()):
        rel = "src/" + package.replace(".", "/") + "/__init__.py"
        sample = "import os\nfrom enum import StrEnum\n" + extra + f"__all__ = {list(names)!r}\n"
        root = self.tree({rel: sample})
        return [
            item
            for item in exports.object_exports(root)
            if item.startswith(rel + ":") or item.startswith(package + ":")
        ]

    def test_pinned_inventory_can_only_shrink(self):
        self.assertFalse({key for key in OBJECT_EXPORTS_ALLOWED if key[0] in self.PACKAGES})

    def test_only_pinned_functions_and_objects_are_allowed(self):
        for package in self.PACKAGES:
            with self.subTest(package=package):
                self.assertEqual(self.exports(package, "class Public: pass\n", ["Public"]), [])
                self.assertEqual(
                    len(self.exports(package, "def new_function(): pass\n", ["new_function"])),
                    1,
                )
                self.assertEqual(
                    len(self.exports(package, "def helper(): pass\nAlias = helper\n", ["Alias"])),
                    1,
                )

    def test_missing_all_is_rejected_for_both_packages(self):
        root = self.tree({})
        for package in self.PACKAGES:
            rel = "src/" + package.replace(".", "/") + "/__init__.py"
            self.assertIn(f"{rel}: no literal __all__", exports.object_exports(root))

    def test_literals_and_constant_names_pass_but_callables_and_constructor_calls_fail(self):
        literals = (
            "import os\n"
            "NAME = 'x'\n"
            "LABEL: str = 'label'\n"
            "TIMEOUT = 2 * 60 * 60\n"
            "TOTAL = TIMEOUT * 2\n"
            "COPY = NAME\n"
            "SEPARATORS = (os.sep, '/')\n"
        )
        for package in self.PACKAGES:
            rel = "src/" + package.replace(".", "/") + "/__init__.py"
            for source, names, expected in (
                *(
                    (source, [name], [f"__all__ exports unrecognised value {name}"])
                    for name, source in self.UNRECOGNISED.items()
                ),
                (literals, ["NAME", "LABEL", "TIMEOUT", "TOTAL", "COPY", "SEPARATORS"], []),
                *(
                    (bad, ["BAD"], ["__all__ exports function BAD"])
                    for bad in (
                        "BAD: str = lambda: 'value'\n",
                        "def helper(): pass\nBAD: str = helper\n",
                    )
                ),
            ):
                with self.subTest(package=package, source=source):
                    self.assertEqual(
                        self.exports(package, source, names), [f"{rel}: {e}" for e in expected]
                    )


class OrchestratorObjectExports(Sample):
    INIT = "src/ensemble/orchestrator/__init__.py"

    def found(self, source):
        root = self.tree(
            {
                "src/ensemble/delegate/event/__init__.py": "__all__ = []\n",
                "src/ensemble/orchestrator/engine/run.py": (
                    "class LinearRun: pass\ndef launch(): pass\n"
                ),
                self.INIT: source,
            }
        )
        return [e for e in exports.object_exports(root) if e.startswith(self.INIT)]

    def test_classes_and_constants_pass_but_functions_and_a_missing_all_fail(self):
        for source, expected in (
            (
                "from ensemble.orchestrator.engine.run import LinearRun\n"
                "KINDS = ('a',)\n__all__ = ['LinearRun', 'KINDS']\n",
                [],
            ),
            *(
                (source, [f"{self.INIT}: __all__ exports function launch"])
                for source in (
                    "def launch(): pass\n__all__ = ['launch']\n",
                    "from ensemble.orchestrator.engine.run import launch\n__all__ = ['launch']\n",
                )
            ),
            (
                "from ensemble.orchestrator.engine.run import LinearRun\n",
                [f"{self.INIT}: no literal __all__"],
            ),
        ):
            with self.subTest(source=source):
                self.assertEqual(self.found(source), expected)

    def test_a_missing_package_init_fails(self):
        root = self.tree({"src/ensemble/delegate/event/__init__.py": "__all__ = []\n"})
        self.assertIn(f"{self.INIT}: no literal __all__", exports.object_exports(root))


class ObjectExports(Sample):
    SESSION = f"{PKG}/session/"
    INIT = SESSION + "__init__.py"
    EXTENSION = SESSION + "extension.py"
    ENTRY_EXPORTS = ("SESSION_NAME", "SESSION_NAME_RULE", "get_viewer")
    BAD = ("OS", "Shut", "modes", "f", "g", "LAMBDA", "Ghost")
    BAD_SOURCE = (
        "import os as OS\n"
        "from ensemble.session.engine.config import read_config as Shut\n"
        "from ensemble.session import modes\n"
        "def f(): pass\n"
        "g = f\n"
        "LAMBDA = lambda: 0\n"
    )

    def init(self, source="", names=()):
        """A session __init__ exporting its allowlisted entries, then `names`."""
        imported = (
            "from ensemble.session.engine.config import read_config\n"
            "import re\nSESSION_NAME = re.compile('x')\nSESSION_NAME_RULE = str('x')\n"
            "def get_viewer(): pass\n"
        )
        return f"{imported}{source}__all__ = {[*self.ENTRY_EXPORTS, *names]!r}\n"

    def found(self, files=None):
        tree = {
            "src/ensemble/delegate/event/__init__.py": "__all__ = []\n",
            self.SESSION + "engine/config.py": "def read_config(): pass\n",
            self.SESSION + "modes/__init__.py": "",
            self.INIT: self.init(),
            self.EXTENSION: "__all__ = []\n",
            **(files or {}),
        }
        return [
            entry
            for entry in exports.object_exports(self.tree(tree))
            if entry.startswith(self.SESSION) or entry.startswith("ensemble.session:")
        ]

    def test_the_allowlisted_entries_pass_each_with_a_one_line_reason(self):
        self.assertEqual(self.found(), [])
        self.assertEqual(
            {key for key in OBJECT_EXPORTS_ALLOWED if key[0] == "ensemble.session"},
            {("ensemble.session", n) for n in self.ENTRY_EXPORTS},
        )
        for key, reason in OBJECT_EXPORTS_ALLOWED.items():
            with self.subTest(key=key):
                self.assertTrue(reason.strip())
                self.assertNotIn("\n", reason)

    def test_entry_modules_export_a_literal_all_of_classes_constants_and_allowed_functions(self):
        conditional = "import functools\ndef f(): pass\nalias = f if True else f\npart = functools.partial(f)\n"
        aliases = ("alias", "part")
        extension = (
            "import signal\n"
            "from pathlib import Path\n"
            "from ensemble.session.engine.types import Error, Event, Mode, Spec\n"
            "from ensemble.utils import Display\n"
            "Alias = Spec\n"
            "SIGNALS = (signal.SIGHUP, signal.SIGTERM)\n"
            'NAME = "x"\n'
        )
        names = ["Spec", "Mode", "Error", "Event", "Display", "Alias", "SIGNALS", "NAME", "Path"]
        types = (
            "from enum import StrEnum\n"
            "class Spec: pass\n"
            'class Mode(StrEnum):\n    A = "a"\n'
            "class Error(Exception): pass\n"
            "Event = Spec | Mode\n"
        )
        utils = f"{PKG}/utils/"
        stale = "allowlisted but not an exported function or value"
        for files, expected in (
            *(
                (
                    {rel: text},
                    sorted(f"{rel}: __all__ exports function {n}" for n in self.BAD),
                )
                for rel, text in (
                    (self.INIT, self.init(self.BAD_SOURCE, self.BAD)),
                    (self.EXTENSION, f"{self.BAD_SOURCE}__all__ = {list(self.BAD)!r}\n"),
                )
            ),
            *(
                (
                    {rel: text},
                    sorted(f"{rel}: __all__ exports unrecognised value {n}" for n in aliases),
                )
                for rel, text in (
                    (self.INIT, self.init(conditional, aliases)),
                    (self.EXTENSION, f"{conditional}__all__ = {list(aliases)!r}\n"),
                )
            ),
            (
                {
                    self.EXTENSION: f"{extension}__all__ = {names!r}\n",
                    self.SESSION + "engine/types.py": types,
                    utils + "__init__.py": "from ensemble.utils.record import Display\n"
                    'def helper(): pass\n__all__ = ["Display", "helper"]\n',
                    utils + "record.py": "Display = str | None\n",
                },
                [],
            ),
            *(
                ({self.EXTENSION: source}, [f"{self.EXTENSION}: no literal __all__"])
                for source in ("", "__all__ = names()\n", "__all__ = [1]\n", '__all__ = ("A",)\n')
            ),
            (
                {self.INIT: 'class read_config: pass\n__all__ = ["read_config"]\n'},
                sorted(f"ensemble.session: {name}: {stale}" for name in self.ENTRY_EXPORTS),
            ),
        ):
            with self.subTest(files=files):
                self.assertEqual(self.found(files), expected)


class AnnotatedInterface:
    __module__ = "ensemble.test_interface"
    sid: str
    property_name: str
    _private: str

    def __init__(self):
        self.name = "name"
        self.sid = "sid"
        self._hidden = "hidden"

        def nested(self):
            self.unrelated = 1

        self.callback = nested

    @property
    def property_name(self):
        return "name"


@dataclass(slots=True)
class DataclassInterface(AnnotatedInterface):
    __module__ = "ensemble.test_interface"
    field: str
    default: int = 1


class InterfaceAttributes(Sample):
    def test_annotations_and_initialized_public_attributes_are_included_once(self):
        found = exports._interface_members(AnnotatedInterface)
        self.assertEqual(
            found,
            {
                "sid": "attribute",
                "name": "attribute",
                "callback": "attribute",
                "property_name": "property",
            },
        )

    def test_dataclass_fields_and_inherited_attributes_are_included(self):
        found = exports._interface_members(DataclassInterface)
        self.assertEqual(
            found,
            {
                "sid": "attribute",
                "name": "attribute",
                "callback": "attribute",
                "property_name": "property",
                "field": "attribute",
                "default": "attribute",
            },
        )


class InterfaceSnapshot:
    """An interface snapshot guard's tests: PATH its file, KEY a frozen signature, GONE a new name."""

    def snapshot(self) -> dict:
        with open(os.path.join(REPO, self.PATH), encoding="utf-8") as f:
            return json.load(f)

    def checked(self, snapshot) -> list[str]:
        return self.guard(self.tree({self.PATH: json.dumps(snapshot)}))

    def test_a_changed_removed_or_added_export_or_signature_fails(self):
        path, key, gone = self.PATH, self.KEY, self.GONE
        snapshot = self.snapshot()
        have = snapshot[key]
        for changed, want in (
            (
                {**snapshot, key: "def(self:POSITIONAL_OR_KEYWORD)"},
                f"{path}: {key}: def(self:POSITIONAL_OR_KEYWORD) -> {have}",
            ),
            ({k: v for k, v in snapshot.items() if k != key}, f"{path}: {key}: absent -> {have}"),
            ({**snapshot, gone: "def()"}, f"{path}: {gone}: def() -> absent"),
        ):
            with self.subTest(want=want):
                self.assertEqual(self.checked(changed), [want])

    def test_a_missing_snapshot_fails(self):
        self.assertEqual(self.guard(self.tree({"README.md": ""})), [f"{self.PATH}: missing"])


class SessionInterfaceSnapshot(InterfaceSnapshot, Sample):
    PATH, KEY, GONE = SESSION_INTERFACE, "ensemble.session.Session.show", "ensemble.session.Gone"
    guard = staticmethod(exports.session_interface_snapshot)

    def test_the_snapshot_covers_both_entry_modules_and_their_signatures(self):
        snapshot = self.snapshot()
        self.assertEqual(
            SESSION_INTERFACE_MODULES, ("ensemble.session", "ensemble.session.extension")
        )
        self.assertEqual(
            snapshot["ensemble.session.extension.Host.worker"],
            "def(self:POSITIONAL_OR_KEYWORD, name:POSITIONAL_OR_KEYWORD, "
            "launch:POSITIONAL_OR_KEYWORD, terminal:KEYWORD_ONLY, display:KEYWORD_ONLY=None, "
            "events:KEYWORD_ONLY=None, metadata:KEYWORD_ONLY=None)",
        )
        self.assertIn("ensemble.session.Session.show", snapshot)

    def test_public_annotation_and_constructor_attributes_are_frozen(self):
        snapshot = self.snapshot()
        for key in (
            "ensemble.session.Session.sid",
            "ensemble.session.Session.name",
            "ensemble.session.Host.name",
            "ensemble.session.Host.settings",
        ):
            with self.subTest(key=key):
                self.assertEqual(snapshot[key], "attribute")
                self.assertEqual(
                    self.checked({k: v for k, v in snapshot.items() if k != key}),
                    [f"{SESSION_INTERFACE}: {key}: absent -> attribute"],
                )


class DelegateInterfaceSnapshot(InterfaceSnapshot, Sample):
    PATH, KEY, GONE = (
        DELEGATE_INTERFACE,
        "ensemble.delegate.Delegate.resume",
        "ensemble.delegate.run",
    )
    guard = staticmethod(exports.delegate_interface_snapshot)

    def test_the_snapshot_covers_the_delegate_package_and_its_signatures(self):
        snapshot = self.snapshot()
        self.assertEqual(DELEGATE_INTERFACE_MODULES, ("ensemble.delegate",))
        self.assertIn("ensemble.delegate.HarnessDelegate", snapshot)
        self.assertEqual(
            snapshot["ensemble.delegate.Delegate.resume"],
            "def(self:POSITIONAL_OR_KEYWORD, mode:KEYWORD_ONLY=None, input:KEYWORD_ONLY=None)",
        )
        self.assertEqual(
            snapshot["ensemble.delegate.Delegate.attach"],
            "classmethod(cls:POSITIONAL_OR_KEYWORD, record_or_workdir:POSITIONAL_OR_KEYWORD, "
            "sid:KEYWORD_ONLY=None)",
        )
        self.assertIn("ensemble.delegate.DelegateEvent", snapshot)
