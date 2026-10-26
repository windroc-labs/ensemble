// Usage: node - ROOT NAME... (this file on stdin), as the user who loads the plugins.
// Prints "<name> missing|unreadable|misnamed" for each NAME under ROOT that user cannot load, nothing for one it can;
// exits 0 either way, so a non-zero exit means the check itself failed.
"use strict";
const fs = require("fs");
const path = require("path");

// readable: every entry under dir is readable and every directory searchable; symlinked directories are not followed.
function readable(dir) {
  const pending = [dir];
  while (pending.length > 0) {
    const current = pending.pop();
    try {
      const info = fs.lstatSync(current);
      const mode = info.isDirectory() ? fs.constants.R_OK | fs.constants.X_OK : fs.constants.R_OK;
      fs.accessSync(current, mode);
      if (info.isDirectory()) {
        for (const entry of fs.readdirSync(current)) {
          pending.push(path.join(current, entry));
        }
      }
    } catch {
      return false;
    }
  }
  return true;
}

function problem(root, name) {
  const dir = path.join(root, name);
  const manifest = path.join(dir, ".claude-plugin", "plugin.json");
  try {
    if (!fs.statSync(dir).isDirectory() || !fs.statSync(manifest).isFile()) {
      return "missing";
    }
  } catch (error) {
    return error.code === "EACCES" ? "unreadable" : "missing";
  }
  // The directory itself may be a symlink: walk what it points to.
  if (!readable(fs.realpathSync(dir))) {
    return "unreadable";
  }
  let data;
  try {
    data = JSON.parse(fs.readFileSync(manifest, "utf8"));
  } catch {
    return "misnamed";
  }
  const named = data !== null && typeof data === "object" && !Array.isArray(data) && data.name === name;
  return named ? null : "misnamed";
}

const [root, ...names] = process.argv.slice(2);
for (const name of names) {
  const found = problem(root, name);
  if (found) {
    process.stdout.write(`${name} ${found}\n`);
  }
}
