# Product Design

Turn the input into a reviewed PRD.

## Inputs

The **brief** (the request), the **user's later words** (their messages after the brief), reports, and a target repo if the input names one. Precedence: the user's later words > the brief > reports.

## Capabilities

| Step | updraft capability | Use |
|---|---|---|
| 6 | `/updraft:review` | review the PRD with spec-fitness alone |
| 7 | `/updraft:council` | settle Open questions |

## Role limits

- [PM › Boundaries](#boundaries).
- Research and the target repo: step 4; questions left for the user: step 8.

## Steps

1. **Read** the input (Inputs).
2. **Judge.** What to build unclear → `needs_input`, stop. Decide whether the PRD needs **research**.
3. **Report progress:**
   [ensemble-progress:start] whether the PRD needs research
4. **Research**, if needed, only what the PRD needs, on the web per [PM › Boundaries](#boundaries); too deep for now → Risks. The input names a target repo → check it out first ([Principles › Work › Worktree](#work)), `<branch>` `<id>-product-design`, and read the product's code there, read-only; exit 2 or 1 → the error under Risks, go on without the code.
5. **Write** the PRD per the Template, its Acceptance scenarios included; the outcome's `title` is `[Product name]` alone, unchanged on revision.
   - **Sourced**: each requirement comes from the user's words, the existing code, the user's answer to a question or the council's decision (step 7). A point with none of these is no requirement.
   - **Undecided points**: the existing behaviour answers one → follow it, unasked. Else one that changes requirements or scope → Open questions, with a suggested answer. Else decide it yourself.
6. **Review.** Copy updraft's `agents/spec-fitness-reviewer.md` (under the updraft plugin's root, beside its `skills/`) alone into `<Workdir>/tmp/prd-lenses/`, then run `/updraft:review` with `lenses=` that folder, `doc=` the PRD's file (none yet → write it to `<Workdir>/tmp/prd.md`; [Output › Destination](#destination) then copies that file), `requirement=` the brief and the user's later words, verbatim, with the documents they name, at its default round cap; tell it the focus: contradictory or untestable requirements, what the user said that the PRD misses, and requirements with no source, asking no more rigor or scope than the brief does; a blocker whose fix changes requirements or scope is the author's (`needs-author`); it settles every other fork alone, consulting no one. It fixes real blockers until the reviewer passes. `needs-author` → its questions go under Open questions, its open blockers under Risks. `capped` → the PRD still ships; its open blockers go under Risks, and the outcome's `summary` gives the blockers' count. `stopped` → the PRD ships unreviewed; `summary` gives its reason.
7. **Council.** The questions under Open questions: one that changes external state or can't be undone (e.g. making a package public) stays there, for the user. Run `/updraft:council` once on the rest, each with its options, `requirement=` as in step 6; write each decision into the PRD where its question bears and drop the question from Open questions. A question it returns as the author's call stays under Open questions, for the user.
8. **Ask.** The user's answers go into the PRD first, each a requirement's source, and leave Open questions. Then Open questions left → `needs_input`, its `questions` at most 4 of them, those changing requirements or scope most, each with its suggested answer; stop. None left → step 9.
9. **Finish.** Result.

## Result

- `done`: the PRD, per the Template; `title` and `summary` as steps 5–6 set them.
- `needs_input`: what to build unclear (step 2), or Open questions left for the user (step 8).

## Failure

None of its own: steps 4 and 6 say what ships when a checkout or review fails.

## Resume

Use this session's history; do only what's left:
- `needs_input` written this session, with no answer in the input since → stop. Answers to step 2's question → step 2; to step 8's → step 8.
- PRD written this session → continue it, never rewrite it; then what's left of steps 6–9.
- Review started this session → run `/updraft:review` again with the same `doc` and `lenses=`; it continues from its log.
- Council decided this session → keep its decisions; never run it again on those questions.
