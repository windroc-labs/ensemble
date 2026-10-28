// A fake `opencode` TUI: loads the plugins its OPENCODE_CONFIG_CONTENT lists, as OpenCode 1.19.4
// does, and plays FAKE_SCENARIO's turns ("turns", or "resume" for a --session start) as OpenCode bus
// events to them. After a turn's idle it saves the turn's user messages again (their summary diffs).
// The first turn's text is --prompt's (a fresh start), the rest stdin lines. A step:
// {text, late?: ms, reads?, child?, repeat?, synthetic?, ignored?} (late: saved after idle;
// reads: hidden for N API reads), {tool, permission?, pattern?},
// {bash, child?} (backend ask emits permission.asked, then --auto replies or stdin "allow" does),
// {question, child?} (asks, then waits for the stdin
// line "answer" or "dismiss"), {error, recover?} (recovered: the turn goes on, as
// after a compaction), {unsaved} (an assistant message never completed), {errored} (one that
// failed), {lateUser: ms} (the whole turn, its user message too, saved ms after its idle),
// {compact: text} (a context overflow's auto compaction: its error, compaction user message,
// summary `text`, then a synthetic continue message, or after an earlier turn the input again),
// {foreign}, {busy}, {reinit}, {nested}. Each start and nested probe is a FAKE_LOG line.
import { spawnSync } from "node:child_process";
import { appendFileSync, readFileSync } from "node:fs";
import { createInterface } from "node:readline";

const NATIVE = "ses_fakeTuiRootSession0001";
const CHILD = "ses_fakeTuiChildSession001";
const FOREIGN = "ses_fakeTuiForeignSession1";

function log(value) {
  appendFileSync(process.env.FAKE_LOG, JSON.stringify(value) + "\n");
}

function options(argv) {
  const found = { session: null, prompt: null, auto: false, model: null };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--session" || arg === "-s") found.session = argv[++i];
    else if (arg.startsWith("--prompt=")) found.prompt = arg.slice("--prompt=".length);
    else if (arg === "--model") found.model = argv[++i];
    else if (arg === "--auto") found.auto = true;
  }
  return found;
}

const argv = process.argv.slice(2);
const opts = options(argv);
const config = JSON.parse(process.env.OPENCODE_CONFIG_CONTENT ?? "{}");
const scene = JSON.parse(readFileSync(process.env.FAKE_SCENARIO, "utf8"));
const native = opts.session ?? NATIVE;
log({
  mode: "tui",
  argv,
  cwd: process.cwd(),
  config,
  env: {
    hook: process.env.ENSEMBLE_OPENCODE_HOOK ?? null,
    session: process.env.ENSEMBLE_OPENCODE_SESSION ?? null,
    pure: process.env.OPENCODE_PURE ?? null,
  },
});

const messages = [];
const hidden = new Map();
const late = []; // [ms after the turn's idle, a message saved then]
let held = null; // a lateUser turn's messages, saved together later
let users = []; // the turn's user messages, saved again after its idle
let prompt = null; // the turn's input text
let earlier = false; // a turn before this one
function save(message) {
  (held ?? messages).push(message);
}
const client = { session: { messages: async () => {
  log({ messages_read: native });
  return { data: messages.filter((m) => {
  const left = hidden.get(m.info.id) ?? 0;
  if (!left) return true;
  hidden.set(m.info.id, left - 1);
  return false;
}).sort((a, b) => Number(a.info.id.slice(4)) - Number(b.info.id.slice(4))) };
} } };
const modules = [];
for (const url of config.plugin ?? []) modules.push(await import(url));
let hooks = [];

async function init() {
  hooks = [];
  for (const module of modules) {
    for (const plugin of Object.values(module)) {
      hooks.push(await plugin({ client, directory: process.cwd(), worktree: process.cwd(), $: undefined }));
    }
  }
}

async function emit(type, properties) {
  for (const found of hooks) await found.event?.({ event: { id: "evt", type, properties } });
}

const lines = [];
const waiting = [];
const input = createInterface({ input: process.stdin, terminal: false });
// A managed worker's guard sends a paste bracketed, as OpenCode reads it.
const unbracket = (line) => line.replaceAll("\x1b[200~", "").replaceAll("\x1b[201~", "");
input.on("line", (raw) => {
  const line = unbracket(raw);
  waiting.length ? waiting.shift()(line) : lines.push(line);
});
input.on("close", () => waiting.splice(0).forEach((resolve) => resolve(null)));

function next() {
  return lines.length ? Promise.resolve(lines.shift()) : new Promise((resolve) => waiting.push(resolve));
}

let part = 0;
async function tool(session, name) {
  const id = `prt_${++part}`;
  for (const status of ["pending", "running", "completed"]) {
    await emit("message.part.updated", {
      sessionID: session,
      part: { id, sessionID: session, type: "tool", tool: name, state: { status } },
      time: Date.now(),
    });
  }
}

async function ask(permission, pattern, session) {
  const rule = config.permission?.[permission] ?? "ask";
  const found = typeof rule === "string" ? rule : (rule[pattern] ?? rule["*"] ?? "ask");
  if (found !== "ask") return;
  await emit("permission.asked", { id: "per_1", sessionID: session, permission, patterns: [pattern] });
  if (!opts.auto) while ((await next()) !== "allow");
  await emit("permission.replied", { sessionID: session, requestID: "per_1", reply: "once" });
}

// A user message and its first part: the input's text, or one OpenCode makes itself.
async function user(fields) {
  const info = { id: `msg_${++part}`, sessionID: native, role: "user", time: { created: Date.now() } };
  const first = { id: `prt_${++part}`, sessionID: native, messageID: info.id, ...fields };
  save({ info, parts: [first] });
  users.push(info);
  await emit("message.updated", { info });
  await emit("message.part.updated", { part: first });
}

async function step(item) {
  if ("text" in item) {
    const session = item.child ? CHILD : native;
    if (item.child) await emit("session.created", { info: { id: CHILD, parentID: native } });
    const text = { id: `prt_${++part}`, sessionID: session, type: "text", text: item.text,
      synthetic: item.synthetic, ignored: item.ignored };
    const message = assistant([text], session);
    if (item.reads) hidden.set(message.info.id, item.reads);
    if (item.late) late.push([item.late, message]);
    else save(message);
    await emit("message.part.updated", { part: text });
    for (let n = 0; n < (item.repeat ?? 1); n++) await emit("message.updated", { info: message.info });
  } else if ("unsaved" in item || "errored" in item) {
    const error = item.errored ? { name: "MessageAbortedError", data: { message: "aborted" } } : undefined;
    save({ info: { id: `msg_${++part}`, sessionID: native, role: "assistant", time: { created: Date.now() }, error }, parts: [] });
  } else if ("tool" in item) {
    if (item.permission) await ask(item.permission, item.pattern, native);
    await tool(native, item.tool);
    save(assistant([{ type: "tool", tool: item.tool }]));
  } else if ("bash" in item) {
    const session = item.child ? CHILD : native;
    if (item.child) await emit("session.created", { info: { id: CHILD, parentID: native } });
    await ask("bash", item.bash, session);
    await tool(session, "bash");
  } else if ("question" in item) {
    const session = item.child ? CHILD : native;
    if (item.child) await emit("session.created", { info: { id: CHILD, parentID: native } });
    const questions = [{ question: "Which one?", header: item.question, options: [{ label: "A", description: "" }] }];
    await emit("question.asked", { id: "que_1", sessionID: session, questions });
    let line;
    while ((line = await next()) !== "answer" && line !== "dismiss");
    if (line === "answer") await emit("question.replied", { sessionID: session, requestID: "que_1", answers: [["A"]] });
    else await emit("question.rejected", { sessionID: session, requestID: "que_1" });
    await tool(session, "question");
  } else if ("error" in item) {
    await emit("session.error", { sessionID: native, error: item.error });
    if (!item.recover) {
      // halt's idle; turn() then sends the run's own (processor.ts halt, runner.ts finishRun)
      await emit("session.idle", { sessionID: native });
      return true;
    }
    await emit("session.status", { sessionID: native, status: { type: "busy" } });
  } else if ("compact" in item) {
    // processor.ts halt on an overflow, then compaction.ts: the run goes on
    const error = { name: "ContextOverflowError", data: { message: "context overflow" } };
    await emit("session.error", { sessionID: native, error });
    const overflowed = assistant([]);
    save(overflowed);
    await emit("message.updated", { info: overflowed.info });
    await user({ type: "compaction", auto: true, overflow: true });
    await emit("session.status", { sessionID: native, status: { type: "busy" } });
    const text = { id: `prt_${++part}`, sessionID: native, type: "text", text: item.compact };
    const summary = assistant([text]);
    Object.assign(summary.info, { summary: true, mode: "compaction" });
    save(summary);
    await emit("message.part.updated", { part: text });
    await emit("message.updated", { info: summary.info });
    if (earlier) await user({ type: "text", text: prompt });
    else await user({ type: "text", text: "Continue.", synthetic: true, metadata: { compaction_continue: true } });
    await emit("session.status", { sessionID: native, status: { type: "busy" } });
  } else if ("foreign" in item) {
    await emit("session.created", { info: { id: FOREIGN } });
    await emit("session.status", { sessionID: FOREIGN, status: { type: "busy" } });
    await tool(FOREIGN, "read");
    await emit("permission.asked", { id: "per_2", sessionID: FOREIGN, permission: "bash", patterns: [] });
    await emit("question.asked", { id: "que_2", sessionID: FOREIGN, questions: [{ question: "?", header: "Foreign", options: [] }] });
    await emit("session.idle", { sessionID: FOREIGN });
  } else if ("busy" in item) {
    await emit("session.status", { sessionID: native, status: { type: "busy" } });
  } else if ("reinit" in item) {
    await init();
  } else if ("nested" in item) {
    const probe = "process.stdout.write(JSON.stringify(['ENSEMBLE_OPENCODE_HOOK', 'ENSEMBLE_OPENCODE_SESSION'].map((k) => k in process.env)))";
    const child = spawnSync(process.execPath, ["-e", probe], { env: process.env, encoding: "utf8" });
    log({ nested: JSON.parse(child.stdout) });
  }
  return false;
}

function assistant(parts, session = native) {
  const now = Date.now();
  return { info: { id: `msg_${++part}`, sessionID: session, role: "assistant", time: { created: now, completed: now } }, parts };
}

async function turn(text, steps, first) {
  if (first && !opts.session) await emit("session.created", { info: { id: native } });
  const lateUser = steps.find((item) => "lateUser" in item)?.lateUser;
  held = lateUser ? [] : null;
  users = [];
  prompt = text;
  await user({ type: "text", text });
  await emit("session.status", { sessionID: native, status: { type: "busy" } });
  for (const item of steps) if (await step(item)) break;
  await emit("session.status", { sessionID: native, status: { type: "idle" } });
  await emit("session.idle", { sessionID: native });
  for (const info of users) await emit("message.updated", { info: { ...info, summary: { diffs: [] } } });
  earlier = true;
  for (const [ms, message] of late.splice(0)) setTimeout(() => messages.push(message), ms);
  if (held) {
    const turnMessages = held;
    held = null;
    setTimeout(() => messages.push(...turnMessages), lateUser);
  }
}

await init();
const turns = (opts.session && scene.resume) || scene.turns || [[{ text: "first" }]];
let index = 0;
let first = true;
if (opts.prompt !== null && !opts.session) {
  await turn(opts.prompt, turns[index++] ?? [], first);
  first = false;
}
for (let line = await next(); line !== null; line = await next()) {
  await turn(line, turns[index++] ?? [], first);
  first = false;
}
