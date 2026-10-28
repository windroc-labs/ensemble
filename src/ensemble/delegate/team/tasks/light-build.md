# Light Build

Build the requirement with `/updraft:light-build`, then open a pull request.

## Input processing

[Engineer › Input processing](#input-processing), the requirement being the input's text; a PRD, if given, is context for it.

## Steps

1. **Read** the input. The user's requirements outrank the requirement, which outranks a PRD.
2. **Repo** ([Engineer › Repo](#repo)); from status, act on `plan_docs` too.
3. **Which build:**
   - The newest plan doc (`plan_docs`' last) not at `done` → continue it.
   - Else a plan doc or a `pr` (a **finished build**) and a user requirement → a new build of the user's requirements. Review input alone never starts one.
   - Else a finished build → `needs_input` asking what next; stop.
   - Else → build the requirement.
4. **Report progress:**
   [ensemble-progress:start] the build (continue, new or first)
5. **Build**: continuing → [Engineer › Repo › Continue](#repo); else `/updraft:light-build` ([Engineer › Updraft](#updraft)), the requirements being the requirement, a PRD if given and the user's requirements; docs line "The plan doc stays where updraft puts it; S7 copies it to where the repo keeps design docs; never `git add -f` it."; push points: S5 and each fix.
   - **Data shape**: at planning, use [`model-the-domain`](#principle-index) to choose the structure for stateful or branching logic before splitting implementation tasks.
   - **Shared state**: before fan-out, use [`separate-before-serializing-shared-state`](#principle-index) to remove shared write targets; serialize only when one shared target is a real invariant.
   - **Delivery**: use [`sequence-verifiable-units`](#principle-index) to implement, verify and commit each small unit before starting the next.
   - **Report progress** at the end of each build step (`S<i>`) you run and of each S5 task:
     [ensemble-progress:step] the step and its result; an S5 task as `S5 task <k>/<n> done: <commit>; <checks run>`, `<k>` its number, `<n>` the plan doc's task count (its `### Task` headings)
6. **Finish** ([Engineer › Finish](#finish)).

## Resume

[Engineer › Repo › Resume](#repo).
