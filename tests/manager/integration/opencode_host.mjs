// Plays steps through .opencode-plugin/ensemble.js as OpenCode 1.19.3 calls a plugin: argv[2] the
// plugin's path, stdin a JSON list of steps; prints a JSON list of one result per step. The client's
// session.get answers from the saved sessions a `saved` step adds; the plugin's directory is the cwd.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const steps = JSON.parse(readFileSync(0, "utf8"));
const saved = {};
const client = { session: { get: async ({ path }) => ({ data: saved[path.id] ?? { id: path.id } }) } };
const hooks = await (await import(pathToFileURL(process.argv[2]).href)).EnsembleCommander({
  client,
  directory: process.cwd(),
});
const results = [];
for (const step of steps) {
  if (step.saved) {
    Object.assign(saved, step.saved);
    results.push(null);
  } else if (step.hooks) {
    results.push(Object.keys(hooks).sort());
  } else if (step.config) {
    await hooks.config(step.config);
    results.push(step.config);
  } else if (step.env) {
    const output = { env: step.env };
    await hooks["shell.env"]({ cwd: "/" }, output);
    results.push(output.env);
  } else if (step.event) {
    await hooks.event({ event: step.event });
    results.push(null);
  } else if (step.system) {
    const output = { system: ["header"] };
    await hooks["experimental.chat.system.transform"]({ sessionID: step.system, model: {} }, output);
    results.push(output.system.slice(1));
  }
}
console.log(JSON.stringify(results));
