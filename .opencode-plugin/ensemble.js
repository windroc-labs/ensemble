// ensemble's OpenCode plugin: each skill of package.json's
// `skills` as the command /ensemble:<skill>, this plugin's bin/ on the shell's PATH, and a commander's
// SessionStart re-arm prompt (src/ensemble/manager/rearm.py --mode manual) in the system prompt
// of a root session's turn after a start, resume or compaction. A session ensemble starts
// (ENSEMBLE_AGENT_SESSION) gets none of it. State lives here, not per plugin call: OpenCode calls a
// plugin again whenever it rebuilds its instance.
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, readlinkSync, symlinkSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

// `root`, or a link to it under ~/.ensemble/links when it holds the PATH delimiter (OpenCode's own cache path,
// .../ensemble@git+https:/..., does), which no PATH or PYTHONPATH entry can hold.
function delimiterFree(root) {
  if (!root.includes(path.delimiter)) return root;
  const links = path.join(homedir(), ".ensemble", "links");
  const link = path.join(links, `opencode-${createHash("sha256").update(root).digest("hex").slice(0, 16)}`);
  try {
    if (readlinkSync(link) === root) return link;
    unlinkSync(link);
  } catch {}
  mkdirSync(links, { recursive: true });
  symlinkSync(root, link);
  return link;
}

const ROOT = delimiterFree(path.dirname(path.dirname(fileURLToPath(import.meta.url))));
const REARM = path.join(ROOT, "src", "ensemble", "manager", "rearm.py");
const REARM_WAIT = 5000; // ms, the SessionStart hooks' timeout

const created = new Set(); // root sessions created in this process
const children = new Set();
let client = null;
const seen = new Set();
const pending = new Map(); // session → its re-arm prompt's promise, until the turn that got it ends
const reached = new Set();

// A SKILL.md's frontmatter fields (top-level `key: value` lines, a quoted value as JSON) and body.
function split(text) {
  const end = text.startsWith("---\n") ? text.indexOf("\n---\n", 3) : -1;
  if (end < 0) return [{}, text];
  const head = {};
  for (const line of text.slice(4, end).split("\n")) {
    const match = /^([\w-]+):\s*(.*)$/.exec(line);
    if (!match) continue;
    try {
      head[match[1]] = match[2].startsWith('"') ? JSON.parse(match[2]) : match[2];
    } catch {
      head[match[1]] = match[2];
    }
  }
  return [head, text.slice(end + 5).replace(/^\n+/, "")];
}

function commands() {
  const { skills } = JSON.parse(readFileSync(path.join(ROOT, "package.json"), "utf8"));
  const found = {};
  for (const dir of skills) {
    const base = path.join(ROOT, dir);
    for (const entry of readdirSync(base).sort()) {
      const file = path.join(base, entry, "SKILL.md");
      if (!existsSync(file)) continue;
      const [head, body] = split(readFileSync(file, "utf8"));
      found[`ensemble:${head.name || entry}`] = {
        description: head.description || "",
        template: body.replaceAll("${CLAUDE_SKILL_DIR}", path.join(base, entry)),
      };
    }
  }
  return found;
}

// rearm.py's additionalContext for a SessionStart of `source`; null when it prints none.
function rearm(source) {
  return new Promise((resolve) => {
    let child;
    let out = "";
    try {
      child = spawn("python3", ["-I", REARM, "--mode", "manual"], { stdio: ["pipe", "pipe", "ignore"] });
    } catch {
      resolve(null);
      return;
    }
    const timer = setTimeout(() => child.kill(), REARM_WAIT);
    child.stdout.on("data", (chunk) => (out += chunk));
    child.on("error", () => {
      clearTimeout(timer);
      resolve(null);
    });
    child.on("close", () => {
      clearTimeout(timer);
      try {
        const text = JSON.parse(out)?.hookSpecificOutput?.additionalContext;
        resolve(typeof text === "string" && text ? text : null);
      } catch {
        resolve(null);
      }
    });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify({ hook_event_name: "SessionStart", source }));
  });
}

function arm(session, source) {
  pending.set(session, rearm(source));
  reached.delete(session);
}

function handle(event) {
  const p = event?.properties ?? {};
  if (event?.type === "session.created" && p.info?.id) {
    (p.info.parentID ? children : created).add(p.info.id);
  } else if (event?.type === "session.compacted" && p.sessionID && !children.has(p.sessionID)) {
    arm(p.sessionID, "compact");
  } else if (event?.type === "session.idle" && reached.has(p.sessionID)) {
    pending.delete(p.sessionID);
    reached.delete(p.sessionID);
  }
}

// A session this process did not create: its saved parent, null when unknown.
async function parent(session) {
  try {
    return (await client?.session?.get({ path: { id: session } }))?.data?.parentID || null;
  } catch {
    return null;
  }
}

async function system(session, output) {
  if (!session || children.has(session)) return;
  if (!seen.has(session)) {
    seen.add(session);
    if (!created.has(session) && (await parent(session))) {
      children.add(session);
      pending.delete(session);
      return;
    }
    if (!pending.has(session)) arm(session, created.has(session) ? "startup" : "resume");
  }
  const text = await pending.get(session);
  if (!pending.has(session)) return;
  reached.add(session);
  if (text) output.system.push(text);
}

export const EnsembleCommander = async (input) => {
  if (process.env.ENSEMBLE_AGENT_SESSION !== undefined) return {};
  client = input?.client ?? client;
  const found = commands();
  return {
    config: async (config) => {
      config.command = { ...config.command, ...found };
    },
    "shell.env": async (_input, output) => {
      const rest = output.env.PATH ?? process.env.PATH;
      output.env.PATH = rest ? `${path.join(ROOT, "bin")}${path.delimiter}${rest}` : path.join(ROOT, "bin");
    },
    event: async ({ event }) => handle(event),
    "experimental.chat.system.transform": async (input, output) => system(input?.sessionID, output),
  };
};
