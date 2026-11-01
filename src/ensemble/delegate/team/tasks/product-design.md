# Product Design

Turn the input into a reviewed PRD.

## Steps

1. **Read** the input. Precedence: the **user's later words** (their messages after the **brief**, the request) > the brief > reports.
2. **Judge.** What to build unclear → `needs_input`, stop; only the audience or depth unclear → self-grill. Decide whether the PRD needs **research** and a **self-grill** (open decisions or rival options, within [PM › Boundaries](#boundaries)).
3. **Report progress:**
   [ensemble-progress:start] whether the PRD needs research and a self-grill
4. **Research**, if needed, only what the PRD needs, on the web per [PM › Boundaries](#boundaries); too deep for now → Risks. The input names a target repo → check it out first ([Principles › Work › Worktree](#work)), `<branch>` `<id>-product-design`, and read the product's code there, read-only; exit 2 or 1 → the error under Risks, go on without the code.
5. **Self-grill**, if needed, by the grilling method `<team>/methods/grilling.md`, at most 3 rounds. Each round, one fresh subagent is the method's questioner: from the brief and the PRD so far, it asks the round's key decisions (ones changing requirements or scope), each with its recommended answer. You are the user: settle each in the PRD; what you can't settle → Open questions. Stop early, before the cap, once the questions are minor details that no longer change requirements or scope.
6. **Write** the PRD per the Template, its Acceptance scenarios included; the outcome's `title` is `[Product name]` alone, unchanged on revision.
7. **Review.** Run `/updraft:review`, `doc=` the PRD's file (none yet → write it to `<Workdir>/tmp/prd.md`; [Output › Destination](#destination) then copies that file), `requirement=` the brief and the user's later words, verbatim, with the documents they name, at its default round cap; tell it the reviewers' focus: missing, contradictory or untestable requirements, scope beyond what the user asked for and over-engineering, asking no more rigor or scope than the brief does. It fixes real blockers until every reviewer passes. `capped` or `needs-author` → the PRD still ships; its open blockers and `needs-author`'s questions go under Open questions, and the outcome's `summary` gives the blockers' count. `stopped` → the PRD ships unreviewed; `summary` gives its reason.
8. **Council.** The questions under Open questions, open review blockers aside: one that changes external state or can't be undone (e.g. making a package public) stays there, for the user. Run `/updraft:council` once on the rest, each with its options, `requirement=` as in step 7; write each decision into the PRD where its question bears and drop the question from Open questions. A question it returns as the author's call stays undecided: once the rest are written, `needs_input`, its `questions` those questions with their options; stop. The user's answers go into the PRD, then step 9.
9. **Finish.** `status: done`.

## Resume

Use this session's history; do only what's left:
- `needs_input` written this session → stop.
- Self-grill rounds answered this session → keep their answers and never rerun them; run only the rounds left.
- PRD written this session → continue it, never rewrite it; then what's left of steps 7–9.
- Review started this session → run `/updraft:review` again with the same `doc`; it continues from its log.
- Council decided this session → keep its decisions; never run it again on those questions.
