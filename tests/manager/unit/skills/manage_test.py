"""skills/manage/SKILL.md: command references, closing permissions, Codex issues,
decision markers, board proposals and the one commit exception agree.
"""

import os
import re

from tests.manager.case import REPO, Case
from tests.manager.skills.commands import (
    BIN,
    body,
    doc_sections,
    examples,
    paths,
    references,
    rules,
    section,
    sections,
)
from tests.manager.skills.manage import rule

RETIRED = (
    "crew.py",
    "drive.py",
    "tmux_host.py",
    "router.py",
    "--runner",
    "attach --resume",
    "ctmux's",
    "${CLAUDE_SKILL_DIR}/../../src/ensemble/manager",
    "tmux ls",
    "ensemble-<role>-<ID>",
)


class Manage(Case):
    def test_tmux_skill_stays_retired(self):
        self.assertFalse(os.path.isfile(os.path.join(REPO, "skills", "tmux", "SKILL.md")))

    def test_each_cited_command_section_exists(self):
        cited = references("manage")
        self.assertGreaterEqual(set(cited), {"Start", "Events", "Launch", "Direct", "Recover"})
        for name in cited:
            self.assertIn(name, sections("manage"))

    def test_commands_are_bin_commands_and_no_retired_name(self):
        for command in examples(body("manage")):
            self.assertIn(command[0], sorted(os.listdir(BIN)), command)
        for word in RETIRED:
            self.assertNotIn(word, body("manage"))
        self.assertNotIn("## Crew commands", body("manage"))

    def test_stale_holder_recovery_restarts_under_hold_without_polling(self):
        lease_free = "env -u ENSEMBLE_HOLDER_ID -u ENSEMBLE_HOLDER_GENERATION"
        [item] = [
            line
            for line in section("manage", "Recover").splitlines()
            if line.startswith("- **Stale holder**")
        ]
        for phrase in (
            "`stale holder generation`",
            "`holder no longer alive`",
            "ack --through",
            f"`{lease_free}`",
            "those `attach` prints",
            "`ensemble-crew hold --manager <manager> --session <manager> -- claude --continue`",
            "`codex resume --last`",
            "never run `hold --force` yourself",
            "Manual",
            "polling loop",
        ):
            self.assertIn(phrase, item)
        self.assertIn("[Recover](#recover)", section("manage", "Start"))
        events = section("manage", "Events")
        self.assertIn("Unacknowledged events replay at every recovery", events)
        self.assertIn("`stale holder generation`", events)
        self.assertFalse([rule for rule in rules("manage") if rule.startswith("env ")])

    def test_a_mid_turn_answer_is_a_reply_event(self):
        direct = section("manage", "Direct")
        self.assertNotIn("send after `done`", direct)
        self.assertIn("A Codex or Claude worker takes text sent mid-turn", direct)
        events = section("manage", "Events")
        self.assertIn("- **reply `<id>`**: a Codex or Claude session answered", events)
        self.assertIn("except `reply`", events)

    def test_events_arrive_with_their_content_and_need_no_pane_read(self):
        monitor = section("manage", "Start")
        self.assertNotIn("confirm by reading the pane", monitor)
        self.assertIn("event line, then its content", monitor)

    def test_monitoring_has_one_authoritative_contract(self):
        monitor = section("manage", "Start") + section("manage", "Events")
        self.assertNotIn("skills/tmux/SKILL.md", body("manage"))
        for phrase in (
            "every active turn",
            "Shell `&`",
            "automatically delivers output or completion notifications",
            "Manual monitoring; no idle notifications",
            "pending --content",
            "ack --through <line> --gen <generation>",
            "reconcile its durable result",
            "Rotation retains the unhandled suffix",
        ):
            self.assertIn(phrase, monitor)
        for retired in ("--receipt", "--notify", "reader.lock", "ready: true", "version 3"):
            self.assertNotIn(retired, body("manage"))
        self.assertNotIn("marks each printed event handled", body("manage"))

    def test_a_roster_entry_closes_through_crew(self):
        close = rule("AppleScript")
        self.assertIn("Close through Crew (Direct)", close)
        self.assertRegex(close, r"tmux only for sessions outside the Roster")

    def test_a_codex_issue_worker_starts_by_name_with_its_prompt_and_closes_on_merge(self):
        codex = rule("An issue for Codex")
        self.assertRegex(
            codex,
            r"`ensemble-crew worker <name> --client codex "
            r"--cwd <work dir>/agent --prompt 'Read <work dir>/agent/prompt\.md[^`]+'",
        )
        self.assertRegex(codex, r"[Cc]lose it .*PR merges")

    def test_one_decision_marker_and_one_commit_exception(self):
        self.assertIn("`(the user, <YYYY-MM-DD>)`", rule("Record each decision"))
        self.assertNotIn("<the user's name>", body("manage"))
        self.assertRegex(rule("Clear small"), r"commit only, no push")
        self.assertIn(".gitignore", rule("Commit or push only when asked"))

    def test_issue_operations_go_through_the_fixed_commands_skill(self):
        board = section("manage", "The task board")
        issues = rule("**Issue operations**")
        self.assertIn(issues, board)
        for literal in (
            "`/ensemble:board`",
            "`ensemble-orchestrator issue`",
            "never a hand-written query",
        ):
            self.assertIn(literal, issues)

    def test_issue_work_goes_to_the_board_by_id_and_the_router_picks_its_model(self):
        runs = rule("**Board runs**")
        self.assertIn(runs, section("manage", "The task board"))
        for literal in (
            "by its ID",
            "`ensemble-crew issue <ID>`",
            "moving the issue to Todo",
            "the router picks its model, tier and effort",
            "never read an issue to choose a model, tier or effort",
        ):
            self.assertIn(literal, runs, literal)
        for line in body("manage").splitlines():
            if re.search(r"\b(tier|effort)\b", line):
                self.assertEqual(runs, line)

    def test_approved_board_proposals_run_after_a_state_reread(self):
        board = section("manage", "The task board")
        proposals = rule("**Board proposals**")
        self.assertIn(proposals, board)
        for literal in (
            "lines `<candidate id>: <action>: <reason>` in the deliverable's final `Board proposal` section",
            "once the user approves",
            "without asking again",
            "Before a cancel or split residue, re-read the old issue's state",
            "no longer Backlog, Todo or In Review → cross-link instead and tell the user",
            "**Split residue** first",
            "in the old issue's state (In Review → Todo)",
            "its project, assignee and labels",
            "`related` to both the proposing issue and the old one",
            "re-point each issue it blocks in the old issue's team to be blocked by the proposing issue "
            "(and the residue issue, if any)",
            "comment on each blocked issue in another team",
            "Canceled as a Duplicate of the proposing issue",
            "comment the reason and the proposing issue's link",
            "**Cross-link**: a `related` link and a comment with the reason on each",
        ):
            self.assertIn(literal, proposals, literal)

    def test_the_files_and_doc_sections_it_names_exist(self):
        self.assertTrue(paths("manage"))
        for path in paths("manage"):
            self.assertTrue(os.path.isfile(path), path)
        cited = doc_sections("manage")
        self.assertIn(("docs/architecture.md", ["Board", "Using the board", "Finish"]), cited)
        for path, (top, *inner) in cited:
            with open(os.path.join(REPO, path), encoding="utf-8") as f:
                doc = f.read()
            head = re.search(rf"^(#+) {re.escape(top)}$", doc, re.M)
            self.assertIsNotNone(head, f"{path} › {top}")
            rest = doc[head.end() :]
            end = re.search(rf"^#{{1,{len(head[1])}}} ", rest, re.M)
            part = rest[: end.start()] if end else rest
            for name in inner:
                self.assertRegex(part, rf"(?m)^#+ {re.escape(name)}$|\*\*{re.escape(name)}:\*\*")

    def test_a_plugin_update_names_each_clients_command(self):
        update = rule("bumps the ensemble plugin version")
        self.assertIn("`claude plugin update ensemble@windroc`", update)
        self.assertIn("`codex plugin marketplace upgrade windroc`", update)
        wait = "once Release `v<version>` has its broker asset and sha256 file (`AGENTS.md` › Releasing)"
        self.assertLess(update.index(wait), update.index("update the installed plugin"))

    def test_a_shared_flakes_owner_fixes_its_root_cause_by_the_method(self):
        flake = rule("A flake or bug that several PRs hit has one owner")
        self.assertIn(
            "fixes its root cause (`src/ensemble/delegate/team/methods/fix-root-causes.md`); ",
            flake,
        )
        self.assertNotIn("› Rules", flake)

    def test_docker_host_workers_start_no_sessions_or_containers(self):
        [docker] = re.findall(r"^On the Docker Host .*$", body("manage"), re.M)
        for phrase in (
            "Crew starts each worker's and role run's container through the broker",
            "can start no session and no container",
            "no Docker socket and no broker",
            "the same launch commands and client flags as on this machine",
            "permission mode comes from its Host (`docs/architecture.md` › Client policy)",
        ):
            self.assertIn(phrase, docker)
        self.assertIn("so a worker or role run starts no sessions of its own", body("manage"))

    def test_releasing_uploads_and_confirms_the_asset_before_plugin_update(self):
        with open(os.path.join(REPO, "AGENTS.md"), encoding="utf-8") as f:
            releasing = re.search(r"(?ms)^## Releasing\n(.*?)(?=^## |\Z)", f.read())[1]
        order = [
            "publish Release `v<version>`",
            "`sh scripts/release-broker.sh v<version>`",
            "`ensemble-broker-darwin-arm64.tar.gz.sha256`",
            "`gh release view v<version> --repo windroc-labs/ensemble --json assets`",
            "update the installed plugin",
        ]
        found = [releasing.find(step) for step in order]
        self.assertNotIn(-1, found, releasing)
        self.assertEqual(found, sorted(found))
        self.assertNotIn("attestation", releasing)
        self.assertIn("A client without this plugin's `bin/` on its PATH", body("manage"))

    def test_a_pipeline_needs_input_has_its_own_steps(self):
        recover = section("manage", "Recover")
        [steps] = re.findall(r"^- \*\*Pipeline needs_input\*\*.*?(?=^- |\Z)", recover, re.M | re.S)
        order = [
            "`ensemble-delegate outcome <work_dir>/work/<ID>`",
            "`ensemble-orchestrator issue comment <ID> --body-file <file>`",
            "`ensemble-orchestrator issue move <ID> running`",
            "`ensemble-session ls --match '.*-<ID>-[0-9a-f]{8}'`",
            "`ensemble-session send <session> '<answer>'`",
            "transcript",
            "`ensemble-crew close <ID>`",
            "`ensemble-crew reopen --work-dir <work_dir> <ID>`",
        ]
        found = [steps.find(step) for step in order]
        self.assertNotIn(-1, found, steps)
        self.assertEqual(found, sorted(found))
        for phrase in (
            "`ensemble-orchestrator resume <ID>`",
            "`resume` field",
            "delivers no answer",
            "Never re-run the issue for an answer",
        ):
            self.assertIn(phrase, steps)
        self.assertNotIn("/work/<ID>/run`", steps)
        self.assertRegex(recover, r"(?m)^- \*\*Waiting role run\*\*")
        events = section("manage", "Events")
        self.assertIn("a role run: Recover › Waiting role run", events)
        self.assertIn("a pipeline run: Recover › Pipeline needs_input", events)
        self.assertIn("the router's reason", section("manage", "Launch"))

    def test_a_pipeline_answer_is_confirmed_per_client_and_its_tui_still_reports(self):
        [step] = re.findall(r"^  5\. .*?(?=^  6\. )", section("manage", "Recover"), re.M | re.S)
        reader = "${CLAUDE_SKILL_DIR}/../../src/ensemble/session/clients/opencode/transcript.py"
        found = [
            step.find(phrase)
            for phrase in (
                "transcript",
                f"`{reader}`",
                "`client_id` in `<work_dir>/work/<ID>/run/run.jsonl`",
                "Its tui still sends",
                "`blocked` and `done` events",
                "per Events",
                "for this one call only, never",
                "Once it finishes",
                "shows the new outcome when its harness still owns the tui",
                "write-back has posted that result and moved the issue",
                "still shows the old `needs_input` (no harness)",
                "its last outcome block",
                "`deliverable` for a `local` or `orchestrator` destination",
                "`<work_dir>/work/<ID>/agent/tmp/deliverable.md`",
                "else its `url`",
                "In Review (`docs/architecture.md` › Board › Using the board)",
                "`ensemble-orchestrator issue move <ID> review`",
            )
        ]
        self.assertNotIn(-1, found, step)
        self.assertEqual(found, sorted(found))
        self.assertIn(
            "A client that does not expand `${CLAUDE_SKILL_DIR}` (Codex, OpenCode) writes every"
            " `${CLAUDE_SKILL_DIR}` in this file as this file's directory",
            body("manage"),
        )
