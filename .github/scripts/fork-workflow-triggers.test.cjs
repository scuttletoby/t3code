// Tests for fork-workflow-triggers.cjs: what it reads, and every shape it refuses.
//   node --test .github/scripts/fork-workflow-triggers.test.cjs
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { readTriggers } = require("./fork-workflow-triggers.cjs");

const SCRIPT = path.join(__dirname, "fork-workflow-triggers.cjs");
const yml = (...lines) => `${lines.join("\n")}\n`;
const refuses = (text, why) => assert.throws(() => readTriggers(text), why);

test("reads the top-level on: in each plain form", () => {
  assert.deepEqual(readTriggers(yml("on: push")), ["push"]);
  assert.deepEqual(readTriggers(yml("on: [push, issues] # two")), ["issues", "push"]);
  assert.deepEqual(readTriggers(yml("'on': 'issues'")), ["issues"]);
  assert.deepEqual(
    readTriggers(
      yml('"on":', "  push:", "    branches: [main]", "  - fork", '  "watch":', "jobs: {}"),
    ),
    ["fork", "push", "watch"],
  );
});

test("issues: as a permission or a job is not a trigger", () => {
  const text = yml(
    "on:",
    "  push:",
    "permissions:",
    "  issues: write",
    "jobs:",
    "  issues:",
    "    runs-on: x",
  );
  assert.deepEqual(readTriggers(text), ["push"]);
});

test("takes what upstream writes: CRLF, block scalars, quotes in text, a needs list over lines", () => {
  const text = yml(
    'name: it\'s "fine" # a comment',
    "on:",
    "  push: # on: issues",
    "jobs:",
    "  check:",
    '    name: "a #b"',
    "    needs:",
    "      [",
    "        build,",
    "        test,",
    "      ]",
    "    steps:",
    "      - run: |",
    "          echo \"it's open [",
    "",
    "          'it's [ open",
    "        shell: bash",
    "      - with:",
    "          args: >-",
    "            don't",
    "      - |",
    '        "open',
    "      - >2",
    "          [open",
    "on_more: x",
  );
  assert.deepEqual(readTriggers(text), ["push"]);
  assert.deepEqual(readTriggers(text.replace(/\n/g, "\r\n")), ["push"]);
});

test("refuses a line break other than \\n or \\r\\n", () => {
  for (const nl of ["\r", "\u0085", "\u2028", "\u2029"]) {
    refuses(`on:\n  push: {}${nl}  issues: {}\n`, /line break other than/);
  }
});

test("refuses a tab in the indent", () => {
  refuses("on:\n\tissues:\n", /indented with a tab/);
});

test("refuses an unindented line that is not a plain key", () => {
  for (const line of [
    "on : issues",
    '"o\\x6en": [issues]',
    "? on",
    "%YAML 1.2",
    "- push",
    "{on: issues}",
    "\uFEFFon: push",
    "\u00a0on: push",
    "true :",
  ]) {
    refuses(yml(line, "jobs: {}"), /is not a plain `key:`/);
  }
  refuses(yml("on:", "- issues"), /is not a plain `key:`/);
});

test("refuses a quote left open at the end of a line (a decoy on: inside a string)", () => {
  refuses(
    yml('name: "x', "on: push", '"', "on : issues"),
    /quote is left open at the end of line 1/,
  );
  refuses(yml("name: 'x", "on: push", "'"), /quote is left open at the end of line 1/);
  refuses(
    yml("on: push", "jobs:", "  a:", '    name: "x \\"', '    y"'),
    /quote is left open at the end of line 4/,
  );
});

test("reads the lines after a block scalar's text again", () => {
  refuses(
    yml(
      "on: push",
      "jobs:",
      "  a:",
      "    steps:",
      "      - run: |",
      "          x",
      '        name: "open',
    ),
    /line 7/,
  );
  refuses(yml("on: push", "x:", "  - |", "    y", '  - "open'), /line 5/);
  refuses(yml("name: |", "  text", "on : issues"), /is not a plain `key:`/);
});

test("refuses a bracket list whose lines are not indented more than its first line", () => {
  const decoy = yml(
    "jobs:",
    "  a:",
    "    strategy:",
    "      matrix: [x,",
    "on: push",
    "]",
    '"on": [issues]',
  );
  refuses(decoy, /line 5 is inside the bracket opened on line 4/);
  refuses(
    yml("on: push", "jobs:", "  a:", "    needs: [", "      a,", "    ] x"),
    /line 6 is inside the bracket/,
  );
  refuses(
    yml("on: push", "jobs:", "  a:", "    needs: [", "      a,"),
    /bracket opened on line 4 is never closed/,
  );
});

test("refuses on: twice and more than one document", () => {
  refuses(yml("on: push", "jobs: {}", "on: issues"), /`on:` appears twice/);
  refuses(yml("on: push", '"on": issues'), /`on:` appears twice/);
  refuses(yml("---", "on: push", "---", "on: issues"), /more than one YAML document/);
  refuses(yml("on: push", "...", "on: issues"), /more than one YAML document/);
  assert.deepEqual(readTriggers(yml("--- # one document", "on: push")), ["push"]);
});

test("refuses no on:, an empty on: and uneven indent under on:", () => {
  refuses(yml("jobs: {}"), /no top-level `on:`/);
  refuses(yml("name: x", "  on: issues"), /no top-level `on:`/);
  refuses(yml("on:", "jobs: {}"), /`on:` is empty/);
  refuses(yml("on:", "    push:", "  issues:"), /indented unevenly/);
});

test("refuses a list over lines and every other form under on:", () => {
  refuses(yml("on: [push,", "  issues]"), /form this reader does not take/);
  for (const rest of ["{push: {}}", "&a push", "*a", "|", ">-", "!!str push", "[push, [issues]]"]) {
    refuses(yml(`on: ${rest}`, "  x"), /form this reader does not take/);
  }
  refuses(yml("on:", "  <<: *a"), /`<<: \*a` under `on:`/);
  refuses(yml("on:", "  ? issues"), /`\? issues` under `on:`/);
  refuses(yml("on: Issues"), /`Issues` is not an event name/);
  refuses(yml("on: [push, 'iss\\x75es']"), /is not an event name/);
  refuses(yml("on:", "  - pull_request_target: x"), /is not an event name/);
});

test("the command prints one event per line, and exits 2 with the reason when unsure", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fork-triggers-"));
  try {
    const file = path.join(dir, "w.yml");
    fs.writeFileSync(file, yml("on: [workflow_run, push]"));
    let run = spawnSync(process.execPath, [SCRIPT, file], { encoding: "utf8" });
    assert.equal(run.status, 0);
    assert.equal(run.stdout, "push\nworkflow_run\n");
    fs.writeFileSync(file, yml("jobs: {}"));
    run = spawnSync(process.execPath, [SCRIPT, file], { encoding: "utf8" });
    assert.equal(run.status, 2);
    assert.equal(run.stdout, "");
    assert.match(run.stderr, /^cannot read the triggers: no top-level `on:`\n$/);
    run = spawnSync(process.execPath, [SCRIPT, path.join(dir, "missing.yml")], {
      encoding: "utf8",
    });
    assert.equal(run.status, 2);
    assert.match(run.stderr, /^cannot read the triggers: cannot read .*missing\.yml/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
