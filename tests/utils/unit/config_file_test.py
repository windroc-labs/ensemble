"""
utils/config_file.py: load, merge, read_resources (layers, ownership, the one local file, unknown
keys tolerated), unset, freeze and thaw.
"""

import copy
import importlib.util
import operator
import os
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from ensemble.utils import config_file
from ensemble.utils.config_file import (
    ConfigFileError,
    freeze,
    load,
    merge,
    read_resources,
    thaw,
    tolerate_unknown_keys,
    unset,
)
from tests.utils.case import REPO, Case
from tests.utils.sandbox import scratch


class ConfigFileTest(Case):
    def setUp(self):
        self.root = Path(scratch(self, os.environ.get("ENSEMBLE_TEST_TMP", REPO)))
        self.path = self.root / "config.toml"

    def test_merge_overlays_one_config_on_another(self):
        for base, overlay, want in (
            (
                {"table": {"keep": 1, "change": {"x": 1}, "list": [1]}, "value": 1},
                {"table": {"change": {"y": 2}, "list": [2]}, "value": {"x": 3}},
                {"table": {"keep": 1, "change": {"x": 1, "y": 2}, "list": [2]}, "value": {"x": 3}},
            ),
            (
                {
                    "a": 1,
                    "s": "x",
                    "u": {"k": 1},
                    "t": {"x": 1, "l": [1, 2], "n": {"p": 1, "q": 2}},
                },
                {"b": 2, "s": {"k": 1}, "u": 5, "t": {"y": 2, "l": [3], "n": {"q": 3}}},
                {
                    "a": 1,
                    "b": 2,
                    "s": {"k": 1},
                    "u": 5,
                    "t": {"x": 1, "y": 2, "l": [3], "n": {"p": 1, "q": 3}},
                },
            ),
        ):
            with self.subTest(base=base):
                original = copy.deepcopy((base, overlay))
                self.assertEqual(merge(base, overlay), want)
                self.assertEqual((base, overlay), original)

    def test_load_reads_one_file_and_a_missing_or_malformed_one_raises(self):
        with self.assertRaises(FileNotFoundError):
            load(str(self.path))
        self.path.write_text('value = 1\n[table]\nkeep = "base"\n')
        self.assertEqual(load(str(self.path)), {"value": 1, "table": {"keep": "base"}})
        self.path.write_text("malformed = [\n")
        with self.assertRaises(tomllib.TOMLDecodeError):
            load(str(self.path))


SESSION = '[hosts]\ndefault = "tmux"\n[hosts.tmux]\nstatus_line = false\n'
DELEGATE = 'client = "a"\nshow = ""\nteam_dirs = []\n[roles.pm]\ntier = 2\n[roles.pm.output]\ntype = "local"\n'
ORCH = 'team = ""\nwork_dir = "~/w"\n[roles.pm]\nnote = ""\nmax_runs = 3\n[core.roles.pm]\ngate = "g"\n'


class FrozenTables(Case):
    def test_freeze_is_a_read_only_deep_copy_its_lists_tuples(self):
        source = {"a": {"l": [1, {"b": 2}], "t": (3, [4])}, "n": 5}
        frozen = freeze(source)
        source["a"]["l"][1]["b"] = 9
        source["a"]["l"].append(6)
        source["c"] = 7
        self.assertEqual(frozen, {"a": {"l": (1, {"b": 2}), "t": (3, (4,))}, "n": 5})
        for table, key in ((frozen, "c"), (frozen["a"], "c"), (frozen["a"]["l"][1], "c")):
            with self.subTest(key=key), self.assertRaises(TypeError):
                operator.setitem(table, key, 1)
        self.assertFalse(hasattr(frozen["a"]["l"], "append"))
        self.assertEqual(freeze(frozen), frozen)

    def test_thaw_is_a_fresh_mutable_copy_lists_and_tuples_as_given(self):
        source = {"a": {"l": [1, [2]], "t": (3, [4])}}
        frozen = freeze(source)
        first, second = thaw(frozen), thaw(freeze(frozen))
        self.assertEqual((first, second), (source, source))
        self.assertEqual([type(first["a"][k]) for k in ("l", "t")], [list, tuple])
        self.assertEqual((type(first), type(first["a"]["t"][1])), (dict, list))
        first["a"]["l"].append(5)
        self.assertEqual(second, source)
        self.assertIsNot(thaw(source)["a"]["l"], source["a"]["l"])


class Resources(Case):
    def setUp(self):
        self.root = scratch(self, real=False)
        self.package = "config_fixture_" + Path(self.root).name.replace("-", "_")
        for section, text in (
            ("session", SESSION),
            ("delegate", DELEGATE),
            ("orchestrator", ORCH),
        ):
            folder = Path(self.root, self.package, section)
            folder.mkdir(parents=True)
            (folder / "__init__.py").write_text("")
            (folder / "config.toml").write_text(text)
        modules = {}
        for section in ("session", "delegate", "orchestrator"):
            name = f"{self.package}.{section}"
            spec = importlib.util.spec_from_file_location(
                name, Path(self.root, self.package, section, "__init__.py")
            )
            modules[name] = importlib.util.module_from_spec(spec)
        self.enterContext(mock.patch("importlib.import_module", side_effect=modules.__getitem__))
        self.resources = [
            (f"{self.package}.{section}", "config.toml")
            for section in ("session", "delegate", "orchestrator")
        ]

    def local(self, text):
        Path(self.ensemble, "config.local.toml").write_text(text)

    def test_utils_without_a_resource_has_no_local_keys(self):
        self.local("[utils]\nunknown = 1\n")
        with self.assertRaisesRegex(ConfigFileError, "unknown key 'unknown' in \\[utils\\]"):
            read_resources(self.resources[:1])
        self.local("[utils]\n")
        self.assertEqual(
            read_resources(self.resources[:1]), read_resources(self.resources[:1], local=os.devnull)
        )

    def test_reads_resources_and_interleaves_each_local_section(self):
        self.local('[delegate.roles.pm]\ntier = 1\n[orchestrator.roles.pm]\ntier = 3\nnote = "n"\n')
        found = read_resources(iter(self.resources))
        self.assertEqual(
            found["roles"]["pm"],
            {"tier": 3, "output": {"type": "local"}, "note": "n", "max_runs": 3},
        )
        self.assertEqual(read_resources(self.resources[:2])["roles"]["pm"]["tier"], 1)
        self.assertEqual(found["hosts"]["default"], "tmux")

    def test_upper_packaged_values_override_lower_local_values(self):
        self.local('[delegate.roles.pm]\nnote = "not-known-at-this-layer"\n')
        self.assertEqual(read_resources(self.resources)["roles"]["pm"]["note"], "")
        self.local('[delegate]\nclient = "lower-local"\n[delegate.roles.pm]\ntier = 1\n')
        Path(self.root, self.package, "orchestrator", "config.toml").write_text(
            ORCH.replace('note = ""', 'tier = 3\nnote = ""')
        )
        self.assertEqual(read_resources(self.resources)["roles"]["pm"]["tier"], 3)
        self.assertEqual(read_resources(self.resources)["client"], "lower-local")

    def test_only_supplied_layers_are_validated(self):
        self.local(
            "[session.hosts.tmux]\nstatus_line = true\n[delegate]\nanything = 1\n[orchestrator]\nunknown = true\n"
        )
        self.assertTrue(read_resources(self.resources[:1])["hosts"]["tmux"]["status_line"])
        with self.assertRaisesRegex(ConfigFileError, "unknown key 'anything'"):
            read_resources(self.resources[:2])
        self.local("[session]\nunknown = 1\n")
        with self.assertRaisesRegex(ConfigFileError, "unknown key 'unknown'"):
            read_resources(self.resources[:1])

    def test_unprovided_layer_tables_need_no_project_inventory(self):
        self.local(
            "[plugin_settings]\nanything = 1\n[other_app]\nunknown = true\n[session.hosts.tmux]\nstatus_line = true\n"
        )
        self.assertEqual(
            read_resources(self.resources[:1]),
            {"hosts": {"default": "tmux", "tmux": {"status_line": True}}},
        )

    def test_path_replaces_one_resource_with_defaults_as_schema(self):
        override = Path(self.root, "override.toml")
        override.write_text('client = "override"\nnew_key = "default"\n')
        self.local('[delegate]\nnew_key = "local"\n[delegate.roles.pm]\ntier = 1\n')
        found = read_resources(
            iter(self.resources[:2]), paths={self.resources[1][0]: str(override)}
        )
        self.assertEqual(
            (found["client"], found["new_key"], found["roles"]["pm"]),
            ("override", "local", {"tier": 1}),
        )
        with self.assertRaises(FileNotFoundError):
            read_resources(
                self.resources[:2], paths={self.resources[1][0]: str(override) + ".missing"}
            )
        with self.assertRaisesRegex(ConfigFileError, "no resource"):
            read_resources(self.resources[:1], paths={"absent": str(override)})

    def test_owner_validates_each_raw_resource_once_before_local_overlay(self):
        override = Path(self.root, "override.toml")
        override.write_text('client = "own-file"\n')
        self.local('[delegate]\nclient = "local"\n')
        seen = []

        def validate(package, source, table):
            seen.append((package, source, copy.deepcopy(table)))

        found = read_resources(
            self.resources[:2],
            paths={self.resources[1][0]: str(override)},
            validate=validate,
        )
        self.assertEqual(found["client"], "local")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][0], self.resources[0][0])
        self.assertEqual(seen[0][2], tomllib.loads(SESSION))
        self.assertEqual(seen[1], (self.resources[1][0], str(override), {"client": "own-file"}))

    def test_owner_validation_failure_propagates_without_local_replacing_it(self):
        self.local('[delegate]\nclient = "local"\n')
        error = ValueError("owner rejected raw configuration")

        def validate(package, _source, table):
            if package == self.resources[1][0] and table["client"] == "a":
                raise error

        with self.assertRaises(ValueError) as caught:
            read_resources(self.resources[:2], validate=validate)
        self.assertIs(caught.exception, error)

    def test_flat_migration_hints_match_key_paths_and_never_choose_utils(self):
        unrelated = DELEGATE + '[clients.writer]\nnote = ""\n'
        Path(self.root, self.package, "delegate", "config.toml").write_text(unrelated)
        extra = 'typo_key = ""\n' + ORCH
        Path(self.root, self.package, "orchestrator", "config.toml").write_text(extra)
        for text, key, hint in (
            (
                'typo_key = "secret-service"\n',
                "typo_key",
                "move it under [orchestrator]",
            ),
            (
                '[roles.engineer]\nnote = "n"\n',
                "roles.engineer.note",
                "move it under [orchestrator]",
            ),
            (
                'unknown_key = "x"\n',
                "unknown_key",
                "no supplied layer has it; move it under its owning layer's section",
            ),
            ('client = "x"\n', "client", "move it under [delegate]"),
            (
                "[hosts.tmux]\nstatus_line = true\n",
                "hosts.tmux.status_line",
                "move it under [session]",
            ),
        ):
            self.local(text)
            with self.subTest(text=text), self.assertRaises(ConfigFileError) as caught:
                read_resources(self.resources)
            self.assertEqual(
                str(caught.exception),
                f"~/.ensemble/config.local.toml: unknown key {key!r}: flat local configuration; {hint}",
            )
            self.assertNotIn("[utils]", str(caught.exception))

    def test_misplaced_or_unknown_keys_are_errors(self):
        for text, error in (
            ("[delegate]\nunknown_field = 1\n", r"unknown key 'unknown_field' in \[delegate\]"),
            (
                "session = 1\n",
                r"^~/.ensemble/config.local.toml: \[session\]: want a table$",
            ),
            ("utils = 1\n", r"^~/.ensemble/config.local.toml: \[utils\]: want a table$"),
        ):
            self.local(text)
            with self.subTest(text=text), self.assertRaisesRegex(ConfigFileError, error):
                read_resources(self.resources)

    def test_tolerated_unknown_keys_are_reported_and_dropped(self):
        self.local('[delegate]\nnewer_field = 1\nclient = "y"\n[session]\nnewer = {x = 1}\n')
        with self.assertRaisesRegex(ConfigFileError, r"unknown key 'newer' in \[session\]"):
            read_resources(self.resources)
        reported = []
        with tolerate_unknown_keys(reported.append):
            cfg = read_resources(self.resources)
        self.assertEqual(
            reported,
            [
                "~/.ensemble/config.local.toml: unknown key 'newer' in [session]",
                "~/.ensemble/config.local.toml: unknown key 'newer_field' in [delegate]",
            ],
        )
        self.assertNotIn("newer_field", cfg)
        self.assertNotIn("newer", cfg)
        self.assertEqual(cfg["client"], "y")
        with self.assertRaisesRegex(ConfigFileError, "newer"):
            read_resources(self.resources)

    def test_tolerating_unknown_keys_keeps_every_other_error(self):
        for text in ('client = "x"\n', "session = 1\n"):
            self.local(text)
            with (
                self.subTest(text=text),
                tolerate_unknown_keys(self.fail),
                self.assertRaises(ConfigFileError),
            ):
                read_resources(self.resources)

    def test_flat_key_errors_with_supplied_layers(self):
        for text, layers, message in (
            (
                'typo_key = "x"\n',
                1,
                "~/.ensemble/config.local.toml: unknown key 'typo_key': flat local configuration; no supplied layer has it; move it under its owning layer's section",
            ),
            (
                'typo_key = "unprovided"\nsession = 1\n',
                1,
                "~/.ensemble/config.local.toml: [session]: want a table",
            ),
            (
                '[delegate]\nruntime = "codex"\n',
                2,
                "~/.ensemble/config.local.toml: unknown key 'runtime' in [delegate]; renamed to 'client'",
            ),
            (
                '[delegate.accounts]\nclaude = "work"\n',
                2,
                "~/.ensemble/config.local.toml: unknown key 'accounts' in [delegate]; moved to [session.clients.<client>] account",
            ),
        ):
            self.local(text)
            with self.subTest(text=text):
                with self.assertRaises(ConfigFileError) as caught:
                    read_resources(self.resources[:layers])
                self.assertEqual(str(caught.exception), message)

    def test_nested_keys_are_passed_to_their_owner(self):
        self.local(
            "[delegate.roles.pm]\nunknown_field = 1\n[delegate.roles.pm.unknown]\ntier = 1\n"
        )
        self.assertEqual(
            read_resources(self.resources)["roles"]["pm"],
            {
                "tier": 2,
                "output": {"type": "local"},
                "unknown_field": 1,
                "unknown": {"tier": 1},
                "note": "",
                "max_runs": 3,
            },
        )

    def test_empty_table_schema_supports_new_named_instances(self):
        Path(self.root, self.package, "session", "config.toml").write_text(
            SESSION + "[clients.stub.env]\n"
        )
        self.local('[session.clients.other.env]\nCUSTOM = "value"\n')
        self.assertEqual(
            read_resources(self.resources[:1])["clients"]["other"]["env"], {"CUSTOM": "value"}
        )

    def test_the_local_file(self):
        self.assertEqual(config_file.LOCAL, "~/.ensemble/config.local.toml")
        local = Path(self.root, "mine.toml")
        local.write_text('[delegate]\nclient = "q"\n')
        self.assertEqual(read_resources(self.resources[:2], local=str(local))["client"], "q")

    def test_local_goes_over_every_table_merging_and_lists_replaced_whole(self):
        self.local(
            '[session.hosts.tmux]\nstatus_line = true\n[delegate]\nclient = "b"\nteam_dirs = ["~/t"]\n[delegate.roles.pm]\ntier = 1\n[orchestrator]\nteam = "T"\n[orchestrator.roles.pm]\nnote = "n"\n'
        )
        found = read_resources(self.resources)
        self.assertEqual(
            (found["client"], found["team_dirs"], found["team"], found["hosts"]["tmux"]),
            ("b", ["~/t"], "T", {"status_line": True}),
        )
        self.assertEqual(
            found["roles"]["pm"],
            {"tier": 1, "output": {"type": "local"}, "note": "n", "max_runs": 3},
        )

    def test_a_new_table_under_a_known_one_is_taken_as_its_owners(self):
        self.local('[session.hosts.other]\nimplementation = "a.b:C"\n[session.clients]\n')
        with self.assertRaisesRegex(ConfigFileError, r"unknown key 'clients' in \[session\]"):
            read_resources(self.resources[:1])
        self.local('[session.hosts.other]\nimplementation = "a.b:C"\n')
        self.assertEqual(
            read_resources(self.resources[:1])["hosts"]["other"], {"implementation": "a.b:C"}
        )

    def test_a_new_empty_local_table_is_dropped(self):
        self.local("[delegate.roles.ghost]\n")
        self.assertNotIn("ghost", read_resources(self.resources)["roles"])
        self.assertNotIn("ghost", read_resources(self.resources[:2])["roles"])

    def test_opaque_values_survive_extraction_and_replace_complete_subtrees(self):
        paths = (("roles", "*", "payload"),)
        packaged = Path(self.root, self.package, "delegate", "config.toml")
        packaged.write_text(DELEGATE + "[roles.pm.payload]\nold = { x = 1 }\n")
        for source, expected in (("{}", {}), ("{ future = {} }", {"future": {}})):
            with self.subTest(source=source):
                self.local(f"[delegate.roles.pm]\npayload = {source}\n")
                found = read_resources(self.resources, opaque=paths)
                self.assertEqual(found["roles"]["pm"]["payload"], expected)
        self.local("")
        Path(self.root, self.package, "orchestrator", "config.toml").write_text(
            ORCH + "[roles.pm.payload]\nnext = {}\n"
        )
        found = read_resources(self.resources, opaque=paths)
        self.assertEqual(found["roles"]["pm"]["payload"], {"next": {}})

    def test_a_malformed_local_file_propagates(self):
        self.local("x = [\n")
        with self.assertRaises(tomllib.TOMLDecodeError):
            read_resources(self.resources[:2])


class Unset(Case):
    def test_missing_or_empty_required_keys_by_dotted_key_a_star_each_subtable(self):
        cfg = {
            "team": "",
            "key": "k",
            "states": {"a": "1", "b": ""},
            "roles": {"x": {"note": ""}, "y": {}},
        }
        self.assertEqual(
            unset(cfg, ("team", "key", "gone", "states.a", "states.b", "states.c", "roles.*.note")),
            ["team", "gone", "states.b", "states.c", "roles.x.note", "roles.y.note"],
        )
        self.assertEqual(unset({}, ("roles.*.note",)), [])


if __name__ == "__main__":
    unittest.main()
