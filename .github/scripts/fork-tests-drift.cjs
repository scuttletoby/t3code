// Fork only: fails when upstream's ci.yml or release.yml test jobs change since
// the copy in .github/workflows/fork-tests.yml last followed them. Compares a
// digest of every job and step, and of the top-level keys that change how every
// job runs (TOP_LEVEL), with .github/fork-tests-upstream.json; comments are ignored.
//
// Limit: the check compares upstream with its own record, not with
// fork-tests.yml. Recording without porting passes, so --record prints what
// changed, and the commit and PR that update the record must say, for each line,
// what was ported or why it does not apply.
//   node .github/scripts/fork-tests-drift.cjs           check
//   node .github/scripts/fork-tests-drift.cjs --record  after porting: print the changes, save the record
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const recordPath = path.join(root, ".github/fork-tests-upstream.json");

// Upstream file -> jobs the fork copies (null = every job in the file).
const TRACKED = {
  ".github/workflows/ci.yml": null,
  ".github/workflows/release.yml": ["quality", "test", "test_web", "test_server"],
};

// Top-level keys digested as the pseudo-job TOP (job ids cannot contain parentheses).
const TOP_LEVEL = ["env", "defaults", "concurrency", "permissions"];
const TOP = "(top-level)";

const digest = (lines) =>
  crypto.createHash("sha256").update(lines.join("\n")).digest("hex").slice(0, 16);

const indentOf = (line) => line.length - line.trimStart().length;

function parseJobs(text) {
  // Drop blank lines, full-line comments and trailing spaces.
  const lines = text
    .split("\n")
    .map((line) => line.replace(/\s+$/, ""))
    .filter((line) => line.trim() !== "" && !line.trim().startsWith("#"));
  const start = lines.findIndex((line) => line === "jobs:");
  if (start === -1) throw new Error("no top-level `jobs:` key");

  // Top-level keys, wherever they are in the file: one "step" per key present.
  const top = [];
  let topKey = null;
  for (const line of lines) {
    if (indentOf(line) === 0) {
      const key = line.match(/^([A-Za-z0-9_-]+):/)?.[1];
      topKey = TOP_LEVEL.includes(key) ? { name: key, lines: [] } : null;
      if (topKey) top.push(topKey);
    }
    topKey?.lines.push(line);
  }

  const jobs = {};
  let job = null;
  let inSteps = false;
  let stepsIndent = 0;
  let step = null;
  for (const line of lines.slice(start + 1)) {
    if (indentOf(line) === 0) break;
    const jobKey = line.match(/^ {2}([A-Za-z0-9_-]+):$/);
    if (jobKey) {
      job = jobs[jobKey[1]] = { header: [], steps: [] };
      inSteps = false;
      step = null;
      continue;
    }
    if (!job) continue;
    if (inSteps && indentOf(line) <= stepsIndent && !line.trimStart().startsWith("- ")) {
      inSteps = false;
      step = null;
    }
    if (!inSteps && /^\s+steps:$/.test(line)) {
      inSteps = true;
      stepsIndent = indentOf(line);
      continue;
    }
    if (!inSteps) {
      job.header.push(line);
      continue;
    }
    if (indentOf(line) <= stepsIndent + 2 && line.trimStart().startsWith("- ")) {
      step = { lines: [] };
      job.steps.push(step);
    }
    step?.lines.push(line);
  }

  const result = {};
  if (top.length)
    result[TOP] = {
      header: digest([]),
      steps: top.map((key) => ({ name: key.name, digest: digest(key.lines) })),
    };
  for (const [name, { header, steps }] of Object.entries(jobs)) {
    const seen = new Map();
    result[name] = {
      header: digest(header),
      steps: steps.map((s) => {
        const named = s.lines
          .map((line) => line.trim().replace(/^- /, ""))
          .map((line) => line.match(/^(?:name|uses):\s*(.+)$/))
          .find(Boolean);
        let label = named ? named[1].replace(/^["']|["']$/g, "") : "(unnamed step)";
        // Repeated labels get a counter so each step keeps one identity.
        const count = (seen.get(label) ?? 0) + 1;
        seen.set(label, count);
        if (count > 1) label = `${label} #${count}`;
        return { name: label, digest: digest(s.lines) };
      }),
    };
  }
  return result;
}

// Returns the digests and the tracked jobs upstream no longer has.
function current() {
  const out = {};
  const missing = [];
  for (const [file, only] of Object.entries(TRACKED)) {
    const jobs = parseJobs(fs.readFileSync(path.join(root, file), "utf8"));
    if (only) {
      for (const name of only) if (!jobs[name]) missing.push(`${file}: job \`${name}\``);
      out[file] = Object.fromEntries(
        [TOP, ...only].filter((name) => jobs[name]).map((name) => [name, jobs[name]]),
      );
    } else {
      out[file] = jobs;
    }
  }
  return { now: out, missing };
}

function compare(recorded, now) {
  const changes = [];
  for (const file of new Set([...Object.keys(recorded), ...Object.keys(now)])) {
    const before = recorded[file] ?? {};
    const after = now[file] ?? {};
    for (const job of new Set([...Object.keys(before), ...Object.keys(after)])) {
      // A record saved before --record refused missing jobs may hold null.
      const a = before[job] ?? null;
      const b = after[job] ?? null;
      if (job === TOP) {
        const oldKeys = new Map((a?.steps ?? []).map((s) => [s.name, s.digest]));
        const newKeys = new Map((b?.steps ?? []).map((s) => [s.name, s.digest]));
        for (const [key, d] of newKeys) {
          if (!oldKeys.has(key)) changes.push(`${file}: top-level \`${key}:\` added`);
          else if (oldKeys.get(key) !== d) changes.push(`${file}: top-level \`${key}:\` changed`);
        }
        for (const key of oldKeys.keys())
          if (!newKeys.has(key)) changes.push(`${file}: top-level \`${key}:\` removed`);
        continue;
      }
      if (!a && !b) {
        changes.push(`${file}: job \`${job}\` missing upstream (renamed or removed?)`);
        continue;
      }
      if (!a) {
        changes.push(`${file}: job \`${job}\` added upstream`);
        continue;
      }
      if (!b) {
        changes.push(`${file}: job \`${job}\` removed upstream`);
        continue;
      }
      if (a.header !== b.header)
        changes.push(
          `${file}: job \`${job}\`: settings changed (runs-on, if, needs, matrix, env...)`,
        );
      const oldSteps = new Map(a.steps.map((s) => [s.name, s.digest]));
      const newSteps = new Map(b.steps.map((s) => [s.name, s.digest]));
      for (const [name, d] of newSteps) {
        if (!oldSteps.has(name)) changes.push(`${file}: job \`${job}\`: step "${name}" added`);
        else if (oldSteps.get(name) !== d)
          changes.push(`${file}: job \`${job}\`: step "${name}" changed`);
      }
      for (const name of oldSteps.keys())
        if (!newSteps.has(name)) changes.push(`${file}: job \`${job}\`: step "${name}" removed`);
      const oldOrder = a.steps.map((s) => s.name).filter((n) => newSteps.has(n));
      const newOrder = b.steps.map((s) => s.name).filter((n) => oldSteps.has(n));
      if (oldOrder.join("\0") !== newOrder.join("\0"))
        changes.push(`${file}: job \`${job}\`: steps reordered`);
    }
  }
  return changes;
}

function readRecord(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return {};
    throw error;
  }
}

// Refuses while a tracked job is missing: the copy in fork-tests.yml then follows
// a job upstream no longer has, and TRACKED must be updated first.
function record(now, missing, file = recordPath) {
  if (missing.length) {
    console.error(
      `Not recorded: tracked jobs missing upstream: ${missing.join(", ")}.\n` +
        "Find what replaced them, port that into fork-tests.yml and update TRACKED in " +
        "fork-tests-drift.cjs, then record again.",
    );
    process.exitCode = 1;
    return;
  }
  const changes = compare(readRecord(file), now);
  fs.writeFileSync(file, `${JSON.stringify(now, null, 2)}\n`);
  console.log(`Recorded ${path.relative(root, file)}.`);
  if (changes.length === 0) {
    console.log("Nothing changed upstream since the last record.");
    return;
  }
  console.log(
    `\nUpstream changes this record accepts (${changes.length}):\n` +
      changes.map((c) => `- ${c}`).join("\n") +
      "\n\nSay in the commit message and the PR body, for each line, what was ported " +
      "into fork-tests.yml or why it does not apply. The check cannot tell; reviewers rely on it.",
  );
}

function main() {
  const { now, missing } = current();
  if (process.argv.includes("--record")) return record(now, missing);
  const recorded = JSON.parse(fs.readFileSync(recordPath, "utf8"));
  const changes = compare(recorded, now);
  const summary = process.env.GITHUB_STEP_SUMMARY;
  if (changes.length === 0) {
    const msg = "Upstream test jobs match the copy in fork-tests.yml (recorded digest).";
    console.log(msg);
    if (summary) fs.appendFileSync(summary, `### Upstream drift\n\n${msg}\n`);
    return;
  }
  const report =
    "### Upstream drift\n\n" +
    "Upstream's test jobs changed since `.github/workflows/fork-tests.yml` last followed them:\n\n" +
    changes.map((c) => `- ${c}`).join("\n") +
    "\n\nPort the change into fork-tests.yml (or decide it does not apply and say why in the PR), then run " +
    "`node .github/scripts/fork-tests-drift.cjs --record` and commit `.github/fork-tests-upstream.json`.\n";
  console.error(report);
  if (summary) fs.appendFileSync(summary, report);
  for (const c of changes) console.log(`::error title=Upstream drift::${c}`);
  process.exitCode = 1;
}

module.exports = { parseJobs, compare, record, TOP };

if (require.main === module) main();
