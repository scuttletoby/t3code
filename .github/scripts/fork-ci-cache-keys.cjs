// Fork only: no stale cache may turn a red test green. Checks every cache key in .github/workflows/fork-*.yml
// against its FAMILY (below); a key of no family fails. `node <this> [files...]` prints an ::error:: per problem.
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");

// download: the key names the tool's version. output (built here): the key hashes the lockfiles, every restore key
// keeps that hash, it ends with the commit (exact ones: names only their inputs, has no restore key and is saved only
// on a miss), and it is saved under the key the job restored; pushOnly ones save only with actions/cache/save on a
// push after every earlier step passed, so no PR or failed run writes them.
const FAMILIES = [
  { prefix: "electron-", kind: "download" },
  { prefix: "playwright-chromium-shell-", kind: "download" },
  { prefix: "windows-tests-packages-v1-", kind: "download" },
  { prefix: "vp-tasks-v1-", kind: "output", lockfiles: ["pnpm-lock.yaml"] },
  { prefix: "vitest-modules-", kind: "output", lockfiles: ["pnpm-lock.yaml"], pushOnly: true },
  { prefix: "cargo-", kind: "output", lockfiles: ["Cargo.lock"], pushOnly: true, exact: true },
];

const CACHE_USES = /^actions\/cache(\/restore|\/save)?@/;

const indentOf = (line) => line.length - line.trimStart().length;
const isSkippable = (line) => line.trim() === "" || line.trim().startsWith("#");

// A plain scalar without its trailing comment and quotes.
function scalar(text) {
  let value = text.trim();
  if (/^'.*'$/.test(value)) return value.slice(1, -1).replace(/''/g, "'");
  if (/^".*"$/.test(value)) return value.slice(1, -1);
  const comment = value.search(/\s#/);
  if (comment !== -1) value = value.slice(0, comment).trim();
  return value;
}

// The subset of YAML the workflows use: mappings, sequences, block scalars (| and >), plain and quoted scalars.
// Returns plain objects, arrays and strings (flow collections stay strings).
function parseYaml(text) {
  const lines = text.split("\n").map((line) => ({ indent: indentOf(line), text: line.trimEnd() }));
  let i = 0;

  const skip = () => {
    while (i < lines.length && isSkippable(lines[i].text)) i++;
  };

  function blockScalar(style, parentIndent) {
    const body = [];
    while (i < lines.length && (lines[i].text.trim() === "" || lines[i].indent > parentIndent)) {
      body.push(lines[i].text);
      i++;
    }
    while (body.length && body[body.length - 1].trim() === "") body.pop();
    const indent = Math.min(...body.filter((line) => line.trim()).map(indentOf));
    const content = body.map((line) => line.slice(indent));
    return style.startsWith("|")
      ? content.join("\n")
      : content.map((line) => line.trim()).join(" ");
  }

  function value(rest, keyIndent) {
    if (/^[|>][-+]?$/.test(rest)) return blockScalar(rest, keyIndent);
    if (rest !== "") {
      let text = scalar(rest);
      // A plain scalar continued on deeper lines.
      skip();
      while (i < lines.length && lines[i].indent > keyIndent) {
        text += ` ${scalar(lines[i].text)}`;
        i++;
        skip();
      }
      return text;
    }
    skip();
    if (i >= lines.length) return null;
    const next = lines[i];
    // A flow collection over several lines, kept as one string.
    if (next.indent > keyIndent && /^[[{]/.test(next.text.trim())) {
      let flow = "";
      let depth = 0;
      do {
        const part = lines[i].text.trim();
        flow += part;
        for (const char of part) depth += "[{".includes(char) ? 1 : "]}".includes(char) ? -1 : 0;
        i++;
      } while (depth > 0 && i < lines.length);
      return flow;
    }
    if (next.indent > keyIndent || (next.indent === keyIndent && /^\s*-( |$)/.test(next.text))) {
      return block(next.indent);
    }
    return null;
  }

  function mapping(indent) {
    const result = {};
    for (skip(); i < lines.length && lines[i].indent === indent; skip()) {
      const text = lines[i].text.trim();
      if (text.startsWith("- ") || text === "-") break;
      const match = text.match(/^("[^"]*"|'[^']*'|[^:]+?):(?:\s+(.*))?$/);
      if (!match)
        throw new Error(`line ${i + 1}: expected "key: value", got ${JSON.stringify(text)}`);
      i++;
      result[scalar(match[1])] = value(match[2] ?? "", indent);
    }
    return result;
  }

  function sequence(indent) {
    const result = [];
    for (skip(); i < lines.length && lines[i].indent === indent; skip()) {
      const text = lines[i].text.trim();
      if (!(text.startsWith("- ") || text === "-")) break;
      const rest = text.slice(1).trim();
      if (rest === "" || /^("[^"]*"|'[^']*'|[^:\s][^:]*?):(\s|$)/.test(rest)) {
        // A mapping item: its first key sits two columns in, on the dash's line.
        lines[i] = { indent: indent + 2, text: " ".repeat(indent + 2) + rest };
        if (rest === "") i++;
        result.push(rest === "" ? block(lines[i]?.indent ?? indent + 2) : mapping(indent + 2));
      } else {
        i++;
        result.push(scalar(rest));
      }
    }
    return result;
  }

  function block(indent) {
    skip();
    const text = lines[i].text.trim();
    return text.startsWith("- ") || text === "-" ? sequence(indent) : mapping(indent);
  }

  skip();
  return i < lines.length ? block(lines[i].indent) : {};
}

// Every cache step: { job, index, name, id, if, uses, path, key, restoreKeys }.
function cacheSteps(text) {
  const workflow = parseYaml(text);
  const steps = [];
  for (const [job, spec] of Object.entries(workflow.jobs ?? {})) {
    const list = Array.isArray(spec?.steps) ? spec.steps : [];
    list.forEach((step, index) => {
      if (!step || typeof step.uses !== "string" || !CACHE_USES.test(step.uses)) return;
      const inputs = step.with ?? {};
      steps.push({
        job,
        index,
        name: step.name ?? step.uses,
        id: step.id,
        if: step.if ?? "",
        uses: step.uses,
        kind: step.uses.match(CACHE_USES)[1]?.slice(1) ?? "both",
        path: String(inputs.path ?? "").trim(),
        key: String(inputs.key ?? "").trim(),
        restoreKeys: String(inputs["restore-keys"] ?? "")
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean),
      });
    });
  }
  return steps;
}

const hashFilesCalls = (key) => key.match(/hashFiles\([^)]*\)/g) ?? [];

// The lockfiles a hashFiles() call reads: each quoted argument whose last part is the lockfile's name.
const hashesLockfile = (call, lockfile) =>
  (call.match(/'([^']*)'|"([^"]*)"/g) ?? [])
    .map((arg) => arg.slice(1, -1))
    .some((arg) => arg === lockfile || arg.endsWith(`/${lockfile}`));

const familyOf = (key) => FAMILIES.find((family) => key.startsWith(family.prefix));

// Problems in one workflow's text, as "file: job/step: problem" lines.
function check(text, file = "workflow") {
  const problems = [];
  const steps = cacheSteps(text);
  for (const step of steps) {
    const where = `${file}: ${step.job} / ${step.name}`;
    const say = (problem) => problems.push(`${where}: ${problem}`);
    // A save step whose key is the earlier restore's cache-primary-key: judge the restore's key.
    let key = step.key;
    const primary = key.match(
      /^\$\{\{\s*steps\.([A-Za-z0-9_-]+)\.outputs\.cache-primary-key\s*\}\}$/,
    );
    const source = primary
      ? steps.find(
          (other) => other.job === step.job && other.id === primary[1] && other.index < step.index,
        )
      : null;
    if (primary) {
      if (!source || source.kind === "save") {
        say(`its key reads steps.${primary[1]}, which is no earlier restore in this job`);
        continue;
      }
      key = source.key;
    }
    if (!key) {
      say("no key");
      continue;
    }
    const family = familyOf(key);
    if (!family) {
      say(
        `no cache family for the key ${JSON.stringify(key)}: add its rules to FAMILIES in .github/scripts/fork-ci-cache-keys.cjs`,
      );
      continue;
    }
    if (family.kind === "download") {
      if (!/\$\{\{[^}]*\}\}/.test(key.slice(family.prefix.length)))
        say("a download cache's key must name the version it holds");
      continue;
    }

    const calls = hashFilesCalls(key);
    for (const lockfile of family.lockfiles) {
      if (!calls.some((call) => hashesLockfile(call, lockfile)))
        say(`the key does not hash ${lockfile}`);
    }
    if (family.exact) {
      if (/\$\{\{\s*github\.sha\s*\}\}/.test(key))
        say("an exact cache's key names only its inputs, not the commit");
      if (step.restoreKeys.length)
        say("an exact cache has no restore keys: it is read only for the same inputs");
      if (step.kind === "save" && !/cache-hit\s*!=\s*'true'/.test(step.if)) {
        say("an exact cache is saved only when its restore missed: cache-hit != 'true'");
      }
    } else if (!/\$\{\{\s*github\.sha\s*\}\}$/.test(key)) {
      say("the key must end with ${{ github.sha }}: one entry per commit");
    }
    for (const restoreKey of step.kind === "save" ? [] : step.restoreKeys) {
      if (!key.startsWith(restoreKey))
        say(`the restore key ${JSON.stringify(restoreKey)} is not a prefix of the key`);
      for (const call of calls) {
        if (
          family.lockfiles.some((lockfile) => hashesLockfile(call, lockfile)) &&
          !restoreKey.includes(call)
        ) {
          say(
            `the restore key ${JSON.stringify(restoreKey)} drops ${call}, so it reads entries of another lockfile`,
          );
        }
      }
    }

    if (family.pushOnly) {
      if (step.kind === "both")
        say(
          "a pushOnly cache restores with actions/cache/restore and saves with actions/cache/save",
        );
      if (step.kind === "save") {
        if (!/github\.event_name\s*==\s*'push'/.test(step.if))
          say("its save must run only on a push: if: github.event_name == 'push'");
        if (/\b(always|failure|cancelled)\(\)/.test(step.if))
          say("its save must run only when every step before it passed");
      }
    }
    if (step.kind === "save") {
      const restored = steps.find(
        (other) =>
          other.job === step.job &&
          other.index < step.index &&
          other.kind !== "save" &&
          other.path === step.path &&
          other.key === key,
      );
      if (!restored) say("it saves a path or key that no earlier step of this job restores");
    }
  }
  return problems;
}

function forkWorkflows() {
  const dir = path.join(root, ".github/workflows");
  return fs
    .readdirSync(dir)
    .filter((name) => /^fork-.*\.ya?ml$/.test(name))
    .sort()
    .map((name) => path.join(dir, name));
}

function main(argv = process.argv.slice(2)) {
  const files = argv.length ? argv.map((file) => path.resolve(file)) : forkWorkflows();
  const problems = [];
  for (const file of files) {
    try {
      problems.push(...check(fs.readFileSync(file, "utf8"), path.relative(root, file)));
    } catch (error) {
      problems.push(`${path.relative(root, file)}: cannot read: ${error.message}`);
    }
  }
  for (const problem of problems) console.log(`::error::${problem}`);
  if (!problems.length)
    console.log(`fork-ci-cache-keys: ${files.length} workflows, every cache key keeps its rules`);
  return problems.length ? 1 : 0;
}

module.exports = { FAMILIES, parseYaml, cacheSteps, check, forkWorkflows, main };

if (require.main === module) process.exitCode = main();
