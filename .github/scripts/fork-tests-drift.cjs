// Fork only: fails when a GROUP's tracked upstream workflows (or their top-level keys) changed since
// its record (comments ignored). `--record` saves the changes; its commit and PR say per change what was
// ported or why not. `node <this> [--windows] [--record]`: tests (fork-tests.yml, the merge path) or
// windows (fork-windows-tests.yml's own nightly job, so upstream's Windows lane never reaches the merge path).
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");

// tracked: upstream file -> jobs the fork copies (null = every job). topLevel: the keys digested as TOP.
// sameSteps: the copy's job keeps upstream's step names and pinned actions, in order.
const GROUPS = {
  tests: {
    record: ".github/fork-tests-upstream.json",
    copy: "fork-tests.yml",
    tracked: {
      ".github/workflows/ci.yml": null,
      ".github/workflows/release.yml": ["quality", "test", "test_web", "test_server"],
    },
    topLevel: ["env", "defaults", "concurrency", "permissions"],
  },
  // The copy replaces `on:` on purpose, but upstream's inputs say what its Test step runs: tracked too.
  windows: {
    record: ".github/fork-windows-tests-upstream.json",
    copy: "fork-windows-tests.yml",
    tracked: { ".github/workflows/windows-tests.yml": null },
    topLevel: ["on", "env", "defaults", "concurrency", "permissions"],
    sameSteps: { file: ".github/workflows/windows-tests.yml", job: "test", copyJob: "test" },
  },
};

// Top-level keys are digested as the pseudo-job TOP (job ids cannot contain parentheses).
const TOP = "(top-level)";

const digest = (lines) =>
  crypto.createHash("sha256").update(lines.join("\n")).digest("hex").slice(0, 16);

const indentOf = (line) => line.length - line.trimStart().length;

// Splits a workflow into its top-level keys (topLevel) and its jobs (header lines and raw step lines).
function splitJobs(text, topLevel = GROUPS.tests.topLevel) {
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
      topKey = topLevel.includes(key) ? { name: key, lines: [] } : null;
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

  return { top, jobs };
}

function parseJobs(text, topLevel) {
  const { top, jobs } = splitJobs(text, topLevel);
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

// Returns the digests, the tracked jobs upstream no longer has and the tracked files it removed.
function current(group = GROUPS.tests, base = root) {
  const out = {};
  const missing = [];
  const removed = [];
  for (const [file, only] of Object.entries(group.tracked)) {
    const full = path.join(base, file);
    if (!fs.existsSync(full)) {
      removed.push(file);
      continue;
    }
    const jobs = parseJobs(fs.readFileSync(full, "utf8"), group.topLevel);
    if (only) {
      for (const name of only) if (!jobs[name]) missing.push(`${file}: job \`${name}\``);
      out[file] = Object.fromEntries(
        [TOP, ...only].filter((name) => jobs[name]).map((name) => [name, jobs[name]]),
      );
    } else {
      out[file] = jobs;
    }
  }
  return { now: out, missing, removed };
}

const removedLine = (file, copy) =>
  `${file}: removed upstream: port what replaced it into ${copy} or drop the copy, ` +
  "then update GROUPS in fork-tests-drift.cjs";

// A job's steps as [name, uses] pairs, read from each step's own keys (not its with: block).
function stepsOf(text, job) {
  return (splitJobs(text).jobs[job]?.steps ?? []).map(({ lines }) => {
    const own = indentOf(lines[0]) + 2;
    const keys = {};
    for (const [i, line] of lines.entries()) {
      const flat = i === 0 ? line.replace(/^(\s*)- /, "$1  ") : line;
      const m = indentOf(flat) === own && flat.trim().match(/^(name|uses):\s*(.+)$/);
      if (m && !(m[1] in keys))
        keys[m[1]] = m[2].replace(/\s+#.*$/, "").replace(/^(["'])(.*)\1$/, "$2");
    }
    return [keys.name ?? null, keys.uses ?? null];
  });
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

// Refuses while a tracked job or file is missing: the fork's copy then follows
// something upstream no longer has, and GROUPS must be updated first.
function record(now, missing, file, copy = GROUPS.tests.copy) {
  if (missing.length) {
    console.error(
      `Not recorded: tracked jobs missing upstream: ${missing.join(", ")}.\n` +
        `Find what replaced them, port that into ${copy} and update GROUPS in ` +
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
      `into ${copy} or why it does not apply. The check cannot tell; reviewers rely on it.`,
  );
}

// What the check reports for a group: removed files, changes since the record, and steps that differ.
function drift(group = GROUPS.tests, base = root) {
  const { now, removed } = current(group, base);
  const recorded = readRecord(path.join(base, group.record));
  for (const file of removed) delete recorded[file];
  const lines = [
    ...removed.map((file) => removedLine(file, group.copy)),
    ...compare(recorded, now),
  ];
  const same = group.sameSteps;
  const copyFile = path.join(base, ".github/workflows", group.copy);
  if (same && !removed.includes(same.file) && fs.existsSync(copyFile)) {
    const theirs = stepsOf(fs.readFileSync(path.join(base, same.file), "utf8"), same.job);
    const mine = stepsOf(fs.readFileSync(copyFile, "utf8"), same.copyJob);
    if (JSON.stringify(mine) !== JSON.stringify(theirs))
      lines.push(
        `${group.copy}: job \`${same.copyJob}\`: steps (names, pinned actions) differ from ` +
          `${same.file} job \`${same.job}\`: ${JSON.stringify(mine)} != ${JSON.stringify(theirs)}`,
      );
  }
  return lines;
}

function main() {
  const group = process.argv.includes("--windows") ? GROUPS.windows : GROUPS.tests;
  if (process.argv.includes("--record")) {
    const { now, missing, removed } = current(group);
    const gone = removed.map((file) => `${file} (file removed upstream)`);
    return record(now, [...gone, ...missing], path.join(root, group.record), group.copy);
  }
  const changes = drift(group);
  const summary = process.env.GITHUB_STEP_SUMMARY;
  if (changes.length === 0) {
    const msg = `Upstream test jobs match ${group.copy} (recorded digest).`;
    console.log(msg);
    if (summary) fs.appendFileSync(summary, `### Upstream drift\n\n${msg}\n`);
    return;
  }
  const report =
    "### Upstream drift\n\n" +
    `Upstream's test jobs changed since ${group.copy} last followed them:\n\n` +
    changes.map((c) => `- ${c}`).join("\n") +
    `\n\nPort the change into ${group.copy} (or decide it does not apply and say why in the PR), then run ` +
    `\`node .github/scripts/fork-tests-drift.cjs${group === GROUPS.windows ? " --windows" : ""} --record\` ` +
    `and commit \`${group.record}\`.\n`;
  console.error(report);
  if (summary) fs.appendFileSync(summary, report);
  for (const c of changes) console.log(`::error title=Upstream drift::${c}`);
  process.exitCode = 1;
}

module.exports = { parseJobs, compare, record, current, drift, stepsOf, GROUPS, TOP };

if (require.main === module) main();
