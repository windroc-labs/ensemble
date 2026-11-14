"""skills/manage/SKILL.md (/ensemble:manage): the commander runs only ensemble-crew commands and
pre-approves only crew reads; it launches all three kinds through crew start, handles every
event word, answers dialogs with keys and recovers one entry at a time, through no retired path.
"""

import os
import re
import tomllib

from tests.manager.case import REPO, Case
from tests.manager.skills.commands import (
    BIN,
    SKILLS,
    body,
    examples,
    flags,
    paths,
    rules,
    section,
    sections,
    spans,
    subcommand,
    written,
)
from tests.manager.skills.manage import rule

RETIRED = (
    "crew.py",
    "drive.py",
    "tmux_host.py",
    "router.py",
    "--runner",
    "attach --resume",
    "${CLAUDE_SKILL_DIR}/../../src/ensemble/manager",
    "src/ensemble/manager",
    "tmux ls",
    "ensemble-<role>-<ID>",
)
REPAIRS = ("rename-session", "/rename", "@grid-busy", "@opener", "@pane", "send-keys", "set-option")


class Commands(Case):
    def test_nested_issue_command_selects_its_leaf_help(self):
        self.assertEqual(
            subcommand(["ensemble-orchestrator", "issue", "read", "<ID>", "--full"]),
            ("issue", "read"),
        )
        self.assertEqual(subcommand(["ensemble-session", "send", "worker", "text"]), ("send",))
        self.assertEqual(subcommand(["ensemble-orchestrator", "--help"]), ())
        self.assertEqual(
            subcommand(["ensemble-crew", "start", "worker", "<name>"]), ("start", "worker")
        )
        self.assertEqual(subcommand(["ensemble-crew", "start", "--list"]), ("start",))

    def test_the_one_worker_launch_is_generic(self):
        [launch] = [
            " ".join(c)
            for c in written(body("manage"))
            if c[:3] == ["ensemble-crew", "start", "worker"] and "--client" in c
        ]
        self.assertIn("--client <client>", launch)
        self.assertIn("[--prompt '<text>']", launch)
        self.assertNotIn("--cwd <work dir>/agent", body("manage"))

    def test_the_commander_runs_only_crew_commands(self):
        forwarded = rule("**Issue updates**")
        own = body("manage").replace(forwarded, "")
        commands = [c for c in written(own) if len(c) > 1]
        self.assertGreater(len(commands), 20)
        for command in commands:
            self.assertEqual(command[0], "ensemble-crew", command)
        for name in ("ensemble-delegate", "transcript.py", "ensemble-orchestrator", "hold --force"):
            self.assertNotIn(name, own)
        for name in ("ensemble-session", "ensemble-orchestrator"):
            self.assertFalse([c for c in written(own) if c[0] == name and len(c) > 1], name)
        [run] = [c for c in written(forwarded) if len(c) > 1]
        self.assertEqual(run[:3], ["ensemble-orchestrator", "issue", "read"])
        self.assertIn("tell its run to use", forwarded)
        for command in examples(own):
            self.assertIn(command[0], sorted(os.listdir(BIN)), command)

    def test_pre_approved_rules_are_crew_reads_and_the_role_listing(self):
        for rule_ in rules("manage"):
            self.assertTrue(rule_.startswith("ensemble-crew "), rule_)
        self.assertIn("ensemble-crew read *", rules("manage"))
        self.assertIn("ensemble-crew start --list", rules("manage"))

    def test_pre_approved_rules_are_read_only(self):
        for rule_ in rules("manage"):
            if rule_ == "ensemble-crew start --list":
                continue
            self.assertNotRegex(
                rule_, r"\b(run|start|send|stop|forget|close|resume|restart|tile|hold)\b"
            )

    def test_an_issue_launches_only_through_crew_start_issue(self):
        launches = [
            c for c in written(body("manage")) if c[:2] == ["ensemble-crew", "start"] and len(c) > 2
        ]
        issues = {c[2] for c in launches if "<ID>" in c}
        self.assertEqual(issues, {"issue", "reopen"})
        self.assertIn("`ensemble-crew start issue <ID>`", rule("**Board runs**"))
        self.assertNotIn("An issue for Codex", body("manage"))
        self.assertNotIn("prompt.md", body("manage"))

    def test_delivery_is_crews_report_with_no_client_reader(self):
        send = rule("Before sending text to a worker")
        self.assertIn("`ensemble-crew read <name>`", send)
        self.assertIn("`ensemble-crew send <name>`", send)
        self.assertIn("never the screen", send)
        self.assertNotIn("transcript.py", body("manage"))
        self.assertNotIn("An OpenCode worker has no jsonl transcript", body("manage"))

    def test_a_client_without_bin_on_its_path_reaches_this_plugins_bin(self):
        fallback = "${CLAUDE_SKILL_DIR}/../../bin/<command>"
        self.assertIn(fallback, body("manage"))
        own = os.path.join(SKILLS, "manage", "../../bin")
        self.assertEqual(os.path.realpath(own), os.path.realpath(BIN))

    def test_crew_launches_take_no_session_id(self):
        for commands in spans(body("manage")):
            outer = commands[0][:2] == ["ensemble-crew", "start"] and len(commands[0]) > 2
            for command in commands[1:] if outer else commands:
                if outer:
                    self.assertFalse({"--sid", "--resume"} & set(flags(command)), command)

    def test_content_uses_crew_without_unused_layer_permissions(self):
        for command in ("ensemble-session", "ensemble-delegate", "ensemble-orchestrator"):
            self.assertNotIn(f"{command} content *", rules("manage"))
        self.assertIn("owning ctl", section("manage", "Events"))

    def test_has_no_retired_launch_or_resume_forms(self):
        for retired in (
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
        self.assertNotIn("## Crew commands", body("manage"))


class Names(Case):
    def test_the_files_it_names_exist_and_ensemble_runs(self):
        named = paths("manage")
        self.assertIn(os.path.join(SKILLS, "manage", "scripts", "ensemble"), named)
        for path in named:
            self.assertTrue(os.path.isfile(path), path)
        self.assertTrue(os.access(os.path.join(SKILLS, "manage", "scripts", "ensemble"), os.X_OK))

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
            "refuse inside them (exit 2: each session's client gets `ENSEMBLE_AGENT_SESSION=1`)",
            body("manage"),
        )
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
            "ensemble-crew start worker",
            "ensemble-crew start role",
            "ensemble-crew start issue",
            "ensemble-crew start --list",
        ):
            self.assertIn(command, launch)
        self.assertNotIn("ensemble-crew start command", launch)
        self.assertIn("before", launch)
        self.assertIn("exit 2", launch)
        self.assertIn("--mode headless", launch)
        self.assertIn("the tui `<name>`; its harness `<name>-drive` is a local process", launch)
        self.assertIn("--display D", launch)
        self.assertIn("`--display none`", launch)
        self.assertIn("ensemble-crew start --help", launch)

    def test_start_attaches_and_points_to_the_monitoring_contract(self):
        start = section("manage", "Start")
        for phrase in (
            "`ensemble <name>`",
            "`ensemble <manager> -- <client argv>`",
            "`ensemble --help`: taking over a hung holder",
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
            "`ensemble-crew send <name> '<text>'`",
            "`ensemble-crew send <name> --key C-x C-s`",
            "`ensemble-crew read <name> [--lines <n>]`",
            "`ensemble-crew read <name> --reply`",
            "`ensemble-crew restart <name>`",
            "`ensemble-crew hide <name>…`",
            "`ensemble-crew show <name>…`",
            "`--all`",
            "`ensemble-crew note <name> '<text>'`",
            "`''` clears",
            "`ensemble-crew status [<name>…]`",
            "`ensemble-crew report`",
            "PR's URL",
            "`ensemble-crew close <name>`",
            "`ensemble-crew close --all`",
            "`ensemble-crew close <name> --forget`",
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
        self.assertIn("`ensemble-crew read <name>`", blocked)
        self.assertIn("`ensemble-crew send <name> --key K…`", blocked)
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
        for word in ("workers_per_column", "grid_retile", "`ensemble-crew show --tile`"):
            self.assertIn(word, grid)

    def test_sections(self):
        self.assertEqual(
            sections("manage")[:6], ["Start", "Events", "Launch", "Grid", "Direct", "Recover"]
        )
