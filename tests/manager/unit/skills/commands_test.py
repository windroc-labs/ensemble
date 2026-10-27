"""skills/manage/SKILL.md (/ensemble:manage): every layer command it writes is one of the four
bin/ commands; it launches all three kinds through Crew, handles every event word,
answers dialogs with keys and recovers one entry at a time, through no retired path.
"""

import os
import re
import tomllib

from tests.manager.case import REPO, Case
from tests.manager.skills.commands import (
    BIN,
    SKILLS,
    body,
    example,
    examples,
    flags,
    paths,
    rules,
    section,
    sections,
    spans,
)

RETIRED = (
    "crew.py",
    "drive.py",
    "tmux_host.py",
    "router.py",
    "--runner",
    "attach --resume",
    "${CLAUDE_SKILL_DIR}/../../src/ensemble/manager",
    "src/ensemble/manager",
)
REPAIRS = ("rename-session", "/rename", "@grid-busy", "@opener", "@pane", "send-keys", "set-option")


class Commands(Case):
    def test_worker_examples_select_generic_or_codex_launch(self):
        generic = example("manage", "ensemble-crew worker", "--client <client>")
        codex = example("manage", "ensemble-crew worker", "--client codex")
        self.assertIn("[--prompt '<text>']", generic)
        self.assertIn("--cwd <work dir>/agent", codex)

    def test_every_example_is_one_of_the_four_bin_commands(self):
        commands = examples(body("manage"))
        self.assertGreater(len(commands), 20)
        for command in commands:
            self.assertIn(command[0], sorted(os.listdir(BIN)), command)

    def test_pre_approved_rules_are_read_only(self):
        for rule in rules("manage"):
            self.assertNotRegex(
                rule, r"\b(run(?! --list)|start|send|stop|forget|close|resume|restart|tile)\b"
            )

    def test_a_client_without_bin_on_its_path_reaches_this_plugins_bin(self):
        fallback = "${CLAUDE_SKILL_DIR}/../../bin/<command>"
        self.assertIn(fallback, body("manage"))
        own = os.path.join(SKILLS, "manage", "../../bin")
        self.assertEqual(os.path.realpath(own), os.path.realpath(BIN))

    def test_crew_launches_take_no_session_id(self):
        for commands in spans(body("manage")):
            outer = (
                len(commands[0]) > 1
                and commands[0][0] == "ensemble-crew"
                and commands[0][1] in {"worker", "issue", "role"}
            )
            for command in commands[1:] if outer else commands:
                if outer:
                    self.assertFalse({"--sid", "--resume"} & set(flags(command)), command)

    def test_content_uses_crew_without_unused_layer_permissions(self):
        for command in ("ensemble-session", "ensemble-delegate", "ensemble-orchestrator"):
            self.assertNotIn(f"{command} content *", rules("manage"))
        self.assertIn("owning ctl", section("manage", "Events"))

    def test_fr33_has_no_retired_launch_or_resume_forms(self):
        for retired in (
            "ensemble-crew run <name> -- ensemble-session start",
            "--dry-run",
            "--resume --sid",
        ):
            self.assertNotIn(retired, body("manage"))

    def test_only_manage_owns_commands_permissions_and_scripts(self):
        self.assertTrue(rules("manage"))

    def test_role_launch_has_no_separate_dry_run(self):
        [role] = [
            line
            for line in section("manage", "Launch").splitlines()
            if line.startswith("- **Role run**:")
        ]
        self.assertNotIn("--dry-run", role)

    def test_no_retired_path_flag_or_manual_repair(self):
        for word in (*RETIRED, *REPAIRS):
            self.assertNotIn(word, body("manage"))
        self.assertNotIn("--split", body("manage"))


class Names(Case):
    def test_the_files_it_names_exist_and_ctmux_runs(self):
        named = paths("manage")
        self.assertIn(os.path.join(SKILLS, "manage", "scripts", "ctmux"), named)
        for path in named:
            self.assertTrue(os.path.isfile(path), path)
        self.assertTrue(os.access(os.path.join(SKILLS, "manage", "scripts", "ctmux"), os.X_OK))

    def test_the_grid_keys_it_names_are_the_viewers(self):
        [config] = {p for p in paths("manage") if p.endswith("config.toml")}
        with open(config, "rb") as f:
            viewer = tomllib.load(f)["viewer"]
        for key in ("workers_per_column", "grid_retile"):
            self.assertIn(key, viewer)
            self.assertIn(f"`{key}`", body("manage"))

    def test_only_the_commander_has_it_as_the_session_config_turns_it_off(self):
        self.assertIn("so a worker or role run starts no sessions of its own", body("manage"))
        self.assertIn(
            "refuse inside them (`docs/architecture.md` › Session settings)", body("manage")
        )
        with open(os.path.join(REPO, "docs", "architecture.md"), encoding="utf-8") as f:
            self.assertIn("`ENSEMBLE_AGENT_SESSION=1`", f.read())
        [config] = {p for p in paths("manage") if p.endswith("config.toml")}
        with open(config, "rb") as f:
            clients = tomllib.load(f)["clients"]
        for client in ("claude", "codex"):
            self.assertTrue(
                any("ensemble@windroc" in flag for flag in clients[client]["flags"]), client
            )

    def test_the_skills_it_names_are_this_plugins(self):
        for skill in re.findall(r"`/ensemble:([a-z-]+)`", body("manage")):
            self.assertTrue(os.path.isfile(os.path.join(REPO, "skills", skill, "SKILL.md")), skill)


class Flows(Case):
    def test_each_kind_launches_through_its_crew_shorthand(self):
        launch = section("manage", "Launch")
        for command in (
            "ensemble-crew worker",
            "ensemble-crew role",
            "ensemble-crew issue",
        ):
            self.assertIn(command, launch)
        self.assertNotIn("ensemble-crew run", launch)
        self.assertIn("before", launch)
        self.assertIn("exit 2", launch)
        self.assertIn("--mode headless", launch)
        self.assertIn("the tui `<name>`; its harness `<name>-drive` is a local process", launch)
        self.assertIn("--display D", launch)
        self.assertIn("`--display none`", launch)
        self.assertIn("ensemble-session --help", launch)

    def test_start_attaches_and_points_to_the_monitoring_contract(self):
        start = section("manage", "Start")
        for phrase in (
            "`ctmux <name>`",
            "ensemble-crew hold --manager",
            "`ensemble-crew attach`",
            "lease",
            "`name kind sid state view note cwd attach`",
            "backlog",
            "`resume: ensemble-crew resume … <name>`",
            "`next-event:`",
            "`monitor expired:`",
        ):
            self.assertIn(phrase, start)
        self.assertIn("Monitoring mode", start)
        self.assertIn(
            "qualifies only when its runtime contract promises a notification to this commander",
            start,
        )
        self.assertNotIn("marks each printed event handled", body("manage"))

    def test_direct_names_every_daily_operation(self):
        direct = section("manage", "Direct")
        for phrase in (
            "`ensemble-session send <name> '<text>'`",
            "`ensemble-session read <name> [--lines <n>]`",
            "`ensemble-session reply <name>`",
            "`ensemble-session restart <name>`",
            "`ensemble-crew hide <name>…`",
            "`ensemble-crew show <name>…`",
            "`--all`",
            "`ensemble-crew note <name> '<text>'`",
            "`''` clears",
            "`ensemble-crew status [<name>…]`",
            "`ensemble-crew report`",
            "PR's URL",
            "`ensemble-crew close <name>`",
            "`ensemble-crew close-all`",
            "`ensemble-crew forget <name>`",
            "`--force`",
            "`ensemble-crew release`",
        ):
            self.assertIn(phrase, direct)
        for phrase in (
            "ensemble-delegate hide",
            "ensemble-orchestrator hide",
            "ensemble-session hide",
        ):
            self.assertNotIn(phrase, direct)

    def test_color_is_left_to_the_session_but_a_start_dialog(self):
        (worker,) = [
            line for line in section("manage", "Launch").splitlines() if "**Worker**" in line
        ]
        self.assertEqual(body("manage").count("/color"), 1)
        self.assertIn("/color", worker)
        self.assertIn('"color not set"', worker)
        self.assertNotIn("Color", section("manage", "Direct"))

    def test_events_handles_every_word(self):
        events = section("manage", "Events")
        for word in ("done", "blocked", "dead", "exited", "outcome"):
            self.assertRegex(events, rf"\*\*{word}\b[^*]*\*\*", word)

    def test_an_events_content_is_its_read_and_only_numbered_lines_are_events(self):
        events = section("manage", "Events")
        self.assertNotIn("confirm each by reading", events)
        for phrase in (
            "content",
            "`content unavailable",
            "untrusted",
            "greater than the previous event's",
        ):
            self.assertIn(phrase, events)
        self.assertIn("ensemble-crew report", rules("manage"))
        self.assertIn("ensemble-crew report *", rules("manage"))

    def test_a_dialog_is_read_then_answered_with_keys_never_with_text(self):
        events = section("manage", "Events")
        blocked = re.search(r"\*\*blocked\*\*(.*?)(?=^- \*\*)", events, re.M | re.S)[1]
        self.assertIn("ensemble-session read <name>", blocked)
        for keys in ("--key Down Enter", "--key 2", "--key Escape", "--key C-c"):
            self.assertIn(keys, blocked)
        self.assertRegex(blocked, r"send no text")
        self.assertRegex(blocked, r"read (it )?again")
        self.assertRegex(blocked, r"not by the last event")
        self.assertIn("ask the user", blocked)

    def test_recovery_is_per_entry_and_a_waiting_role_uses_recorded_run_settings(self):
        recover = section("manage", "Recover")
        self.assertIn("ensemble-crew resume <name>", recover)
        self.assertIn("ensemble-crew resume <name> --input '<answers>'", recover)
        self.assertNotIn("ensemble-delegate resume", recover)
        self.assertNotIn("--resume --sid", recover)
        self.assertRegex(recover, r"no resume-all")

    def test_grid_names_its_keys_and_the_public_retile(self):
        grid = section("manage", "Grid")
        for word in ("workers_per_column", "grid_retile", "ensemble-session tile <window>"):
            self.assertIn(word, grid)

    def test_sections(self):
        self.assertEqual(
            sections("manage")[:6], ["Start", "Events", "Launch", "Grid", "Direct", "Recover"]
        )
