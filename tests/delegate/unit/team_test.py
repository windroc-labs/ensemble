import os
import re
import tomllib
from dataclasses import replace
from importlib.resources import files

from ensemble import session
from ensemble.delegate.composer import Composer
from ensemble.delegate.composer.base import TEAM, outline
from ensemble.delegate.composer.teams import START
from ensemble.delegate.engine.config import read_config
from ensemble.utils import unfenced
from tests.delegate.unit.composer.prompts import (
    RUN,
    composed,
    item,
    parameters,
    rule,
    section,
    task_text,
)
from tests.delegate.unit.composer.shipped import (
    CHECKOUT_RULE,
    PICK,
    REPO_ARGS,
    TASKS,
    Packaged,
    RealConfig,
    catalog,
    expanded,
    guide,
    role_tasks,
    roles,
    runs,
    team_texts,
)
from tests.delegate.unit.composer.teams import INTRO

PUBLISHING = (
    {"type": "pull-request"},
    {"type": "github", "repo": "o/notes", "branch": "main", "dir": "reports/"},
)

WRITE_ONCE = (
    "\n**Write once**: write the deliverable's text once; a later step that needs it elsewhere copies "
    "that file (`cp`); only a Destination asking for it inline in the outcome repeats it.\n"
)


def destined(role, output, *, docker=False):
    """The real core's prompt for `role` with `output`, rendered for Docker when `docker`."""
    composition = Composer()
    run = composition.resolve(role, overlays=[{"roles": {role: {"output": output}}}])
    return composition.render(run, replace(RUN, docker=docker))


def output_rules(prompt):
    """The prompt's output.md part: from # Output up to its ## Destination."""
    return prompt.split("\n# Output\n", 1)[1].split("\n## Destination\n", 1)[0]


class Acceptance(Packaged):
    def test_each_requested_command_runs_a_team_tool(self):
        for role in roles():
            for command in composed(role)[1].commands:
                tool = re.match(r"python3 -P \{\{team\}\}/tools/([\w.-]+\.py) ", command)
                self.assertTrue(tool, (role, command))
                self.assertTrue(os.path.isfile(os.path.join(TEAM, "tools", tool[1])), command)
        self.assertIn(
            "python3 -P {{team}}/tools/svg_check.py *", composed("researcher")[1].commands
        )
        self.assertIn(
            "python3 -P {{team}}/tools/report_check.py *", composed("researcher")[1].commands
        )
        decompose = task_text("decompose")
        read = re.search(r"^1\. \*\*Read\*\*.*$", decompose, re.M)[0]
        # a document over the input's inline budget is only a link there, but tickets quote
        # its acceptance cells verbatim
        self.assertIn("fetch in full each document the input links but did not inline", read)
        # write-back's DECOMPOSED pattern reads this summary
        self.assertIn("`summary`: `Created <identifiers>; reused none`", decompose)

    def test_decompose_engineer_tickets_quote_acceptance_and_verifier_tickets_link_the_prd(self):
        draft = re.search(r"^2\. \*\*Draft\*\*.*$", task_text("decompose"), re.M)[0]
        # the gate and requirement-fidelity review judge an engineer ticket by its text alone
        self.assertIn(
            "acceptance quoting each covered requirement's PRD acceptance cell verbatim and in full",
            draft,
        )
        self.assertIn("Verifier tickets list the requirement identifiers and link the PRD", draft)
        self.assertIn("at most 8 requirement identifiers on an engineer ticket", draft)

    def test_report_delivery_and_figure_checks(self):
        prompt = composed("researcher")[0]
        concise = item(section(prompt, "Standards"), "Concise")
        for text in (
            "before [Output › Destination](#destination)",
            "python3 -P <team>/tools/report_check.py <file>",
            "at most 3 runs total",
            "Length check: <N> over",
            "Length check: failed: <error>",
        ):
            self.assertIn(text, concise)
        figures = item(section(prompt, "Standards"), "Figures")
        for text in (
            "only with a repo destination (`<pub>`)",
            "a file you write yourself beside `<file>`",
            "`![<caption>](<its file name>)`",
            "Before [Output › Destination](#destination) step 3",
            "python3 -P <team>/tools/svg_check.py <pub>/<svg>",
            "`SVG 跳过：<svg>: <reason>`",
            "Step 3 adds and commits the new or changed figures with `<file>`",
        ):
            self.assertIn(text, figures)
        self.assertNotIn("Visual", prompt)
        self.assertNotIn("visual-explainer", prompt)

    def test_template_owns_report_structure_and_task_depth(self):
        template = team_texts()["templates/research-report.md"]
        for text in (
            "> [!IMPORTANT]",
            "at most 3 reasons",
            "<type>: <task> · <web|local|mixed>",
            "12-character",
            "| Light Research |",
            "| Research |",
            "| Deep Research |",
            "a figure (Mermaid, or SVG: [Researcher › Standards › Figures](#standards)) or table",
            "No essay paragraphs.",
            "`[n]` is source number n in Sources, written as a link to that source, "
            "`[[n]](<URL or code permalink>)`, the same target as its Sources item",
            "Both have:",
            "A compared project with its own address is a link to it",
            "| [Grok Build](https://github.com/xai-org/grok-build) |",
            "| Impact | Gap |",
            '<details id="findings" open>',
            "<summary>Findings: <A> aspects, <F> findings</summary>",
            "<summary>Sources (<count>)</summary>",
            "at most once per section",
            "bold answer",
            "then a blank line (the blank line before `---` is required",
            "`### <n>. <Aspect name>`",
            "`> **<Answer, translated>:** <this aspect's answer, 1–2 sentences> (<confidence>)`",
            "`#### <n.m> <claim>`, the claim one plain sentence; then a body of 2–5 sentences",
            "that aspect says `see <n.m>` instead of repeating it",
            "| Practice, pitfalls | Do / avoid table with the reason per row |",
            "Point to findings by number (`see 2.3`)",
            "## What it means for us",
            "omit when the input asks no decision",
        ):
            self.assertIn(text, template)
        for task in ("research", "light-research"):
            step = task_text(task).split("6. **Report**", 1)[1].split("\n7.", 1)[0]
            self.assertIn("Template", step)
            self.assertNotIn("<commit>", step)
            self.assertNotIn("type line, translated", step)
        writing = section(composed("researcher")[0], "Writing")
        for label in ("high", "medium", "low", "single-source", "unverified"):
            self.assertIn(f"`{label}`", writing)

    def test_a_local_destination_names_deliverable_file(self):
        prompt = composed(
            "researcher", overlays=[{"roles": {"researcher": {"output": {"type": "local"}}}}]
        )[0]
        destination = section(prompt, "Destination")
        self.assertIn(
            (
                "Write the deliverable to `<Workdir>/out.md` and name that file in the outcome's "
                "`deliverable_file`"
            ),
            destination,
        )
        self.assertNotIn("outcome's `deliverable`", destination)
        self.assertIn("Leave `url` empty.", destination)
        self.assertEqual(prompt.count("`<Workdir>/out.md`"), 1)
        self.assertIn(
            (
                "\n- `deliverable_file`: a `.md` file in `<Workdir>` holding the deliverable, in place "
                "of `deliverable`, when [Output › Destination](#destination) says so.\n"
            ),
            prompt,
        )

    def test_publishing_destinations_name_no_outcome_deliverable(self):
        for role in roles():
            for output in PUBLISHING:
                for docker in (False, True):
                    with self.subTest(role=role, output=output["type"], docker=docker):
                        prompt = destined(role, output, docker=docker)
                        self.assertNotIn("`deliverable`", prompt)
                        self.assertNotIn("`deliverable_file`", prompt)

    def test_a_publishing_prompts_output_rules_lack_only_the_two_deliverable_fields(self):
        fields = (
            "- `deliverable`: the document, when [Output › Destination](#destination) says so.\n",
            (
                "- `deliverable_file`: a `.md` file in `<Workdir>` holding the deliverable, in place "
                "of `deliverable`, when [Output › Destination](#destination) says so.\n"
            ),
        )
        returning = output_rules(destined("researcher", {"type": "local"}))
        for field in fields:
            self.assertEqual(returning.count(field), 1)
            returning = returning.replace(field, "")
        for output in PUBLISHING:
            with self.subTest(output=output["type"]):
                self.assertEqual(output_rules(destined("researcher", output)), returning)

    def test_every_destination_has_the_write_once_rule(self):
        for output in ({"type": "local"}, *PUBLISHING):
            for docker in (False, True):
                with self.subTest(output=output["type"], docker=docker):
                    self.assertIn(
                        WRITE_ONCE, output_rules(destined("researcher", output, docker=docker))
                    )
        step = task_text("product-design").split("7. **Review.**", 1)[1].split("\n8.", 1)[0]
        self.assertIn("`<Workdir>/tmp/prd.md`", step)
        self.assertIn("copies that file", step)

    def test_prompts_leave_operation_permissions_to_the_token(self):
        limits = (
            "**No comments**",
            "**Board boundary**",
            "**Write scope**",
            "authorize more board operations",
            "never pushes a branch or opens a PR",
            "commit and push nothing",
            "otherwise touch any other branch",
        )
        for role, task in runs():
            with self.subTest(role=role, task=task):
                prompt = composed(role, task)[0]
                for limit in limits:
                    self.assertNotIn(limit, prompt)

    def test_each_task_file_has_one_start_mark_and_a_resume_section(self):
        for name in os.listdir(TASKS):
            text = task_text(name.removesuffix(".md"))
            self.assertEqual(len(START.findall(text)), 1, name)
            self.assertIn("## Resume", [line.rstrip() for _, line in unfenced(text)], name)


class Shipped(Packaged):
    def test_text_holds_only_the_placeholders_render_fills(self):
        inline = {"role", "role_anchor", "task", "language", "deliverable"}
        for rel, text in team_texts().items():
            if rel.endswith(".md"):
                self.assertLessEqual(set(re.findall(r"\{\{(\w+)", text)), inline, rel)

    def test_each_charter_opens_with_its_index_naming_a_listed_default(self):
        defaults = {
            "researcher": "research",
            "pm": "product-design",
            "engineer": "build",
            "verifier": "verify",
            "dummy-tester": "echo",
        }
        self.assertEqual(set(roles()), set(defaults))
        for role, default in defaults.items():
            with open(os.path.join(TEAM, "roles", f"{role}.md")) as f:
                text = f.read()
            self.assertTrue(
                text.split("\n## ")[1].startswith(f"Tasks\n\n{INTRO.format(default)}- `"), role
            )
            self.assertIn(default, catalog()[role], role)
            for t, line in catalog()[role].items():
                self.assertIn(f"\n- `{t}` (`<tasks>/{t}.md`): {line}\n", text, role)
                self.assertRegex(line, r"; (light|heavy)\.$", role)
        self.assertEqual(
            sorted(t for _, t in role_tasks()),
            sorted(f.removesuffix(".md") for f in os.listdir(TASKS)),
        )

    def test_task_files_have_no_frontmatter_and_no_placeholder(self):
        for name in os.listdir(TASKS):
            text = task_text(name.removesuffix(".md"))
            self.assertTrue(text.startswith("# "), name)
            self.assertNotIn("{{", text, name)
            self.assertNotIn("ensemble-progress:budget", text, name)

    def test_engineers_tasks_name_their_skill_by_slash_command(self):
        for t in ("build", "light-build"):
            self.assertIn(f" with `/updraft:{t}` (", catalog()["engineer"][t])
            self.assertIn(f"`/updraft:{t}`", task_text(t))
            self.assertNotRegex(task_text(t), r"(?<!/)updraft:")

    def test_the_verify_task_runs_mini_op_and_fixes_its_outcome_statuses(self):
        self.assertIn(" with the `mini-op` skill", catalog()["verifier"]["verify"])
        text = task_text("verify")
        self.assertIn("the `mini-op` skill", text)
        self.assertNotIn("/mini-op:run", text)
        steps = text.split("## Steps", 1)[1].split("## Resume", 1)[0]
        for literal in (
            "No clear check → `needs_input`",
            "The check ran to its end, passing or not → `status: done`",
            "`summary` opens with the report's Verdict mark, ✅, ❌ or ⚠️",
            "The check could not run (the mini unreachable, the runbook broke midway, "
            "the `mini-op` skill missing) → `status: failed`",
            "The request is still unclear → `needs_input`",
            "The `done` deliverable is the report, per the Template.",
            "/run/ensemble/secrets/",
        ):
            self.assertIn(literal, steps)
        self.assertNotIn("full text", steps)

    def test_the_verify_report_template_is_one_table_with_a_column_per_round(self):
        template = team_texts()["templates/verify-report.md"]
        for text in (
            "**Verdict:** <✅ | ❌ | ⚠️>: <one line why>\n\n✅ pass · ❌ fail · ⚠️ partial or problem · ⏭️ not run\n\n",
            "| Requirement | Check | R1 | Notes (latest non-✅ only) |",
            "the Verdict line, the legend line, the table",
            "Marks: only the legend's four",
            "keep every earlier round's column and add the next one (R<n+1>)",
            "never restart at R1",
            "carry its per-requirement results into R1..Rn columns",
            "Blocked / next:",
        ):
            self.assertIn(text, template)
        self.assertNotIn("start a new table at R1", template)
        self.assertNotIn("⏭️ skipped", template)
        checklist = item(composed("verifier", "verify")[0], "Checklist first")
        self.assertIn("the Template's table", checklist)
        self.assertIn("extending an earlier report", checklist)
        self.assertNotIn("evidence", checklist)

    def test_every_plugin_the_team_calls_is_in_the_default_docker_whitelist(self):
        """Or, for a role whose packaged environment adds it, in that environment's plugins."""
        prefix = re.compile(r"(?<![\w/.])/([a-z0-9][a-z0-9-]*):[a-z]")
        ((package, name),) = session.CONFIG_RESOURCES
        with files(package).joinpath(name).open("rb") as f:
            whitelist = {s.split("@", 1)[0] for s in tomllib.load(f)["hosts"]["docker"]["plugins"]}
        packaged = read_config(local=os.devnull)
        named = packaged["environments"]
        checked = set()
        for role in roles():
            selected = packaged["roles"].get(role, {}).get("environment", {}).get("name")
            extra = set(named[selected]["plugins"]) if selected else set()
            texts = [composed(role)[0], *(task_text(t) for t in catalog()[role])]
            checked.add(f"roles/{role}.md")
            checked.update(f"tasks/{task}.md" for task in catalog()[role])
            called = {m for text in texts for m in prefix.findall(text)}
            with self.subTest(role=role):
                self.assertLessEqual(called, whitelist | extra)
        for path, text in team_texts().items():
            if path not in checked:
                with self.subTest(path=path):
                    self.assertLessEqual(set(prefix.findall(text)), whitelist)
        self.assertEqual(prefix.findall(task_text("verify")), [])

    def test_product_design_reviews_the_prd_with_updraft_review_to_its_default_cap(self):
        text = task_text("product-design")
        step = text.split("7. **Review.**", 1)[1].split("\n8.", 1)[0]
        for literal in (
            "`/updraft:review`",
            "`doc=` the PRD's file",
            "`requirement=` the brief and the user's later words, verbatim, with the documents they name",
            "asking no more rigor or scope than the brief does",
            "`capped` or `needs-author` → the PRD still ships; its open blockers and `needs-author`'s "
            "questions go under Open questions, and the outcome's `summary` gives the blockers' count.",
        ):
            self.assertIn(literal, step)
        self.assertNotIn("cap=", step)
        self.assertNotIn("fresh subagent", step)
        self.assertNotRegex(text, r"(?<!/)updraft:")
        self.assertIn("`/updraft:review` again with the same `doc`", text.split("## Resume", 1)[1])

    def test_product_design_settles_open_questions_with_updraft_council_before_finishing(self):
        text = task_text("product-design")
        step = text.split("8. **Council.**", 1)[1].split("\n9.", 1)[0]
        self.assertIn("\n9. **Finish.**", text)
        for literal in (
            "`/updraft:council`",
            "changes external state or can't be undone",
            "stays there, for the user",
            "write each decision into the PRD",
            "the author's call",
            "`needs_input`",
        ):
            self.assertIn(literal, step)
        self.assertIn("what you can't settle → Open questions", text.split("5. **Self-grill**")[1])
        self.assertIn("never run it again", text.split("## Resume", 1)[1])

    def test_the_engineer_charter_has_six_sections_and_updraft_adds_its_lines_verbatim(self):
        with open(os.path.join(TEAM, "roles", "engineer.md")) as f:
            charter = f.read()
        heads = [path[1] for _, path, _, heading in outline(charter) if heading and len(path) == 2]
        self.assertEqual(heads, ["Tasks", "Rules", "Input processing", "Repo", "Updraft", "Finish"])
        lines = (
            (
                "Work only in `<worktree>` on branch `<branch>`, with absolute paths; create no other clone, worktree "
                "or branch."
            ),
            "Commits merged from `origin/<base>` are not this build's work.",
            "Your and your producers' local checks, in every phase: only lint and the unit tests of the projects "
            "you changed; never integration tests or full suites: the verification reviewer runs them, on the PR's "
            "CI when the repo has a `pull_request` workflow, else locally.",
            "Ask for no pull request: the caller opens it, onto `<base>`.",
            "After <your task's push points>, run exactly `git -C <worktree> push -u origin <branch>`.",
        )
        self.assertIn(
            "\nAdd verbatim, placeholders filled in:\n\n```\n" + "\n".join(lines) + "\n```\n",
            section(charter, "Updraft"),
        )

    def test_builds_report_each_s5_task_against_their_task_count(self):
        for task, doc in (("build", "the plan doc's"), ("light-build", "the plan doc's")):
            text = task_text(task)
            self.assertIn(
                (
                    "**Report progress** at the end of each build step (`S<i>`) you run and of each S5 task:"
                ),
                text,
                task,
            )
            self.assertIn(
                (
                    "[ensemble-progress:step] the step and its result; an S5 task as "
                    f"`S5 task <k>/<n> done: <commit>; <checks run>`, `<k>` its number, `<n>` {doc} task count "
                    "(its `### Task` headings)"
                ),
                text,
                task,
            )

    def test_builds_leave_their_docs_where_updraft_puts_them(self):
        self.assertIn(
            (
                'docs line "Spec and plan doc stay where updraft puts them; S7 copies them to where the repo '
                'keeps design docs; never `git add -f` them."'
            ),
            task_text("build"),
        )
        self.assertIn(
            (
                'docs line "The plan doc stays where updraft puts it; S7 copies it to where the repo keeps '
                'design docs; never `git add -f` it."'
            ),
            task_text("light-build"),
        )
        self.assertIn(
            "(a task list in its plan doc, implementation, verification, a light review, no spec doc)",
            catalog()["engineer"]["light-build"],
        )

    def test_builds_continue_the_newest_unfinished_plan_doc_by_the_engineers_one_rule(self):
        continue_ = item(section(composed("engineer")[0], "Repo"), "Continue")
        self.assertIn(
            (
                " a plan doc: [Engineer › Repo › Merge](#repo), then [Engineer › Repo › Draft](#repo), then "
                "your task's slash command "
                "([Engineer › Tasks](#tasks)) with the plan doc's verbatim input as its requirement; "
                "updraft's resolver `mode` not `resume` → `needs_input` naming the plan doc; stop. Once it "
                "resumes, give it the rest as mid-run input, each logged in its progress log: the user's "
                "requirements, the review input (marked as [Engineer › Updraft](#updraft) says), failing "
                "merge checks, and with a user requirement a review cap 2 rounds beyond its RESUME "
                "`review_round`, never below the skill's cap (a capped plan doc stops again at once). Added to "
                "its input, they would start a new plan doc at HEAD, leaving its commits unreviewed."
            ),
            continue_,
        )
        for task in ("build", "light-build"):
            text = task_text(task)
            self.assertIn(
                "2. **Repo** ([Engineer › Repo](#repo)); from status, act on `plan_docs` too.", text
            )
            self.assertIn(
                "\n   - The newest plan doc (`plan_docs`' last) not at `done` → continue it.\n"
                "   - Else a plan doc or a `pr` (a **finished build**) and a user requirement → a new build",
                text,
                task,
            )
            self.assertIn(
                "\n5. **Build**: continuing → [Engineer › Repo › Continue](#repo); else "
                f"`/updraft:{task}` ([Engineer › Updraft](#updraft)), the requirements being",
                text,
                task,
            )
            self.assertNotIn("mid-run", text, task)

    def test_engineer_lists_non_blocking_items_commits_nothing_after_and_reports_updraft(self):
        with open(os.path.join(TEAM, "roles", "engineer.md")) as f:
            charter = f.read()
        self.assertIn(
            (
                "- **Done**, once the build converges, committing nothing more: [Engineer › Repo](#repo) step 2 "
                "again (its `pr` now the draft PR the build may have opened), then `status: done`; "
                "the deliverable (the PR description): what changed, the input's `Links:`, how to verify, and "
                "each of this run's builds' "
                "deferred non-blocking review items verbatim, for the manager or the user to decide on (never "
                "fix them or start a build for them); `updraft` the `status` and `head` of this run's last "
                "`updraft-result`; `title`"
            ),
            charter,
        )
        self.assertNotIn("leftover", charter)
        self.assertNotIn("`deliverable`", charter)
        self.assertIn("redo Done's deliverable and `updraft`", charter)
        self.assertNotIn("Merge](#repo)", section(composed("engineer")[0], "Finish"))
        self.assertIn(
            '- `updraft`: `{"status", "head"}`, when your task asks for it.\n',
            composed("engineer")[0],
        )

    def test_engineer_prs_are_drafts_until_the_delegate_readies_them(self):
        prompt = composed("engineer")[0]
        destination = section(prompt, "Destination")
        self.assertIn(
            "null → `gh pr create --draft --repo <host>/<owner>/<name> --head", destination
        )
        self.assertNotIn("gh pr ready", destination)
        repo_ = section(prompt, "Repo")
        self.assertIn(
            (
                ": before each updraft start, by the latest `repo.py status`: `pr.state` `OPEN` and "
                "`pr.isDraft` false → `gh pr ready --undo <pr.number> --repo <host>/<owner>/<name>`. Only the "
                "Delegate marks a PR ready."
            ),
            item(repo_, "Draft"),
        )
        for where, text in (
            ("Updraft", section(prompt, "Updraft")),
            ("Repo › Continue", item(repo_, "Continue")),
        ):
            self.assertIn(
                "[Engineer › Repo › Merge](#repo), then [Engineer › Repo › Draft](#repo), then",
                text,
                where,
            )

    def test_research_tasks_prepare_before_their_start_mark(self):
        for task in ("research", "light-research"):
            text = task_text(task)
            self.assertLess(
                text.index("**Prepare**"), text.index("[ensemble-progress:start]"), task
            )

    def test_research_notes_each_findings_evidence(self):
        for task in ("research", "light-research"):
            with self.subTest(task=task):
                self.assertIn("the claim, its evidence, its sources", task_text(task))

    def test_light_research_is_one_pass(self):
        light = task_text("light-research")
        self.assertIn("in one pass: no subagents, no workflow", light)
        self.assertNotIn("angles", light)


class Real(RealConfig):
    def test_fixed_labels_are_translated_and_template_headings_say_so(self):
        self.assertIn(
            (
                "- Reports and PRDs, headings and fixed labels included, are in Chinese; a `<X, translated>` in "
                "a template or task is X in Chinese."
            ),
            composed("researcher")[0],
        )
        for role, task in role_tasks():
            text = composed(role, task)[0]
            for literal in ("`Check:`", "Check:", "Confidence:", "`Light Research. Angles: "):
                self.assertNotIn(literal, text, (role, task, literal))
        for role, task in runs():
            self.assertIn(
                (
                    "Its headings are fixed apart from their language ([Principles › Writing](#writing))"
                ),
                guide(composed(role, task)[0]),
                (role, task),
            )

    def test_writing_has_the_one_gloss_and_diagram_rules_and_no_length_cap(self):
        for role, task in runs():
            prompt = composed(role, task)[0]
            writing = section(prompt, "Writing")
            self.assertNotIn("the first mention adds the", prompt, (role, task))
            self.assertIn(
                (
                    "A term's first mention adds one gloss, its Chinese rendering (for an acronym, of its full "
                    "name); none when the term is common in Chinese, and none in a heading."
                ),
                writing,
            )
            diagrams = item(writing, "Diagrams and tables")
            for literal in (
                "a Mermaid diagram",
                "`flowchart`",
                "`sequenceDiagram`",
                "`timeline`",
                "short node labels",
                "a table",
                "a list",
                "a caption",
                "delete the text the diagram replaces",
                "A research report: its Template's Body rule instead.",
            ):
                self.assertIn(literal, diagrams, (role, task, literal))
            self.assertNotIn("3000 words", prompt, (role, task))
            self.assertNotIn("5 sentences", writing, (role, task))

    def test_every_run_judges_related_work_first_and_only_proposes_board_changes(self):
        for role, task in runs():
            prompt = composed(role, task)[0]
            overlap = section(prompt, "Overlap")
            for literal in (
                "Input has a `## Related work` section",
                "before your task's steps",
                "(the first row that fits wins)",
                "the candidate has scope the request lacks",
                "one line per action",
                "the user's later words say to go on",
                "finished, awaiting review, in progress, not started or canceled",
                *(f"| `{v}` |" for v in ("covered", "supersedes", "intersects", "partial", "none")),
                "not started: never `covered`; the same scope is `supersedes`",
                "`covered` without **evidence** → at most `partial`.",
                "Across candidates, skip any part a partial candidate already answers, whatever "
                "another candidate's verdict.",
                "`covered` beats every other verdict",
                "return `needs_input`, its `questions` naming the candidate's id and **evidence**",
                "canceled: its last comment, as the cancel reason",
                "suggest closing `<id>` as a duplicate of it",
                "| `supersedes` | not started, awaiting review | all of the request | cancel |",
                "| `intersects` | not started, awaiting review | all of the request, the shared part "
                "included | split residue, then cancel |",
                "| `supersedes` | finished |",
                "lists its output as an earlier version",
                "| `intersects` | finished | as `partial` |",
                "| `supersedes`, `intersects` | in progress, canceled |",
                "only the part not yet answered; the deliverable cites the candidate and its output as sources",
                "`<candidate id>: <cancel | split residue | cross-link>: <reason>`",
                "indented `Title:` and `Description:` lines for the residue",
                "Cancel: as a duplicate of `<id>`",
                "`none` → no mention.",
            ):
                self.assertIn(literal, overlap, (role, task, literal))
            rows = [
                overlap.index(f"| `{v}` |")
                for v in ("covered", "supersedes", "intersects", "partial", "none")
            ]
            self.assertEqual(rows, sorted(rows), (role, task))
            self.assertIn(
                "or `covered` related work ([Principles › Overlap](#overlap))", prompt, (role, task)
            )

    def test_templates_put_the_summary_first_one_fact_per_item_and_point_to_the_diagram_rule(self):
        pm, light = composed("pm", "product-design")[0], composed("researcher", "research")[0]
        point = "[Principles › Writing › Diagrams and tables](#writing)"
        self.assertIn("One fact per list item", pm)
        self.assertIn("Outside Findings, one fact per list item", light)
        for text in (pm, light):
            self.assertIn("never repeats `[Reference]`", text)
        self.assertIn("Opens with one sentence", section(pm, "Problem and goals"))
        self.assertIn(point, section(pm, "User flow"))
        self.assertIn(point, section(pm, "Approach and trade-offs"))
        self.assertIn(point, section(pm, "System diagram"))
        requirements = section(pm, "Requirements")
        self.assertIn("Each subsection is one table", requirements)
        self.assertIn("fills the Module cell of its first row only", requirements)
        for row in ("`| FR-<n> | <module> | <the requirement> | ", "`| NFR-<n> | <module> | "):
            self.assertIn(row, pm)
        self.assertIn("> **<one-sentence answer>** (<overall confidence>)", light)
        self.assertIn(point, section(light, "Comparison table"))
        findings = section(light, "Findings")
        self.assertNotIn(point, findings)
        self.assertIn(
            "`(<confidence: high, medium or low>; <single-source, unverified or sources disagree, "
            "when so>)`",
            findings,
        )
        self.assertIn(
            "`1. [<Short title>](<URL or code permalink>) (primary|secondary|code)`",
            section(light, "Sources"),
        )
        for task in ("research", "light-research"):
            self.assertIn("Type line and verification depth: the Template.", task_text(task))

    def test_the_guide_names_a_given_task_else_the_run_picks_it(self):
        named = guide(composed("researcher", "research")[0])
        self.assertIn(
            "**Your task**: `research` (`<tasks>/research.md`). Read that task's file "
            "(no other task's)",
            named,
        )
        self.assertNotIn("pick it from", named)
        picked = guide(composed("researcher")[0])
        self.assertIn(PICK, picked)
        self.assertIn("Your start progress report names the task and why.", picked)
        self.assertNotIn("`research`", picked)

    def test_a_prompt_has_its_roles_index_and_no_task_steps(self):
        for role, task in runs():
            prompt, _ = composed(role, task)
            for t, line in catalog()[role].items():
                self.assertIn(f"\n- `{t}` (`<tasks>/{t}.md`): {line}\n", prompt, (role, task))
                for step in task_text(t).splitlines():
                    if len(step.strip()) >= 30 and not step.startswith("#"):
                        self.assertNotIn(step.strip(), prompt, (role, task, t))

    def test_deep_research_falls_back_to_both_method_files(self):
        prompt, _ = composed("researcher")
        self.assertEqual(parameters(prompt)["<team>"], f"`{TEAM}`")
        for name in ("deep-research", "ultracode"):
            self.assertTrue(os.path.isfile(os.path.join(TEAM, "methods", f"{name}.md")), name)
        self.assertIn("`<team>/methods/deep-research.md`", prompt)
        self.assertIn("`<team>/methods/ultracode.md`", task_text("deep-research"))
        for phrase in (
            "No such tool → follow the deep-research method ([Researcher › Methods](#methods)).",
            "per the **ultracode method** `<team>/methods/ultracode.md` (read-only)",
            "The ultracode round's agents are readers",
            "the deep-research method's agents are web agents",
            "No Workflow tool → follow that method.",
            "Before the second, the brake ([Researcher › Methods](#methods)).",
            "No subagents with a round's tools → skip that round, under Gaps.",
        ):
            self.assertIn(phrase, task_text("deep-research"))

    def test_the_summary_is_short_and_details_go_in_the_deliverable(self):
        for role, task in runs():
            with self.subTest(role=role, task=task):
                prompt = composed(role, task)[0]
                self.assertIn(
                    "- `summary`: 1–2 short lines (≤ 300 characters); every detail goes in the "
                    "deliverable.",
                    prompt,
                )
        for path, text in team_texts().items():
            for stale in ("3–5 lines", "to `summary`", "`summary` with", "`summary`'s final line"):
                with self.subTest(path=path, stale=stale):
                    self.assertNotIn(stale, text)
        overlap = section(composed("researcher")[0], "Overlap")
        self.assertIn(
            "- **Board proposal**: a final `Board proposal` section of the deliverable, one line per "
            "action: ",
            overlap,
        )
        standards = section(composed("researcher")[0], "Standards")
        self.assertIn(
            "- `summary`: line 1 the answer and the type; line 2, only when there are any, the **status "
            "items** (upgrade advice, SVG status, length check, in that order) joined by `; `.",
            standards,
        )

    def test_a_failed_runs_summary_is_one_line(self):
        for role, line in (
            (
                "researcher",
                "Every repo failed → local: `failed`, `summary` one line naming each failed repo "
                "with its error's first line, within 300 characters, the errors verbatim in the "
                "deliverable; mixed: drop the local part.",
            ),
            (
                "engineer",
                "- Exit 1 → `failed`, `summary` one line, the error's first line within 300 "
                "characters; stop.",
            ),
        ):
            with self.subTest(role=role):
                self.assertIn(line, composed(role)[0])
        for path, text in team_texts().items():
            for stale in (
                "`summary` quotes the errors",
                "`summary` the error;",
                "the error verbatim in the deliverable",
            ):
                with self.subTest(path=path, stale=stale):
                    self.assertNotIn(stale, text)

    def test_only_the_deep_research_task_names_ultracode(self):
        # Claude Code answers a bare ultracode with a Workflow reminder, so no prompt says it.
        for task in (None, *catalog()["researcher"]):
            with self.subTest(task=task):
                self.assertNotIn("ultracode", composed("researcher", task)[0].lower())
        for task in catalog()["researcher"]:
            with self.subTest(task=task):
                named = "ultracode" in task_text(task).lower()
                self.assertEqual(named, task == "deep-research")

    def test_every_repo_command_takes_the_inputs_checkout(self):
        found = {}
        for role in roles():
            prompt, _ = composed(role)
            text = prompt + "".join(task_text(t) for t in catalog()[role])
            cmds = re.findall(r"`python3 -P <team>/tools/repo\.py (worktree|status) ([^`]*)`", text)
            self.assertNotIn("<scripts>", text, role)
            found[role] = [cmd for cmd, _ in cmds]
            for cmd, args in cmds:
                self.assertEqual(args, REPO_ARGS[cmd], role)
            self.assertEqual(rule(prompt, "Checkout"), [CHECKOUT_RULE], role)
        self.assertEqual(
            found,
            {
                "researcher": ["worktree"],
                "pm": ["worktree"],
                "engineer": ["worktree", "status"],
                "verifier": ["worktree"],
                "dummy-tester": ["worktree"],
            },
        )

    def test_every_repo_named_by_url_may_be_a_local_clone_path(self):
        for role in roles():
            text = composed(role)[0] + "".join(task_text(t) for t in catalog()[role])
            self.assertIn("repo URL", text, role)
            self.assertEqual(
                text.count("repo URL"),
                len(re.findall(r"repo URLs? or local clone paths?", text)),
                role,
            )

    def test_literals_a_role_or_task_text_holds(self):
        engineer, researcher = composed("engineer")[0], composed("researcher")[0]
        for why, text, literal in (
            ("builds fail without push permission", engineer, "`push` false → `failed`"),
            (
                "the pull request destination reads pr from repo status",
                engineer,
                "3. By `repo.py status`'s `pr`: null → `gh pr create",
            ),
            *(
                (
                    "pm and researcher point to writing for concision",
                    composed(role)[0],
                    f"- **Concise**: after writing {doc}, cut repetition and preamble "
                    "([Principles › Writing](#writing)); length follows content.",
                )
                for role, doc in (("pm", "the PRD"),)
            ),
            (
                "echo's summary overrides Output's length",
                task_text("echo"),
                "`summary` the input's first line, cut to 300 characters.",
            ),
            (
                "a build's files are its spec and plan doc",
                task_text("build"),
                "adding `files`: this build's spec (the `-spec.md` beside its `-plan.md` plan doc) and plan doc "
                "(on failure, those that exist);",
            ),
            (
                "research hand-off phrases",
                task_text("research"),
                "`Suggest upgrading to Deep Research: ",
            ),
            (
                "research hand-off phrases",
                task_text("light-research"),
                "`Suggest upgrading to Research: ",
            ),
            (
                "research hand-off phrases",
                task_text("research"),
                "Revising a Light Research report (its Light Research type line marks it",
            ),
            (
                "research hand-off phrases",
                task_text("deep-research"),
                "Revising a Research or Light Research report (its type line marks it",
            ),
            (
                "a read-only task's branch is <id>-<task>",
                researcher,
                "`<branch>` `<id>-<task>`, `<task>` this task, `deep-research`, `research` or `light-research`",
            ),
            (
                "a read-only task's branch is <id>-<task>",
                task_text("product-design"),
                "`<branch>` `<id>-product-design`",
            ),
            (
                "a run stopped in Prepare starts it over",
                task_text("light-research"),
                "Step 4 not started → step 2. Else reuse this session's findings",
            ),
            (
                "an unlabelled Light Research upgrade is Research",
                researcher,
                "or no task is named and the input revises a Light Research report (an upgrade); light.",
            ),
            (
                "an unlabelled Light Research upgrade is Research",
                researcher,
                "when the answer must be reliable, or a Research report left gaps; heavy.",
            ),
        ):
            with self.subTest(why, literal=literal):
                self.assertIn(literal, text)
        self.assertNotIn("a quick pass left gaps", researcher)

    def test_builds_wait_for_the_prs_checks(self):
        for task in ("build", "light-build"):
            done = item(section(composed("engineer", task)[0], "Finish"), "Done")
            for literal in (
                "`gh pr checks <branch> --repo <host>/<owner>/<name> --watch --fail-fast`",
                "`gh run view <run-id> --repo <host>/<owner>/<name> --log-failed`",
                "a new build of your task's slash command ([Engineer › Updraft](#updraft))",
                "redo Done's deliverable and `updraft`, then [Output › Destination](#destination) updates "
                "the PR, then step 1",
                "3 fix builds",
                "20 minutes",
                "[Engineer › Finish › Failure](#finish)",
            ):
                self.assertIn(literal, done, task)

    def test_builds_merge_the_base_branch_before_updraft_at_resume_and_before_the_pr(self):
        prompt, _ = composed("engineer")
        repo_ = section(prompt, "Repo")
        self.assertIn(
            "JSON `base` → `<base>`: the PR's base branch, else `Base:`, else `<default>`.", repo_
        )
        merge = item(repo_, "Merge")
        for literal in (
            "[Engineer › Repo](#repo) step 1",
            "`git -C <worktree> status`",
            "a merge in progress",
            "`git -C <worktree> merge --no-edit origin/<base>`",
            "`git -C <worktree> merge --abort`",
        ):
            self.assertIn(literal, merge)
        for where, text in (
            ("Updraft", section(prompt, "Updraft")),
            ("Repo › Resume", item(repo_, "Resume")),
        ):
            self.assertIn("[Engineer › Repo › Merge](#repo)", text, where)
        self.assertIn("`origin/<base>`", section(prompt, "Updraft"))
        for literal in (
            "--head <branch> --base <base> --title",
            "/compare/<base>...<branch>?expand=1",
        ):
            self.assertIn(literal, prompt)
        for task in ("build", "light-build"):
            self.assertIn("[Engineer › Repo › Resume](#repo).", task_text(task), task)

    def test_merges_failing_checks_go_into_the_next_build_and_resume_merges_only_before_one(self):
        repo_ = section(composed("engineer")[0], "Repo")
        self.assertIn(
            (
                "One failing → the failing checks go into the next build's requirements (mid-run for a "
                "continued plan doc: [Engineer › Repo › Continue](#repo))."
            ),
            item(repo_, "Merge"),
        )
        self.assertIn(
            "steps 1–2 again, then, with a build left, [Engineer › Repo › Merge](#repo), then",
            item(repo_, "Resume"),
        )


UNIVERSAL = {
    "guard-the-context-window",
    "never-block-on-the-human",
    "attack-the-premise",
    "build-the-lever",
    "prove-it-works",
    "explain-the-number",
}


def _principle_citations(text):
    return {
        name
        for pair in re.findall(
            r"\[`?([\w-]+)`?\]\(#principle-index\)|<team>/principles/([\w-]+)\.md", text
        )
        for name in pair
        if name
    }


class Principles(Packaged):
    def test_index_covers_each_leaf_once_and_resolves_in_every_role(self):
        index = section(team_texts()["principles.md"], "Principle index")
        leaves = {
            name.removesuffix(".md")
            for name in os.listdir(os.path.join(TEAM, "principles"))
            if name.endswith(".md") and name != "README.md"
        }
        self.assertEqual(len(leaves), 24)
        self.assertEqual(set(re.findall(r"<team>/principles/([\w-]+)\.md", index)), leaves)
        for name in leaves:
            self.assertEqual(index.count(f"<team>/principles/{name}.md"), 1)
        for role in roles():
            prompt, run = composed(role)
            self.assertIn(os.path.join(TEAM, "principles", "fix-root-causes.md"), expanded(prompt))
            self.assertEqual(run.read, [])

    def test_task_principle_citations_resolve_and_shape_steps(self):
        index = section(team_texts()["principles.md"], "Principle index")
        names = set(re.findall(r"<team>/principles/([\w-]+)\.md", index))
        for task in os.listdir(os.path.join(TEAM, "tasks")):
            text = task_text(task.removesuffix(".md"))
            with self.subTest(task=task):
                self.assertNotRegex(text, r"(?im)^#+ .*principles")
                citations = _principle_citations(text)
                self.assertLessEqual(citations, names)
                mentioned = {name for name in names if f"`{name}`" in text}
                self.assertEqual(mentioned, citations)
                for line in text.splitlines():
                    if "(#principle-index)" not in line:
                        continue
                    self.assertIn(line, section(text, "Steps"))
                    self.assertRegex(line, r"^\s*(?:\d+\.|-) \*\*[^*]+\*\*")
                    self.assertEqual(line.count("(#principle-index)"), 1)
                    self.assertRegex(line, r"\]\(#principle-index\) [A-Za-z]")

    def test_role_and_task_citations_resolve_without_overlap(self):
        index = section(team_texts()["principles.md"], "Principle index")
        names = set(re.findall(r"<team>/principles/([\w-]+)\.md", index))
        for role, tasks in catalog().items():
            with open(os.path.join(TEAM, "roles", f"{role}.md")) as f:
                selected = _principle_citations(f.read())
            self.assertLessEqual(selected, names, role)
            for task in tasks:
                task_principles = _principle_citations(task_text(task))
                self.assertLessEqual(task_principles, names, (role, task))
                self.assertFalse(selected & task_principles, (role, task))
        universal = index.split("| Principle", 1)[0]
        for name in UNIVERSAL:
            self.assertIn(f"`{name}`", universal)
        self.assertIn("Apply any other principle when its trigger fits", universal)
        self.assertIn("all of a role's work", universal)
        self.assertIn("conflicts with a task it runs", universal)
        self.assertNotRegex(universal, r"roles (?:name|select) none")

    def test_engineer_keeps_standing_principles_without_reviewer_briefs(self):
        rules = section(team_texts()["roles/engineer.md"], "Rules")
        for label, name in (
            ("Fix root causes", "fix-root-causes"),
            ("Subtract before you add", "subtract-before-you-add"),
            ("Redesign from first principles", "redesign-from-first-principles"),
        ):
            standing_rule = item(rules, label)
            self.assertIn(f"<team>/principles/{name}.md", standing_rule)
            self.assertIn("read and follow", standing_rule)
            self.assertNotIn("reviewer", standing_rule)

    def test_shared_contract_keeps_outcomes_without_subagent_guidance(self):
        text = team_texts()["principles.md"]
        index = section(text, "Principle index")
        self.assertIn("Resolve upstream principle links through this index", index)
        self.assertIn("Never invoke an unshipped skill or tool", index)
        self.assertIn("existing role and task rules", section(text, "Non-negotiables"))
        self.assertIn("`needs_input`", section(text, "Autonomy"))
        self.assertIn("never a direct question", section(text, "Autonomy"))
        self.assertNotIn("## Subagents", text)
        for name in ("Non-negotiables", "Writing", "Principle index", "Autonomy"):
            self.assertNotRegex(
                section(text, name),
                r"(?i)\b(subagents?|delegat(?:e|es|ing|ion)|fan-out|parallel|workers?)\b",
            )
        self.assertIn("Read each principle's file in full", section(text, "Principle index"))
        for name in (
            "fix-root-causes",
            "redesign-from-first-principles",
            "subtract-before-you-add",
        ):
            self.assertFalse(os.path.exists(os.path.join(TEAM, "methods", f"{name}.md")))
