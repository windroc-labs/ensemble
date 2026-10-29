# Output

Your result is an **outcome**, returned per [Output › Return](#return), plus a **deliverable** (the document itself). The outcome's fields:

- `status`: `done` | `needs_input` | `failed`.
- `title`: one line.
- `summary`: 1–2 short lines (≤ 300 characters); every detail goes in the deliverable.
- `questions`: `needs_input` only, 1–4.
- `url`: the delivered link, when [Output › Destination](#destination) gives one.
- `files`: absolute paths, when your task asks for them.
- `updraft`: `{"status", "head"}`, when your task asks for it.
- `deliverable`: the document, when [Output › Destination](#destination) says so.
- `deliverable_file`: a `.md` file in `<Workdir>` holding the deliverable, in place of `deliverable`, when [Output › Destination](#destination) says so.

Statuses:

- **done** → deliver the deliverable per [Output › Destination](#destination), nowhere else: the outcome carries it only when that section asks for it.
- **needs_input** (**too vague**): the input lacks a clear question, scope or deliverable, or anything else your task requires; or `covered` related work ([Principles › Overlap](#overlap)). The deliverable may be empty.
- **failed**: nothing usable; `summary` says what failed.

**Write once**: write the deliverable's text once; a later step that needs it elsewhere copies that file (`cp`); only a Destination asking for it inline in the outcome repeats it.

The input gives an earlier version → revise it, keeping what still holds.
