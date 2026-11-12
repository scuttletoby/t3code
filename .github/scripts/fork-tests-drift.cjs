// Fork only. .github/workflows/fork-tests.yml copies upstream's test jobs onto
// GitHub-hosted runners. This check fails when upstream's jobs change and the
// copy was not updated: it digests every job and step of the tracked upstream
// workflows and compares them with .github/fork-tests-upstream.json.
//
//   node .github/scripts/fork-tests-drift.cjs           check (CI)
//   node .github/scripts/fork-tests-drift.cjs --record  after updating fork-tests.yml
//
// Comment-only and blank-line changes are ignored. No dependencies: the
// workflows are parsed by indentation, which is all a job/step list needs.
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

function current() {
  const out = {};
  for (const [file, only] of Object.entries(TRACKED)) {
    const jobs = parseJobs(fs.readFileSync(path.join(root, file), "utf8"));
    if (only) {
      const missing = only.filter((name) => !jobs[name]);
      out[file] = Object.fromEntries(only.map((name) => [name, jobs[name] ?? null]));
      if (missing.length) console.error(`${file}: tracked jobs missing: ${missing.join(", ")}`);
    } else {
      out[file] = jobs;
    }
  }
  return out;
}

function compare(recorded, now) {
  const changes = [];
  for (const file of new Set([...Object.keys(recorded), ...Object.keys(now)])) {
    const before = recorded[file] ?? {};
    const after = now[file] ?? {};
    for (const job of new Set([...Object.keys(before), ...Object.keys(after)])) {
      const a = before[job];
      const b = after[job];
      if (!a && b) {
        changes.push(`${file}: job \`${job}\` added upstream`);
        continue;
      }
      if (a && !b) {
        changes.push(`${file}: job \`${job}\` removed upstream`);
        continue;
      }
      if (a.header !== b.header)
        changes.push(`${file}: job \`${job}\`: settings changed (runs-on, if, needs, matrix, env...)`);
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

function main() {
  const now = current();
  if (process.argv.includes("--record")) {
    fs.writeFileSync(recordPath, `${JSON.stringify(now, null, 2)}\n`);
    console.log(`Recorded ${path.relative(root, recordPath)}`);
    return;
  }
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

module.exports = { parseJobs, compare };

if (require.main === module) main();
