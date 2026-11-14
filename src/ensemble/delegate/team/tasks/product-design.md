# Product Design

Turn the input into a reviewed PRD.

## Inputs

The **brief** (the request), the **user's later words** (their messages after the brief), reports, and a target repo if the input names one. Precedence: the user's later words > the brief > reports.

## Capabilities

| Step | updraft capability | Use |
|---|---|---|
| 6 | `/updraft:review` | review the PRD |
| 7 | `/updraft:council` | settle open questions |

## Role limits

- [PM › Boundaries](#boundaries).
- Research and the target repo: step 4; questions left for the user: step 7.

## Steps

1. **Read** the input (Inputs).
2. **Judge.** What to build unclear → `needs_input`, stop; only the audience or depth unclear → an Open question with its suggested answer. Decide whether the PRD needs **research**.
3. **Report progress:**
   [ensemble-progress:start] whether the PRD needs research
4. **Research**, if needed, only what the PRD needs, on the web per [PM › Boundaries](#boundaries); too deep for now → Risks. The input names a target repo → check it out first ([Principles › Work › Worktree](#work)), `<branch>` `<id>-product-design`, and read the product's code there, read-only; exit 2 or 1 → the error under Risks, go on without the code.
5. **Write** the PRD per the Template, its Acceptance scenarios included; the outcome's `title` is `[Product name]` alone, unchanged on revision.
6. **Review.** Run `/updraft:review`, `doc=` the PRD's file (none yet → write it to `<Workdir>/tmp/prd.md`; [Output › Destination](#destination) then copies that file), `requirement=` the brief and the user's later words, verbatim, with the documents they name, at its default round cap; tell it the reviewers' focus: missing, contradictory or untestable requirements, scope beyond what the user asked for and over-engineering, asking no more rigor or scope than the brief does. It fixes real blockers until every reviewer passes. `capped` or `needs-author` → the PRD still ships; its open blockers and `needs-author`'s questions go under Open questions, and the outcome's `summary` gives the blockers' count. `stopped` → the PRD ships unreviewed; `summary` gives its reason.
7. **Council.** The questions under Open questions, open review blockers aside: one that changes external state or can't be undone (e.g. making a package public) stays there, for the user. Run `/updraft:council` once on the rest, each with its options, `requirement=` as in step 6; write each decision into the PRD where its question bears, and under Approach and trade-offs, per the Template; drop the question from Open questions. A question it returns as the author's call stays undecided: once the rest are written, `needs_input`, its `questions` those questions with their options; stop. The user's answers go into the PRD, then step 8.
8. **Finish.** Result.

## Result

- `done`: the PRD, per the Template; `title` and `summary` as steps 5–6 set them.
- `needs_input`: what to build unclear (step 2), or a council question left as the author's call (step 7).

## Failure

None of its own: steps 4 and 6 say what ships when a checkout or review fails.

## Resume

Use this session's history; do only what's left:
- `needs_input` written this session → stop.
- PRD written this session → continue it, never rewrite it; then what's left of steps 6–8.
- Review started this session → run `/updraft:review` again with the same `doc`; it continues from its log.
- Council decided this session → keep its decisions; never run it again on those questions.
