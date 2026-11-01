"""composer/teams.py: Team, team dirs, roles and task indexes, read whole through Composer.index (ported
from the old composer's Index, TeamDirs and catalog tests) and the team dir checks (the old driver's
TeamRuns).
"""

import os
import re
from unittest import mock

from ensemble.delegate.composer import Composer, ConfigError
from ensemble.delegate.composer.base import TEAM
from ensemble.delegate.composer.teams import (
    TeamKind,
    charter,
    check_all,
    check_overlap,
    check_team_dirs,
    index,
    lookup,
    read,
    role_tables,
    roles,
    tasks_dir,
    tasks_path,
    team,
    team_dirs,
    team_paths,
    used,
)
from ensemble.delegate.engine.config import read_config
from tests.delegate.case import Case
from tests.delegate.unit.composer.shipped import catalog
from tests.delegate.unit.composer.teams import INTRO, POET, STEPS, VERSE, WRITER, FakeTeam

CONFIG = """\
tier = 2
effort = "high"
idle_timeout = 60
show = ""

[output]
type = "local"

[roles.writer]
effort = "medium"

[roles.editor]
tier = 1
"""
BUILT_IN = {
    "writer": {
        "short-note": "a note; most times; light.",
        "long-note": "a long note; when asked; heavy.",
    },
    "editor": {"long-note": "a long note; always; heavy."},
}
TINY = "# Tiny Note\n" + STEPS
TINY_LINE = "- `tiny-note` (`<tasks>/tiny-note.md`): a tiny note; rarely; light.\n"
FRONT = '---\ncommands: ["rm *"]\n---\n'


class Fake(FakeTeam):
    config_text = CONFIG

    def cfg(self):
        return read_config(path=self.config, local=self.local_file)

    def tasks_of(self, role, found=None):
        """
        `role`'s index in `found`, else in a fresh scan of the team root, with the temp config.
        """
        return index(
            role, found if found is not None else team(self.root), self.config, self.local_file
        )

    def indexed(self, found, local=None) -> dict[str, dict[str, str]]:
        local = self.local_file if local is None else local
        return Composer(config=read_config(path=self.config, local=local), team=found).index()

    def refused(self, call, *args, **kw) -> str:
        with self.assertRaises(ConfigError) as cm:
            call(*args, **kw)
        return str(cm.exception)

    def team_dir(self, files, name="team"):
        """Writes {rel: text} under ~/.ensemble/<name> and returns its real path."""
        top = os.path.realpath(os.path.join(self.ensemble, name))
        self.write(files, top)
        return top

    def with_dir(self, files, name="team"):
        """The Team of the team root and one team dir of `files`."""
        return team(self.root, (self.team_dir(files, name),))

    def path(self, rel, name="team"):
        return os.path.join(os.path.realpath(os.path.join(self.ensemble, name)), rel)


class Index(Fake):
    def test_the_tasks_section_lists_each_task_and_its_description_in_order(self):
        self.assertEqual(list(self.tasks_of("writer").items()), list(BUILT_IN["writer"].items()))
        self.assertEqual(list(self.tasks_of("editor")), ["long-note"])

    def test_no_tasks_section_or_an_empty_one(self):
        for text in (
            "# Writer\n\nWrite well.\n",
            "# Writer\n\n## Tasks\n\n"
            + INTRO.format("x")
            + "## Style\n\n- `short-note` (`<tasks>/short-note.md`): a.\n",
        ):
            with self.subTest(text=text):
                self.write({"roles/writer.md": text})
                with self.assertRaisesRegex(ConfigError, r"^roles/writer\.md: no task index"):
                    self.tasks_of("writer")

    def test_a_task_path_other_than_its_file(self):
        line = "- `long-note` (`<tasks>/long-note.md`): "
        for path, wrong in (
            ("`<tasks>/long.md`", "'<tasks>/long.md'"),
            ("`tasks/long-note.md`", "'tasks/long-note.md'"),
            ("`<tasks>/short-note.md`", "'<tasks>/short-note.md'"),
            (None, "''"),
        ):
            with self.subTest(path=path):
                new = "- `long-note`: " if path is None else f"- `long-note` ({path}): "
                self.write({"roles/writer.md": WRITER.replace(line, new)})
                self.assertEqual(
                    self.refused(self.tasks_of, "writer"),
                    f"roles/writer.md: 'long-note' has path {wrong}, not '<tasks>/long-note.md'",
                )

    def test_a_default_unlisted_with_a_wrong_path_or_missing(self):
        intro = INTRO.format("short-note")
        for new, msg in (
            (INTRO.format("essay"), "the default 'essay' is not a listed task"),
            (
                intro.replace("`<tasks>/short-note.md`", "`<tasks>/note.md`"),
                "'short-note' has path '<tasks>/note.md', not '<tasks>/short-note.md'",
            ),
            ("Pick the task below that fits the input.\n\n", "## Tasks opens without its default"),
            ("", "## Tasks opens without its default"),
        ):
            with self.subTest(new=new):
                self.write({"roles/writer.md": WRITER.replace(intro, new)})
                with self.assertRaisesRegex(
                    ConfigError, "^" + re.escape(f"roles/writer.md: {msg}")
                ):
                    self.tasks_of("writer")

    def test_a_listed_task_without_its_file(self):
        os.remove(os.path.join(self.root, "tasks", "long-note.md"))
        self.assertEqual(
            self.refused(self.tasks_of, "writer"),
            "roles/writer.md: lists 'long-note' without tasks/long-note.md",
        )

    def test_a_role_without_a_charter(self):
        self.assertEqual(self.refused(self.tasks_of, "nobody"), "unknown role 'nobody'")
        self.configure(CONFIG + "\n[roles.ghost]\ntier = 3\n")
        self.assertEqual(
            self.refused(self.tasks_of, "ghost"),
            "roles.ghost: no charter roles/ghost.md in core or team_dirs",
        )


class Catalog(Fake):
    def test_configured_roles_in_config_order_with_their_tasks(self):
        found = self.indexed(team(self.root))
        self.assertEqual(found, BUILT_IN)
        self.assertEqual(list(found), ["writer", "editor"])

    def test_roles_core_configured_then_core_sorted_then_each_team_dir_sorted(self):
        self.write({"roles/aide.md": "# Aide\n", "roles/assistant.md": "# Assistant\n"})
        a = self.team_dir({"roles/poet.md": POET, "roles/bard.md": POET}, "a")
        b = self.team_dir({"roles/zed.md": POET}, "b")
        self.assertEqual(
            roles(team(self.root, (b, a)), self.cfg()),
            ["writer", "editor", "aide", "assistant", "zed", "bard", "poet"],
        )

    def test_role_tables_rejects_a_missing_config_or_malformed_roles(self):
        for config, msg in (
            (
                'tier = 2\neffort = "high"\nroles = 1\n[output]\ntype = "local"\n',
                f"malformed [roles] table in {self.config}",
            ),
            (
                'tier = 2\neffort = "high"\n[output]\ntype = "local"\n[roles]\nwriter = 1\n',
                f"malformed [roles.writer] table in {self.config}",
            ),
        ):
            with self.subTest(config=config):
                self.configure(config)
                self.assertEqual(self.refused(role_tables, self.config, self.local_file), msg)
        os.remove(self.config)
        self.assertEqual(
            self.refused(role_tables, self.config, self.local_file), f"no core config {self.config}"
        )

    def test_the_defaults_are_the_real_team_root_and_config(self):
        self.assertEqual(team().root, TEAM)
        self.assertEqual(list(catalog()), ["researcher", "pm", "engineer", "verifier"])
        self.assertEqual(list(index("researcher")), ["deep-research", "research", "light-research"])


class ExplicitPaths(Fake):
    """Each function reads only the config and local passed, never the HOME one (malformed here)."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.ensemble, "config.local.toml"), "w") as f:
            f.write("malformed = [\n")

    def test_team_dirs(self):
        top = self.team_dir({"roles/poet.md": POET, "tasks/verse.md": VERSE})
        self.local(f'team_dirs = ["{top}"]\n')
        self.assertEqual(team_dirs(self.config, self.local_file), (top,))
        self.assertEqual(team_dirs(self.config, os.devnull), ())

    def test_role_tables_and_index(self):
        self.local("[roles.writer]\ntier = 3\n[roles.ghost]\ntier = 3\n")
        self.assertEqual(
            role_tables(self.config, self.local_file)["writer"], {"effort": "medium", "tier": 3}
        )
        self.assertEqual(role_tables(self.config, os.devnull)["writer"], {"effort": "medium"})
        ghost = "roles.ghost: no charter roles/ghost.md in core or team_dirs"
        found = team(self.root)
        self.assertEqual(self.refused(index, "ghost", found, self.config, self.local_file), ghost)
        self.assertEqual(
            self.refused(index, "ghost", found, self.config, os.devnull), "unknown role 'ghost'"
        )
        self.assertEqual(self.refused(self.indexed, found), ghost)
        self.assertEqual(self.indexed(found, os.devnull), BUILT_IN)


class TeamDirs(Fake):
    """Team dirs under the test's HOME, passed as a Team."""

    def test_team_dirs_values(self):
        self.assertEqual(team_dirs(self.config, self.local_file), ())
        top = self.team_dir({"roles/poet.md": POET})
        self.local('team_dirs = ["~/.ensemble/team"]\n')
        self.assertEqual(team_dirs(self.config, self.local_file), (top,))
        for value, msg in (
            ('"x"', "team_dirs: want a list of absolute or ~ paths, got 'x'"),
            ('["rel"]', "team_dirs: 'rel' is not an absolute or ~ path"),
            ("[1]", "team_dirs: 1 is not an absolute or ~ path"),
            ('["~nobody-here-x/t"]', "team_dirs: '~nobody-here-x/t' is not an absolute or ~ path"),
            ('["~/missing"]', "team_dirs: ~/missing is not a directory"),
            (f'["~/.ensemble/team", "{top}"]', f"team_dirs: {top} is listed twice"),
        ):
            with self.subTest(value=value):
                self.local(f"team_dirs = {value}\n")
                self.assertEqual(self.refused(team_dirs, self.config, self.local_file), msg)

    def test_team_dirs_in_the_core_config(self):
        self.configure('team_dirs = ["/x"]\n' + CONFIG)
        self.assertEqual(
            self.refused(team_dirs, self.config, self.local_file),
            f"team_dirs: set it in {self.local_file}, not config.toml",
        )
        self.configure(CONFIG)
        self.local("team_dirs = []\n")
        self.assertEqual(team_dirs(self.config, self.local_file), ())

    def test_no_team_dir_is_found_from_the_cwd(self):
        cwd = os.path.join(self.ensemble, "cwd")
        self.team_dir({"roles/poet.md": POET, "tasks/verse.md": VERSE}, "cwd/.ensemble/team")
        here = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, here)
        self.assertEqual(team_dirs(self.config, self.local_file), ())
        self.assertIsNone(
            lookup(team(self.root, team_dirs(self.config, self.local_file)), "roles", "poet")
        )
        self.assertEqual(self.refused(self.tasks_of, "poet"), "unknown role 'poet'")

    def test_a_new_role_and_the_index(self):
        found = self.with_dir({"roles/poet.md": POET, "tasks/verse.md": VERSE})
        self.assertEqual(self.tasks_of("poet", found), {"verse": "a verse; always; light."})
        self.assertEqual(charter(found, "poet"), (POET, self.path("roles/poet.md")))
        listed = self.indexed(found)
        self.assertEqual(listed, BUILT_IN | {"poet": {"verse": "a verse; always; light."}})
        self.assertEqual(list(listed), ["writer", "editor", "poet"])
        self.assertNotIn("poet", self.indexed(team(self.root)))

    def test_a_role_runs_only_tasks_from_its_own_dir(self):
        a = self.team_dir({"roles/poet.md": POET.replace("verse", "long-note")}, "a")
        b = self.team_dir({"roles/poet.md": POET}, "b")
        c = self.team_dir({"tasks/verse.md": VERSE, "tasks/tiny-note.md": TINY}, "c")
        for dirs, msg in (
            (
                (a,),
                (
                    f"{a}/roles/poet.md: lists 'long-note', whose file tasks/long-note.md is not in {a}/tasks: a "
                    "role runs only its own tasks"
                ),
            ),
            (
                (b, c),
                (
                    f"{b}/roles/poet.md: lists 'verse', whose file {c}/tasks/verse.md is not in {b}/tasks: a role "
                    "runs only its own tasks"
                ),
            ),
        ):
            with self.subTest(msg=msg):
                self.assertEqual(self.refused(self.tasks_of, "poet", team(self.root, dirs)), msg)
        self.write(
            {"roles/writer.md": WRITER.replace("\n\n## Style", "\n" + TINY_LINE + "\n## Style")},
        )
        self.assertEqual(
            self.refused(self.tasks_of, "writer", team(self.root, (c,))),
            f"roles/writer.md: lists 'tiny-note', whose file {c}/tasks/tiny-note.md is not in core "
            "team/tasks: a role runs only its own tasks",
        )
        self.write({"roles/writer.md": WRITER})
        found = self.with_dir(
            {"roles/writer.md": "## Tasks\n\n" + TINY_LINE, "tasks/tiny-note.md": TINY}, "ext"
        )
        self.assertEqual(
            self.refused(self.tasks_of, "writer", found),
            f"{self.path('roles/writer.md', 'ext')}: role 'writer' is also defined by "
            f"{self.root}/roles/writer.md; team dir files only add",
        )

    def test_tasks_dir_and_tasks_path(self):
        found = self.with_dir({"roles/poet.md": POET, "tasks/verse.md": VERSE})
        self.assertEqual(
            (tasks_dir(found, "poet"), tasks_dir(found, "writer"), tasks_dir(found, "nobody")),
            (self.path("tasks"), None, None),
        )
        self.assertEqual(
            (tasks_path("poet", found), tasks_path("writer", found)),
            (self.path("tasks"), os.path.join(self.root, "tasks")),
        )
        self.assertEqual(tasks_path("researcher", team()), os.path.join(TEAM, "tasks"))

    def test_a_name_defined_twice_names_both_files(self):
        for files, kind, name in (
            ({"roles/editor.md": "# Editor\n"}, "roles", "editor"),
            ({"tasks/long-note.md": VERSE}, "tasks", "long-note"),
            ({"templates/note.md": "# N\n"}, "templates", "note"),
        ):
            with self.subTest(kind=kind):
                found = self.with_dir(files, kind)
                rel = next(iter(files))
                self.assertEqual(
                    self.refused(lookup, found, kind, name),
                    f"{self.path(rel, kind)}: {kind[:-1]} {name!r} is also defined by "
                    f"{self.root}/{rel}; team dir files only add",
                )
                self.assertIn("is also defined by", self.refused(check_all, found, self.cfg()))
        a = self.team_dir({"roles/poet.md": POET, "tasks/verse.md": VERSE}, "a")
        b = self.team_dir({"roles/poet.md": POET}, "b")
        self.assertEqual(
            self.refused(self.tasks_of, "poet", team(self.root, (a, b))),
            (
                f"{b}/roles/poet.md: role 'poet' is also defined by {a}/roles/poet.md; team dir files only "
                "add"
            ),
        )

    def test_a_misnamed_file_stops_only_check_all_and_a_lookup_naming_it(self):
        found = self.with_dir(
            {
                "roles/poet.md": POET,
                "tasks/verse.md": VERSE,
                "tasks/Big_Note.md": TINY,
                "templates/Card.md": "# Card\n",
            }
        )
        self.tasks_of("writer", found)
        self.tasks_of("poet", found)
        self.assertEqual(
            self.refused(check_all, found, self.cfg()),
            f"{self.path('tasks/Big_Note.md')}: name 'Big_Note': want [a-z0-9-]+",
        )
        self.assertEqual(
            self.refused(read, found, "templates", "Card"),
            f"{self.path('templates/Card.md')}: name 'Card': want [a-z0-9-]+",
        )
        found = self.with_dir({"roles/Poet.md": POET, "tasks/Verse.md": VERSE}, "caps")
        self.assertEqual(
            self.refused(self.tasks_of, "Poet", found),
            f"{self.path('roles/Poet.md', 'caps')}: name 'Poet': want [a-z0-9-]+",
        )
        self.assertEqual(
            self.refused(read, found, "tasks", "Verse"),
            f"{self.path('tasks/Verse.md', 'caps')}: name 'Verse': want [a-z0-9-]+",
        )
        self.assertEqual(self.refused(self.tasks_of, "Nobody", found), "unknown role 'Nobody'")

    def test_a_defect_in_another_roles_file_leaves_this_role_loading(self):
        found = self.with_dir(
            {
                "roles/poet.md": FRONT + POET,
                "tasks/verse.md": "no title\n",
                "roles/editor.md": "# E\n",
                "templates/card.md": "{{x}}\n",
            }
        )
        self.assertEqual(self.tasks_of("writer", found), BUILT_IN["writer"])
        self.refused(check_all, found, self.cfg())

    def test_roles_without_a_charter(self):
        self.local("[roles.ghost]\ntier = 3\n")
        found = self.with_dir({"roles/poet.md": POET, "tasks/verse.md": VERSE})
        self.tasks_of("editor", found)
        msg = "roles.ghost: no charter roles/ghost.md in core or team_dirs"
        self.assertEqual(self.refused(self.tasks_of, "ghost", found), msg)
        self.assertEqual(self.refused(roles, found, self.cfg()), msg)
        self.local("")
        self.assertEqual(roles(found, self.cfg()), ["writer", "editor", "poet"])

    def test_front_matter_in_any_team_dir_file(self):
        for files, rel, call in (
            (
                {"roles/poet.md": POET, "tasks/verse.md": FRONT + VERSE},
                "tasks/verse.md",
                lambda found: self.tasks_of("poet", found),
            ),
            (
                {"roles/poet.md": FRONT + POET, "tasks/verse.md": VERSE},
                "roles/poet.md",
                lambda found: self.tasks_of("poet", found),
            ),
            (
                {"templates/card.md": FRONT + "# Card\n"},
                "templates/card.md",
                lambda found: read(found, "templates", "card"),
            ),
        ):
            with self.subTest(rel=rel):
                name = rel.replace("/", "-")
                self.assertEqual(
                    self.refused(call, self.with_dir(files, name)),
                    (
                        f"{self.path(rel, name)}: front matter (---) is not read: team dir files hold text "
                        "only; run keys go in ~/.ensemble/config.local.toml"
                    ),
                )

    def test_each_defect_of_a_team_dir_file_names_it(self):
        task, charter = self.path("tasks/verse.md"), self.path("roles/poet.md")
        for verse, poet, msg in (
            ("Verse\n" + STEPS, POET, f"{task}: must start with a '# ' heading"),
            (VERSE, POET.replace("# Poet", "Poet"), f"{charter}: must start with a '# ' heading"),
            (VERSE + "{{x}}\n", POET, f"{task}: unfilled placeholder {{{{x}}}}"),
            (VERSE, POET + "{{role}}\n", f"{charter}: unfilled placeholder {{{{role}}}}"),
            (
                VERSE.replace("[ensemble-progress:start] the topic.", "the topic."),
                POET,
                f"{task}: has 0 [ensemble-progress:start] lines, not 1",
            ),
            (
                VERSE + "\n[ensemble-progress:start] again.\n",
                POET,
                f"{task}: has 2 [ensemble-progress:start] lines, not 1",
            ),
            (VERSE.replace("## Resume", "## Later"), POET, f"{task}: has no ## Resume section"),
            (
                VERSE.replace("## Resume", "```\n## Resume\n```"),
                POET,
                f"{task}: has no ## Resume section",
            ),
            (
                VERSE + "\nUse `<team>/methods/nope.md`.\n",
                POET,
                f"{task}: names <team>/methods/nope.md, which core has no file for",
            ),
        ):
            with self.subTest(msg=msg):
                found = self.with_dir({"roles/poet.md": poet, "tasks/verse.md": verse})
                self.assertEqual(self.refused(self.tasks_of, "poet", found), msg)
        found = self.with_dir(
            {"roles/poet.md": POET, "tasks/verse.md": VERSE + "\nUse `<team>/methods/deep.md`.\n"}
        )
        self.assertEqual(self.tasks_of("poet", found), {"verse": "a verse; always; light."})

    def test_without_a_team_dir_for_this_role_nothing_changes(self):
        other = self.with_dir(
            {"roles/poet.md": POET, "tasks/verse.md": VERSE, "templates/card.md": "# Card\n"}
        )
        self.assertEqual(team(self.root, ()), team(self.root))
        for role in ("writer", "editor"):
            with self.subTest(role=role):
                self.assertEqual(self.tasks_of(role, other), self.tasks_of(role))
        self.assertEqual(used(team(self.root), "writer", ["note"]), [])
        self.assertEqual(used(other, "writer", ["note"]), [])
        self.assertEqual(
            used(other, "poet", ["note", "card"]),
            [
                self.path("roles/poet.md"),
                self.path("tasks/verse.md"),
                self.path("templates/card.md"),
            ],
        )

    def test_labels_and_sources(self):
        found = self.with_dir({"roles/poet.md": POET})
        builtin = os.path.join(self.root, "roles", "writer.md")
        self.assertEqual(
            (found.label("roles", "writer", builtin), found.source(builtin), found.custom(builtin)),
            ("roles/writer.md", "built-in", False),
        )
        poet = self.path("roles/poet.md")
        self.assertEqual(
            (found.label("roles", "poet", poet), found.source(poet), found.custom(poet)),
            (poet, poet, True),
        )
        self.assertEqual(
            read(found, "tasks", "long-note"),
            ("# Long Note\n\nWrite a long note.\n", "tasks/long-note.md"),
        )
        self.assertEqual(self.refused(read, found, "tasks", "nope"), "missing file tasks/nope.md")


class TeamDirChecks(Fake):
    """check_team_dirs, team_paths and check_overlap on a team dir under the test's HOME."""

    def setUp(self):
        super().setUp()
        self.top = self.team_dir(
            {"roles/poet.md": POET, "tasks/verse.md": VERSE, "templates/card.md": "# Card\n"}
        )

    def checked(self, writes=()):
        return self.refused(check_team_dirs, [self.top], writes=list(writes))

    def test_team_paths_are_the_dir_its_kind_subdirs_and_their_md_files(self):
        self.write({"tasks/notes.txt": "x", "methods/m.md": "# M\n"}, self.top)
        self.assertEqual(
            team_paths(self.top),
            [
                self.top,
                self.path("roles"),
                self.path("roles/poet.md"),
                self.path("tasks"),
                self.path("tasks/verse.md"),
                self.path("templates"),
                self.path("templates/card.md"),
            ],
        )
        with mock.patch.object(os, "listdir", side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(
                self.refused(team_paths, self.top), f"{self.path('roles')}: Permission denied"
            )

    def test_no_team_dir_or_a_clean_one_passes(self):
        self.assertIsNone(check_team_dirs((), writes=[("the workdir", "/")]))
        self.assertIsNone(check_team_dirs([self.top], writes=[("the workdir", self.tmp)]))

    def test_a_team_dir_overlapping_a_dir_the_run_can_write(self):
        inside, home = os.path.join(self.top, "w"), os.path.realpath(self.ensemble)
        for writes, want in (
            ([("the workdir", inside)], f"{self.top} overlaps the workdir {inside}"),
            ([("the workdir", self.ensemble)], f"{self.top} overlaps the workdir {home}"),
            (
                [("the cwd", self.tmp), (f"write dir {inside}", inside)],
                f"{self.top} overlaps write dir {inside} {inside}",
            ),
            (
                [("a dir the client adds", home)],
                f"{self.top} overlaps a dir the client adds {home}",
            ),
        ):
            with self.subTest(want=want):
                self.assertEqual(
                    self.checked(writes), f"team_dirs: {want}: the agent run can write there"
                )

    def test_check_overlap_either_way_by_real_path(self):
        parent, sibling = os.path.dirname(self.top), self.top + "-other"
        self.assertIsNone(
            check_overlap("x", self.top, [("the cwd", sibling), ("the workdir", self.tmp)])
        )
        for p in (parent, os.path.join(self.top, "a", "b"), self.top):
            with self.subTest(p=p):
                self.assertEqual(
                    self.refused(check_overlap, "x", self.top, [("the cwd", p)]),
                    f"team_dirs: x overlaps the cwd {p}: the agent run can write there",
                )

    def test_a_symlink_is_checked_by_its_real_path(self):
        elsewhere = os.path.realpath(os.path.join(self.tmp, "elsewhere"))
        os.makedirs(elsewhere)
        link = self.path("tasks/verse.md")
        os.rename(link, os.path.join(elsewhere, "verse.md"))
        os.symlink(os.path.join(elsewhere, "verse.md"), link)
        check_team_dirs([self.top], writes=[])
        work = os.path.realpath(os.path.join(self.tmp, "work"))
        tasks, moved = self.path("tasks"), os.path.join(work, "team-tasks")
        os.makedirs(work)
        os.rename(tasks, moved)
        os.symlink(moved, tasks)
        self.assertEqual(
            self.checked([("the workdir", work)]),
            (
                f"team_dirs: {tasks} ({moved}) overlaps the workdir {work}: the agent run can write there"
            ),
        )

    def test_its_tools_tree_is_checked_by_real_path(self):
        work = os.path.realpath(os.path.join(self.tmp, "work"))
        os.makedirs(os.path.join(work, "bin"))
        self.write({"bin/t.py": ""}, work)
        self.write({"tools/own.py": "", "tools/sub/x.py": ""}, self.top)
        link = self.path("tools/sub/t.py")
        os.symlink(os.path.join(work, "bin", "t.py"), link)
        os.symlink(self.path("tools"), self.path("tools/sub/loop"))
        check_team_dirs([self.top], writes=[])
        shown = f"{link} ({work}/bin/t.py)"
        self.assertEqual(
            self.checked([("the workdir", work)]),
            f"team_dirs: {shown} overlaps the workdir {work}: the agent run can write there",
        )


class Kinds(Case):
    def test_team_kind_is_a_str_enum_in_the_team_dir_order(self):
        self.assertEqual(list(TeamKind), ["roles", "tasks", "templates"])
