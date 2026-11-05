# Build

Build the PRD with `/updraft:build`, then open a pull request.

## Inputs

[Engineer › Input processing](#input-processing), the requirement being the PRD (a path or its text). Precedence: the user's requirements > the PRD.

## Capabilities

| Step | updraft capability | Use |
|---|---|---|
| 5 | `/updraft:build` | spec, plan, implementation, verification, review |
| 5 | `model-the-domain` principle | data shape at planning |
| 5 | `separate-before-serializing-shared-state` principle | concurrent writes |
| 5 | `sequence-verifiable-units` principle | delivery, one verified unit at a time |

## Role limits

- [Engineer › Rules](#rules): **No mutation testing**, **Scenarios are acceptance**, push only this run's branch.
- Local checks only lint and unit tests; the PR stays a draft ([Engineer › Updraft](#updraft), [Engineer › Repo › Draft](#repo)).

## Steps

1. **Read** the input and the PRD (Inputs).
2. **Repo** ([Engineer › Repo](#repo)); from status, act on `plan_docs` too.
3. **Which build:**
   - The newest plan doc (`plan_docs`' last) not at `done` → continue it.
   - Else a plan doc or a `pr` (a **finished build**) and a user requirement → a new build, new plan doc. Review input alone never starts one.
   - Else a finished build → `needs_input` asking what next; stop.
   - Else → a first build of the PRD.
4. **Report progress:**
   [ensemble-progress:start] the build (continue `<plan doc>`, new or first)
5. **Build**: continuing → [Engineer › Repo › Continue](#repo); else `/updraft:build` ([Engineer › Updraft](#updraft)), the requirements being the PRD and the user's requirements; docs line "Spec and plan doc stay where updraft puts them; S7 copies them to where the repo keeps design docs; never `git add -f` them."; push points: each task and review round.
   - **Data shape**: at planning, use [`model-the-domain`](#principle-index) to choose the structure for stateful or branching logic before splitting implementation tasks.
   - **Shared state**: when designing concurrent writes, use [`separate-before-serializing-shared-state`](#principle-index) to remove shared write targets; serialize only when one shared target is a real invariant.
   - **Delivery**: use [`sequence-verifiable-units`](#principle-index) to implement, verify and commit each small unit before starting the next.
   - **Report progress** at the end of each build step (`S<i>`) you run and of each S5 task:
     [ensemble-progress:step] the step and its result; an S5 task as `S5 task <k>/<n> done: <commit>; <checks run>`, `<k>` its number, `<n>` the plan doc's task count (its `### Task` headings)
6. **Finish**: Result.

## Result

[Engineer › Finish](#finish), adding `files`: this build's spec (the `-spec.md` beside its `-plan.md` plan doc) and plan doc (on failure, those that exist); the PR description names the PRD (path or title).

- `done`: the build converged and the PR's checks pass or none run; deliverable the PR description; `title`, `summary`, `url` the PR, `updraft`, `files`.
- `needs_input`: step 3's finished build with no user requirement, or a stop in [Engineer › Repo](#repo) or [Output › Destination](#destination).

## Failure

[Engineer › Finish › Failure](#finish), with Result's `files`: the build stopped or capped, an action denied, or the PR's checks still failing or pending at Done's limits. Also `failed` at [Engineer › Repo](#repo) step 1 (checkout exit 1, no push permission) and at a push [Output › Destination](#destination) denies.

## Resume

[Engineer › Repo › Resume](#repo).
