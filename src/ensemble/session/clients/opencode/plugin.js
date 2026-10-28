// ensemble's OpenCode plugin: turns the root session's events into calls of the tui hook command
// (ENSEMBLE_OPENCODE_HOOK, a JSON argv; the label last, a JSON payload on stdin), one at a time.
// The root is the resumed session (ENSEMBLE_OPENCODE_SESSION) or the first session created without
// a parent; its children's permission asks and questions count too. State lives here, not per plugin call:
// OpenCode calls a plugin again whenever it rebuilds its instance. A turn is the root's input: a new user
// message, not one OpenCode's compaction makes, up to idle; OpenCode saves user messages again later.
import { spawn } from "node:child_process";

const hook = parse(process.env.ENSEMBLE_OPENCODE_HOOK);
let root = process.env.ENSEMBLE_OPENCODE_SESSION || null;
// Shell calls inherit process.env: a nested opencode must not find the hook.
delete process.env.ENSEMBLE_OPENCODE_HOOK;
delete process.env.ENSEMBLE_OPENCODE_SESSION;

const children = new Set();
const seen = new Set();
const TOOL = new Set(["running", "completed", "error"]);
const REPLY_WAIT = 5000; // ms to wait for completed messages to be saved
const REPLY_POLL = 200;
let turnUser = null; // the root's latest input message id
const users = new Map(); // root user message id → whether its first part was seen
let reported = new Set(); // text messages delivered in this user turn; busy steps do not reset it
let completed = new Set(); // completion events can precede the corresponding API records
let busy = false;
let failed = false;
let error = null; // the root's session.error, the turn's failure unless a busy shows it recovered
let started = false;
let chain = Promise.resolve();

function parse(text) {
  try {
    const argv = JSON.parse(text ?? "");
    const ok = Array.isArray(argv) && argv.length > 0 && argv.every((a) => typeof a === "string");
    return ok ? argv : null;
  } catch {
    return null;
  }
}

function run(label, payload) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(hook[0], [...hook.slice(1), label], { stdio: ["pipe", "ignore", "ignore"] });
    } catch {
      resolve();
      return;
    }
    child.on("error", resolve);
    child.on("close", resolve);
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(payload));
  });
}

function call(label, fields = {}) {
  const payload = { session_id: root, turn_id: turnUser, ...fields };
  chain = chain.then(() => run(label, payload));
}

// Compaction's own user messages: its request and its synthetic "continue".
function made(part) {
  return part?.type === "compaction" || (part?.type === "text" && part.synthetic === true && part.metadata?.compaction_continue === true);
}

function input(message) {
  return message?.info?.role === "user" && !(message.parts ?? []).some(made);
}

function texts(message) {
  return (message?.parts ?? [])
    .filter((p) => p?.type === "text" && typeof p.text === "string" && !p.synthetic && !p.ignored)
    .map((p) => p.text)
    .join("\n");
}

// The turn's messages: those after its input message (unknown: after the last one), up to the
// next input; null while that message is not saved yet.
function slice(list, user) {
  const start = user ? list.findIndex((m) => m?.info?.id === user) : list.findLastIndex(input);
  if (user && start < 0) return null;
  const rest = list.slice(start + 1);
  const end = rest.findIndex(input);
  return end < 0 ? rest : rest.slice(0, end);
}

// Deliver completed main-thread messages before idle, including saves found by idle's poll.
// The queue captures the session, user and dedupe set so a later turn cannot overtake this one.
function report(client, ending) {
  const session = root, user = turnUser, sent = reported, known = completed;
  chain = chain.then(async () => {
    const last = await reply(client, session, user, sent, known, ending);
    if (ending) await run("session.idle", { session_id: session, turn_id: user, last_assistant_message: last });
  });
}

// Idle can precede the final save. Until REPLY_WAIT, keep its existing newest-text fallback.
async function reply(client, session, user, sent, known, ending) {
  const until = Date.now() + REPLY_WAIT;
  let best = "";
  for (;;) {
    try {
      const found = await client.session.messages({ path: { id: session } });
      const list = Array.isArray(found) ? found : found?.data;
      const turn = Array.isArray(list) ? slice(list, user) : null;
      const answers = (turn ?? []).filter((m) => m?.info?.sessionID === session && m.info.role === "assistant" && !m.info.summary);
      best = answers.map(texts).findLast((t) => t.trim()) ?? best;
      const last = answers.at(-1);
      const saved = typeof last?.info?.time?.completed === "number" && texts(last).trim();
      const ready = [...known].every((id) => answers.some((m) => m.info.id === id &&
        (typeof m.info.time?.completed === "number" || m.info.error != null)));
      // Wait for missing earlier messages too, so a saved final cannot overtake them.
      if (ready || Date.now() >= until) {
        for (const message of answers) {
          const id = message.info.id, text = texts(message);
          if (!id || sent.has(id) || typeof message.info.time?.completed !== "number" || !text.trim()) continue;
          sent.add(id);
          await run("message.completed", { session_id: session, turn_id: user, text });
        }
        if (!ending || (turn && last && (saved || last.info.error != null))) return best;
      }
    } catch {} // a failed read is one more try, not the reply
    if (Date.now() >= until) return best;
    await new Promise((resolve) => setTimeout(resolve, REPLY_POLL));
  }
}

function begin(id) {
  turnUser = id;
  reported = new Set();
  completed = new Set();
  failed = false;
  error = null;
  call("turn.started", { turn_id: id });
}

function owned(session) {
  return session != null && (session === root || children.has(session));
}

function handle(event, client) {
  const p = event?.properties ?? {};
  switch (event?.type) {
    case "session.created":
    case "session.updated": {
      const info = p.info ?? {};
      if (event.type === "session.created" && root === null && !info.parentID && info.id) {
        root = info.id;
      } else if (info.parentID && owned(info.parentID) && info.id) {
        children.add(info.id);
      }
      return;
    }
    case "session.status":
      // Busy comes at every step; only an accepted user message starts another turn.
      if (p.sessionID !== root || p.status?.type !== "busy") return;
      seen.clear();
      error = null;
      if (!busy) call("session.busy");
      busy = true;
      return;
    case "session.idle": {
      if (p.sessionID !== root) return;
      busy = false;
      // A fatal error's turn idles twice (its halt, then its run's end); only new input clears it.
      if (failed) return;
      if (error) {
        failed = true;
        call("session.error", error);
        return;
      }
      report(client, true);
      return;
    }
    case "session.error":
      if (p.sessionID !== root || root === null) return;
      error = { error: p.error?.name || "unknown", message: p.error?.data?.message || "" };
      return;
    case "permission.asked":
      if (!owned(p.sessionID)) return;
      call("permission.asked", { permission: p.permission ?? "", patterns: p.patterns ?? [] });
      return;
    case "permission.replied":
      if (!owned(p.sessionID)) return;
      call("permission.replied", { reply: p.reply ?? "" });
      return;
    case "question.asked": {
      if (!owned(p.sessionID)) return;
      const titles = (Array.isArray(p.questions) ? p.questions : []).map((q) => q?.header || q?.question || "");
      call("question.asked", { questions: titles.filter((t) => typeof t === "string") });
      return;
    }
    case "question.replied":
    case "question.rejected":
      if (!owned(p.sessionID)) return;
      call(event.type);
      return;
    case "message.updated": {
      const info = p.info ?? {};
      if (info.sessionID !== root) return;
      if (info.role === "user" && info.id && !users.has(info.id)) {
        users.set(info.id, false);
      } else if (info.role === "assistant" && info.id && typeof info.time?.completed === "number" && !info.summary) {
        completed.add(info.id);
        report(client, false);
      }
      return;
    }
    case "message.part.updated": {
      const part = p.part ?? {};
      const status = part.state?.status;
      if ((p.sessionID ?? part.sessionID) !== root) return;
      if (users.get(part.messageID) === false) {
        users.set(part.messageID, true);
        if (!made(part)) begin(part.messageID);
        return;
      }
      if (part.type !== "tool" || !TOOL.has(status)) return;
      const key = `${part.id}:${status}`;
      if (seen.has(key)) return;
      seen.add(key);
      call("tool", { tool_name: part.tool ?? "" });
      return;
    }
  }
}

export const Ensemble = async ({ client }) => {
  if (hook === null) return {};
  if (!started && root !== null) call("plugin.start");
  started = true;
  return { event: async ({ event }) => handle(event, client) };
};
