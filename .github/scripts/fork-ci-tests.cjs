// Fork only: every `*.test.*` under .github/scripts, fork-ci and fork-kit is listed in .github/fork-ci-tests.txt and
// runs here by extension (RUNNER), FORK_CI_TEST_JOBS at once (default: CPU count), or `run by <file>` (not an upstream
// workflow; it names the test). Unlisted or runner-less tests fail; a listed one that is gone fails once history had it.
// Arguments pick a part by path prefix (`fork-kit/`, `!fork-kit/`; none: all), FORK_CI_TEST_SHARD one shard of it.
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const LIST = ".github/fork-ci-tests.txt";
const TEST_DIRS = [".github/scripts", "fork-ci", "fork-kit"];
const TEST_FILE = /\.test\.([^./]+)$/;
const RUNNER = {
  sh: ["bash"],
  py: ["python3"],
  cjs: ["node", "--test"],
  mjs: ["node", "--test"],
  js: ["node", "--test"],
};
const UPSTREAM_WORKFLOW = /^\.github\/workflows\/(?!fork-)[^/]+\.ya?ml$/;

// Words after which a `/` starts a regular expression, not a division.
const BEFORE_REGEX = new Set(
  "return typeof instanceof in of new delete void throw case do else yield await".split(" "),
);

// A JS/TS file's text with its comments blanked (newlines kept): strings, template literals (`${}` nested) and
// regular expressions stay whole, so a `//` or `/*` in one (upstream's `'**/*'`) is no comment. A `/` is a regular
// expression where no value ends before it. Same comments as TypeScript's parser on the tree's JS/TS files, but JSX
// text: there a `//` blanks the rest of its line (removed code: the test counts as not run, the safe side).
function jsCode(text) {
  let out = "";
  let i = 0;
  let valueEnds = false; // the last token can end a value: a `/` after it divides
  const nest = []; // for each open `{`: "code", or "template" for a `${` inside a template literal
  const blank = (part) => part.replace(/[^\n]/g, " ");
  const template = () => {
    // From just after a backtick or a `}` that closes a `${`: to the closing backtick, or into the next `${`.
    const start = i;
    while (i < text.length && text[i] !== "`" && !(text[i] === "$" && text[i + 1] === "{"))
      i += text[i] === "\\" ? 2 : 1;
    if (text[i] === "`") {
      i += 1;
      valueEnds = true;
    } else if (i < text.length) {
      i += 2;
      nest.push("template");
      valueEnds = false;
    }
    out += text.slice(start, i);
  };
  while (i < text.length) {
    const c = text[i];
    const next = text[i + 1];
    if (c === "/" && next === "/") {
      const end = text.indexOf("\n", i);
      const stop = end === -1 ? text.length : end;
      out += blank(text.slice(i, stop));
      i = stop;
    } else if (c === "/" && next === "*") {
      const end = text.indexOf("*/", i + 2);
      const stop = end === -1 ? text.length : end + 2;
      out += blank(text.slice(i, stop));
      i = stop;
    } else if (c === "'" || c === '"') {
      const start = i;
      i += 1;
      while (i < text.length && text[i] !== c && text[i] !== "\n") i += text[i] === "\\" ? 2 : 1;
      i += 1;
      out += text.slice(start, i);
      valueEnds = true;
    } else if (c === "`") {
      out += c;
      i += 1;
      template();
    } else if (c === "/" && !valueEnds) {
      const start = i;
      let inClass = false;
      i += 1;
      while (i < text.length && text[i] !== "\n" && (inClass || text[i] !== "/")) {
        if (text[i] === "\\") i += 1;
        else if (text[i] === "[") inClass = true;
        else if (text[i] === "]") inClass = false;
        i += 1;
      }
      i += 1;
      while (i < text.length && /\w/.test(text[i])) i += 1;
      out += text.slice(start, i);
      valueEnds = true;
    } else if (c === "{") {
      nest.push("code");
      out += c;
      i += 1;
      valueEnds = false;
    } else if (c === "}") {
      i += 1;
      out += c;
      if (nest.pop() === "template") template();
      else valueEnds = true;
    } else if (/[\w$]/.test(c)) {
      const word = text.slice(i).match(/^[\w$]+/)[0];
      out += word;
      i += word.length;
      valueEnds = !BEFORE_REGEX.has(word);
    } else {
      out += c;
      i += 1;
      if (!/\s/.test(c)) valueEnds = c === ")" || c === "]";
    }
  }
  return out;
}

// A runner file's lines with its comments removed, so a path kept only in a comment names nothing. A JavaScript or
// TypeScript file goes through jsCode(), so a comment marker inside a string is not a comment. Any other file (YAML,
// shell, Python) loses `#` to the line's end where `#` starts the line or follows a space; where that errs (a ` #`
// inside a string) it removes code, so the test counts as not run: the safe side.
function codeLines(text, file = "") {
  if (/\.[cm]?[jt]sx?$/.test(file)) return jsCode(text).split("\n");
  return text.split("\n").map((line) => line.replace(/(^|\s)#.*/, "$1"));
}

// `entries`: [{ file, runBy, weight }]; `present`: test files in the tree;
// `ever(file)` says whether the file is in the tested commit's history;
// `read(file)` returns a runner file's text, or null when it does not exist.
function plan(entries, present, ever, read = () => null) {
  const listed = entries.map((entry) => entry.file);
  const unlisted = present.filter((file) => !listed.includes(file));
  const missing = listed.filter((file) => !present.includes(file));
  const deleted = missing.filter((file) => ever(file));
  const pending = missing.filter((file) => !deleted.includes(file));
  const here = entries.filter((entry) => !entry.runBy && present.includes(entry.file));
  const noRunner = here
    .filter((entry) => !RUNNER[entry.file.match(TEST_FILE)?.[1]])
    .map((entry) => entry.file);
  const elsewhere = [];
  const notRun = [];
  for (const { file, runBy } of entries) {
    if (!runBy || !present.includes(file)) continue;
    let why = null;
    if (UPSTREAM_WORKFLOW.test(runBy))
      why = `${runBy} is an upstream workflow, disabled in the fork`;
    else {
      const text = read(runBy);
      if (text === null) why = `${runBy} does not exist`;
      else if (!codeLines(text, runBy).some((line) => line.includes(file)))
        why = `${runBy} does not name ${file} (a comment does not count)`;
    }
    if (why) notRun.push({ file, why });
    else elsewhere.push({ file, runBy });
  }
  return {
    run: here.map((entry) => entry.file).filter((file) => !noRunner.includes(file)),
    unlisted,
    deleted,
    pending,
    noRunner,
    elsewhere,
    notRun,
  };
}

function readList(text) {
  return text
    .split("\n")
    .map((line) => line.replace(/#.*/, "").trim())
    .filter(Boolean)
    .map((line) => {
      const match = line.match(/^(\S+)(?:\s+~([1-9]\d{0,4})s|\s+run by\s+(\S+))?$/);
      if (!match)
        throw new Error(
          `${LIST}: cannot read "${line}" (expected "<test>", "<test> ~<seconds>s" or "<test> run by <file>")`,
        );
      return {
        file: match[1],
        // `./a/b` and `a//b` name the same file as `a/b`, so every reader compares one spelling.
        runBy: match[3] ? path.posix.normalize(match[3]) : null,
        weight: match[2] ? Number(match[2]) : null,
      };
    });
}

// A test without a weight counts this much when shards are split: one over 30 s fails until it has one.
const UNWEIGHTED = 10;

// The files to run in start order: those without a weight first (a new test may be long), in list order, then the
// heaviest first (equal weights in list order). With every slot busy from the start, the longest test sets the time.
function order(files, entries) {
  const weight = new Map(entries.map((entry) => [entry.file, entry.weight]));
  const position = new Map(entries.map((entry, index) => [entry.file, index]));
  const rank = (file) => weight.get(file) ?? Infinity;
  return files.toSorted((a, b) => rank(b) - rank(a) || position.get(a) - position.get(b));
}

// Shard `index` (1-based) of `count`: the files heaviest first (as order() starts them), each onto the shard with the
// least weight so far (the lowest number on a tie). Only the list and the files decide, so every shard of a run makes
// the same split: each file is in exactly one shard.
function shard(files, entries, index, count) {
  const weight = new Map(entries.map((entry) => [entry.file, entry.weight ?? UNWEIGHTED]));
  const load = new Array(count).fill(0);
  const mine = [];
  for (const file of order(
    files,
    entries.map((entry) => ({ ...entry, weight: weight.get(entry.file) })),
  )) {
    const lightest = load.indexOf(Math.min(...load));
    load[lightest] += weight.get(file) ?? UNWEIGHTED;
    if (lightest === index - 1) mine.push(file);
  }
  return mine;
}

// FORK_CI_TEST_SHARD as { index, count }: unset or empty is the whole part (1/1); anything else but `<i>/<n>` with
// 1 <= i <= n throws, so a broken matrix cannot quietly run one shard's tests or none.
function readShard(env = process.env) {
  const value = env.FORK_CI_TEST_SHARD ?? "";
  if (value === "") return { index: 1, count: 1 };
  const match = value.match(/^([1-9]\d*)\/([1-9]\d*)$/);
  if (!match || Number(match[1]) > Number(match[2]))
    throw new Error(`FORK_CI_TEST_SHARD="${value}": expected <index>/<count>, 1 <= index <= count`);
  return { index: Number(match[1]), count: Number(match[2]) };
}

// Whether `file` is in the part the arguments name: under one of the `<prefix>` ones (any, when there are none),
// and under none of the `!<prefix>` ones.
function inPart(file, parts) {
  const want = parts.filter((part) => !part.startsWith("!"));
  const skip = parts.filter((part) => part.startsWith("!")).map((part) => part.slice(1));
  return (
    (want.length === 0 || want.some((prefix) => file.startsWith(prefix))) &&
    !skip.some((prefix) => file.startsWith(prefix))
  );
}

// How fast this runner ran the tests against their weights: the median of time / weight over the passed tests with a
// weight of at least 10 s (1 with fewer than three). A slow runner takes about the same factor longer on each.
function runSpeed(runs, weights) {
  const ratios = runs
    .filter((run) => run.status === 0 && (weights.get(run.file) ?? 0) >= 10)
    .map((run) => Number(run.seconds) / weights.get(run.file))
    .toSorted((a, b) => a - b);
  if (ratios.length < 3) return 1;
  const middle = Math.floor(ratios.length / 2);
  return ratios.length % 2 ? ratios[middle] : (ratios[middle - 1] + ratios[middle]) / 2;
}

// Why a test's weight must change (null: it need not), from its time on a runner of `speed` (runSpeed()): a weight
// missing on a test over 30 s, or off by more than half or double and by more than 30 s (load moves times less than
// that). Shards are split by the weights, so a stale one can push a shard to its time limit: it fails the run.
function weightNote(file, weight, seconds, speed = 1) {
  const usual = Math.max(1, Math.round(seconds / speed));
  const took = `${file} took ${Math.round(seconds)} s${speed === 1 ? "" : ` (~${usual}s on a usual runner)`}`;
  if (weight === null) return usual > 30 ? `${took} and has no weight: add "~${usual}s"` : null;
  return Math.abs(usual - weight) > 30 && (usual > 2 * weight || usual < weight / 2)
    ? `${took}, its weight is ~${weight}s: make it "~${usual}s"`
    : null;
}

// One shard's results: its failed tests, the passed ones whose weight must change (weightNote(), at the runner's
// speed) and that speed. A failed test's time says nothing about its usual one: it gets no weight note.
function judge(runs, weights) {
  const speed = runSpeed(runs, weights);
  const stale = [];
  for (const run of runs) {
    const note =
      run.status === 0 &&
      weightNote(run.file, weights.get(run.file) ?? null, Number(run.seconds), speed);
    if (note) stale.push({ file: run.file, note });
  }
  return { failed: runs.filter((run) => run.status !== 0).map((run) => run.file), stale, speed };
}

// The seconds `files` should take on a usual runner, `jobs` at once: their weights (UNWEIGHTED for none) over the
// slots, never less than the heaviest (the kit shards' shape test plans each shard the same way).
function expectedSeconds(files, weights, jobs) {
  const each = files.map((file) => weights.get(file) ?? UNWEIGHTED);
  return Math.max(each.reduce((total, seconds) => total + seconds, 0) / jobs, ...each, 0);
}

// A warning (null: none) when a shard took more than twice its expected time and over a minute more. Each weight is
// judged at the shard's own speed (runSpeed()), so a slowdown all its tests share (a slow runner, or a change that
// slows them all) is no stale weight: without this, only the job's time limit would show it. A runner can be slow,
// so it warns and does not fail.
function slowNote(files, weights, jobs, seconds) {
  const expected = expectedSeconds(files, weights, jobs);
  return seconds > 2 * expected && seconds - expected > 60
    ? `the tests here took ${Math.round(seconds)} s in all, ~${Math.round(expected)}s expected from their weights ` +
        `(${jobs} at once): a slowdown all of them share, which no weight shows; the job's time limit is next`
    : null;
}

function findTests(dir, root = ROOT) {
  const out = [];
  for (const entry of fs.readdirSync(path.join(root, dir), { withFileTypes: true })) {
    const rel = `${dir}/${entry.name}`;
    if (entry.isDirectory()) out.push(...findTests(rel, root));
    else if (TEST_FILE.test(entry.name)) out.push(rel);
  }
  return out.toSorted();
}

// Every test file under TEST_DIRS. A folder that is not there adds none: a listed test in it is then
// missing, which plan() reports (a failure once the tested history had it).
function presentTests(root = ROOT) {
  return TEST_DIRS.filter((dir) => fs.existsSync(path.join(root, dir))).flatMap((dir) =>
    findTests(dir, root),
  );
}

// `--run <test>` runs a test's declared own runs; with FORK_CI_TEST_RECORDS set (CI) each call records what ran (#664).
// FORK_OWN: the fork's own tests (upstream's, in fork-tests.yml's `test` job, are not recorded).
const FORK_OWN = /^(?:fork-kit\/|fork-ci\/|\.github\/scripts\/fork-)\S*\.test\.\w+$/;
const THIS_WORKFLOW = ".github/workflows/fork-tests.yml";
const DECLARATION = /^\s*(?:#|\/\/)\s*fork-ci:(.*)$/;
// Only a test's own switches: a declaration cannot set PATH, NODE_OPTIONS, PYTHON* or LD_*.
const SWITCH = /^[A-Z][A-Z0-9_]*_TESTS=[\w./:-]*$/;
const RUN_FORM = '"fork-ci: run [as root] [with <NAME>_TESTS=value ...] [only <TestClass>]"';
const SKIPS_FORM = '"fork-ci: skips at most <n>"';

// A test's whole-line `fork-ci:` comments: RUN_FORM is an own run (made by `--run <test>`, it may skip no test);
// SKIPS_FORM lets its run in a part skip up to n tests (else none). The forms are in .github/fork-ci-tests.txt.
// Returns { runs: [{ root, vars, only }], skips, problems }.
function declarations(text, file = "") {
  const out = { runs: [], skips: 0, problems: [] };
  let skipsSeen = false;
  for (const line of text.split("\n")) {
    const match = line.match(DECLARATION);
    if (!match) continue;
    const words = match[1].trim().split(/\s+/).filter(Boolean);
    const said = `${file}: "fork-ci:${match[1]}"`;
    if (words[0] === "skips") {
      const ok =
        words.length === 4 &&
        words[1] === "at" &&
        words[2] === "most" &&
        /^[1-9]\d{0,3}$/.test(words[3]);
      if (!ok || skipsSeen)
        out.problems.push(`${said}: expected ${SKIPS_FORM} (n at least 1), once`);
      else {
        out.skips = Number(words[3]);
        skipsSeen = true;
      }
      continue;
    }
    if (words[0] !== "run") {
      out.problems.push(`${said}: expected ${RUN_FORM} or ${SKIPS_FORM}`);
      continue;
    }
    const run = { root: false, vars: [], only: null };
    let i = 1;
    let bad = null;
    if (words[i] === "as" && words[i + 1] === "root") {
      run.root = true;
      i += 2;
    }
    if (words[i] === "with") {
      i += 1;
      while (i < words.length && SWITCH.test(words[i])) run.vars.push(words[i++]);
      if (run.vars.length === 0) bad = "`with` takes <NAME>_TESTS=value words";
    }
    if (!bad && words[i] === "only") {
      if (file.endsWith(".py") && /^[A-Z]\w*$/.test(words[i + 1] ?? "")) {
        run.only = words[i + 1];
        i += 2;
      } else bad = "`only` takes one test class, in a Python test";
    }
    if (!bad && i < words.length) bad = `\`${words.slice(i).join(" ")}\` is not understood`;
    const names = run.vars.map((v) => v.split("=")[0]);
    if (!bad && new Set(names).size !== names.length) bad = "a switch comes twice";
    if (!bad && out.runs.some((other) => other.only === run.only))
      bad = "two own runs of the same tests";
    if (bad) out.problems.push(`${said}: ${bad}; expected ${RUN_FORM}`);
    else out.runs.push(run);
  }
  return out;
}

// A listed test's own runs: those it declares, or, for a fork test the list says fork-tests.yml runs (so no part
// does), one plain run.
function ownRuns(entry, text) {
  const declared = declarations(text ?? "", entry.file);
  if (declared.runs.length === 0 && entry.runBy === THIS_WORKFLOW && FORK_OWN.test(entry.file))
    declared.runs.push({ root: false, vars: [], only: null });
  return declared;
}

// The name of a test's record: the test for its run in a part, `<test> --run [only <TestClass>]` for an own run.
function runKey(file, run = null) {
  return run ? `${file} --run${run.only ? ` only ${run.only}` : ""}` : file;
}

// Every record a run of fork-tests.yml must leave (fork-ci-result checks each): one for each listed present test a
// part runs, and one for each own run of a test a part or fork-tests.yml runs.
function expectedKeys(entries, present, read) {
  const keys = [];
  for (const entry of entries) {
    if (!present.includes(entry.file)) continue;
    if (!entry.runBy) keys.push(runKey(entry.file));
    if (!entry.runBy || entry.runBy === THIS_WORKFLOW)
      for (const run of ownRuns(entry, read(entry.file)).runs) keys.push(runKey(entry.file, run));
  }
  return keys.toSorted();
}

// What a test's output says it ran, { format, tests, skipped, failed }, or null (no count: no test known to have run):
// unittest's summary at the very end of stderr (stdout is buffered, so never read for it); node --test's last totals
// on stdout (todo counts as skipped); else the fork's `ok <name>` and `FAIL <name>` lines.
function countTests(file, stdout, stderr) {
  const ext = file.match(TEST_FILE)?.[1];
  if (ext === "py") {
    const summary = stderr.match(
      /(?:^|\n)Ran (\d+) tests? in [\d.]+s\n\n(OK|FAILED|NO TESTS RAN)(?: \(([^)\n]*)\))?\s*$/,
    );
    if (summary) {
      const detail = {};
      for (const part of (summary[3] ?? "").split(", ").filter(Boolean)) {
        const [key, value] = part.split("=");
        detail[key] = Number(value);
      }
      const failed =
        (detail.failures ?? 0) + (detail.errors ?? 0) + (detail["unexpected successes"] ?? 0);
      return {
        format: "unittest",
        tests: Number(summary[1]),
        skipped: (detail.skipped ?? 0) + (detail["expected failures"] ?? 0),
        failed: summary[2] === "FAILED" ? Math.max(1, failed) : failed,
      };
    }
  }
  if (ext === "cjs" || ext === "mjs" || ext === "js") {
    const total = (name) => {
      const all = [...stdout.matchAll(new RegExp(`^(?:#|ℹ) ${name} (\\d+)$`, "gm"))];
      return all.length ? Number(all.at(-1)[1]) : null;
    };
    if (total("tests") !== null)
      return {
        format: "node",
        tests: total("tests"),
        skipped: (total("skipped") ?? 0) + (total("todo") ?? 0),
        failed: total("fail") ?? 0,
      };
  }
  const text = `${stdout}\n${stderr}`;
  const passed = text.match(/^ok\s/gm)?.length ?? 0;
  const failed = text.match(/^FAIL\s/gm)?.length ?? 0;
  return passed + failed
    ? { format: "ok lines", tests: passed + failed, skipped: 0, failed }
    : null;
}

// Why one record does not prove its tests ran ([]: it does): a failure, no count, no test run that was not skipped,
// or more skipped than allowed (an own run: none).
function recordProblems(record) {
  const problems = [];
  const name = record.key;
  if (record.status !== 0) problems.push(`${name} failed (exit ${record.status})`);
  if (record.tests === null)
    problems.push(`${name}: its output shows no test count, so no test is known to have run`);
  else {
    if (record.tests - record.skipped <= 0)
      problems.push(`${name} ran no test (${record.tests} found, ${record.skipped} skipped)`);
    if (record.skipped > record.allowed)
      problems.push(
        `${name} skipped ${record.skipped} tests, ${record.allowed} allowed: run them (a switch the test ` +
          `declares in a ${RUN_FORM} line) or, if they cannot run here, declare ${SKIPS_FORM}`,
      );
    if (record.failed > 0) problems.push(`${name}: ${record.failed} failed`);
  }
  return problems;
}

function record(key, file, run, result, allowed) {
  const counts = countTests(file, result.stdout ?? "", result.stderr ?? "");
  return {
    key,
    test: file,
    own: run !== null,
    root: run?.root ?? false,
    vars: run?.vars ?? [],
    only: run?.only ?? null,
    status: result.status,
    seconds: Number(result.seconds),
    format: counts?.format ?? null,
    tests: counts?.tests ?? null,
    skipped: counts?.skipped ?? null,
    failed: counts?.failed ?? null,
    allowed,
  };
}

// The records folder of this call (null: none written), from FORK_CI_TEST_RECORDS, with the run and job they belong
// to; with the folder set, a missing GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT or GITHUB_JOB throws.
function recordsTo(env = process.env) {
  const dir = env.FORK_CI_TEST_RECORDS ?? "";
  if (dir === "") return null;
  for (const name of ["GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB"])
    if (!/^[\w-]+$/.test(env[name] ?? ""))
      throw new Error(
        `FORK_CI_TEST_RECORDS is set but ${name} is not: the records cannot be keyed`,
      );
  return {
    dir,
    run_id: env.GITHUB_RUN_ID,
    run_attempt: Number(env.GITHUB_RUN_ATTEMPT),
    job: env.GITHUB_JOB,
  };
}

// Writes one call's records file; never over an existing one (a second call of the same part in a job fails).
function writeRecords(to, name, body) {
  fs.mkdirSync(to.dir, { recursive: true });
  const file = path.join(to.dir, `${name.replace(/[^\w.-]+/g, "_")}.json`);
  fs.writeFileSync(
    file,
    JSON.stringify(
      { v: 1, run_id: to.run_id, run_attempt: to.run_attempt, job: to.job, ...body },
      null,
      1,
    ) + "\n",
    { flag: "wx" },
  );
  return file;
}

function headSha() {
  try {
    return git(["rev-parse", "HEAD"]);
  } catch {
    return null;
  }
}

function git(args) {
  return childProcess.execFileSync("git", args, { cwd: ROOT, encoding: "utf8" }).trim();
}

// A shallow checkout first fetches the full history of HEAD, trees only.
function everInHistory(file) {
  if (git(["rev-parse", "--is-shallow-repository"]) === "true")
    git([
      "fetch",
      "--quiet",
      "--no-tags",
      "--filter=blob:none",
      "--unshallow",
      "origin",
      git(["rev-parse", "HEAD"]),
    ]);
  return git(["log", "-1", "--format=%h", "HEAD", "--", file]) !== "";
}

function readFile(file) {
  try {
    return fs.readFileSync(path.join(ROOT, file), "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

// Calls start(item) for every item, at most `jobs` at once, each as soon as a slot is free.
// start returns a promise; the results come back in the items' order.
async function runAll(items, jobs, start) {
  const results = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const index = next++;
      results[index] = await start(items[index]);
    }
  }
  await Promise.all(Array.from({ length: Math.max(1, Math.min(jobs, items.length)) }, worker));
  return results;
}

function jobCount(env = process.env) {
  const jobs = Number(env.FORK_CI_TEST_JOBS);
  return Number.isInteger(jobs) && jobs > 0 ? jobs : os.availableParallelism();
}

// The command of a test's run: by its extension (RUNNER), or for an own run (ownRuns()) Python with -I -B and the class
// picked, under `sudo -n env` as root (sudo keeps none of the environment, so the switches and GITHUB_ACTIONS are
// handed on there), else with the switches added to the environment. The records folder never reaches a test: a test
// of this runner that runs it must not write records of its own.
function command(file, run = null, env = process.env) {
  const childEnv = { ...env };
  delete childEnv.FORK_CI_TEST_RECORDS;
  if (!run) return { argv: [...RUNNER[file.match(TEST_FILE)[1]], file], env: childEnv };
  const base = file.endsWith(".py") ? ["python3", "-I", "-B"] : RUNNER[file.match(TEST_FILE)[1]];
  const argv = [...base, file, ...(run.only ? ["-v", run.only] : [])];
  if (run.root) {
    const pass = env.GITHUB_ACTIONS === undefined ? [] : [`GITHUB_ACTIONS=${env.GITHUB_ACTIONS}`];
    return { argv: ["sudo", "-n", "env", ...run.vars, ...pass, ...argv], env: childEnv };
  }
  for (const word of run.vars) {
    const at = word.indexOf("=");
    childEnv[word.slice(0, at)] = word.slice(at + 1);
  }
  return { argv, env: childEnv };
}

// How an own run is named in the log.
function runLabel(run) {
  return [
    run.root && "as root",
    run.vars.length && `with ${run.vars.join(" ")}`,
    run.only && `only ${run.only}`,
  ]
    .filter(Boolean)
    .join(" ");
}

// Runs one test file (`run`: one of its own runs), its stdout and stderr shown together as one group when it ends, and
// each also kept on its own (countTests()).
function runTest(file, run = null) {
  const { argv, env } = command(file, run);
  const [cmd, ...args] = argv;
  const started = Date.now();
  return new Promise((resolve) => {
    const child = childProcess.spawn(cmd, args, {
      cwd: ROOT,
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const output = [];
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => {
      output.push(chunk);
      stdout.push(chunk);
    });
    child.stderr.on("data", (chunk) => {
      output.push(chunk);
      stderr.push(chunk);
    });
    let ended = false;
    const done = (status, signal) => {
      if (ended) return;
      ended = true;
      // Stop reading pipes a left-behind process still holds, so they do not keep this script alive.
      child.stdout.destroy();
      child.stderr.destroy();
      const seconds = ((Date.now() - started) / 1000).toFixed(1);
      const label = run ? ` --run ${runLabel(run)}`.trimEnd() : "";
      process.stdout.write(`::group::${file}${label} (${seconds} s)\n`);
      process.stdout.write(Buffer.concat(output));
      process.stdout.write("\n::endgroup::\n");
      if (status !== 0)
        console.log(`::error file=${file}::${file}${label} failed (exit ${status ?? signal})`);
      resolve({
        file,
        status,
        seconds,
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
      });
    };
    child.on("error", (error) => {
      output.push(Buffer.from(`cannot start ${cmd}: ${error.message}\n`));
      done(null, "spawn error");
    });
    child.on("close", done);
    // A process the test left behind may hold the pipes open: the test is done when it exits.
    child.on("exit", (status, signal) => setTimeout(() => done(status, signal), 5000).unref());
  });
}

// `--run <test>`: the test's own runs (ownRuns()), one after another, each recorded. Only a listed, present test the
// list says a part or fork-tests.yml runs, with at least one own run, and no `fork-ci:` line it cannot read.
async function runOwn(args, { runTest: run = runTest } = {}) {
  const to = recordsTo();
  const [file, ...extra] = args;
  const entry = readList(fs.readFileSync(path.join(ROOT, LIST), "utf8")).find(
    (e) => e.file === file,
  );
  const text = file ? readFile(file) : null;
  let why = null;
  if (!file || extra.length) why = "--run takes one listed test";
  else if (!entry) why = `${file} is not in ${LIST}`;
  else if (text === null) why = `${file} is not present`;
  else if (entry.runBy && entry.runBy !== THIS_WORKFLOW)
    why = `${LIST} says ${entry.runBy} runs ${file}`;
  const own = why ? null : ownRuns(entry, text);
  if (own && own.problems.length) why = own.problems.join("; ");
  else if (own && own.runs.length === 0)
    why = `${file} declares no own run (${RUN_FORM}); a part runs it`;
  if (why) {
    console.log(`::error::${why}`);
    process.exitCode = 1;
    return;
  }
  const results = [];
  for (const how of own.runs) {
    const result = await run(file, how);
    results.push(record(runKey(file, how), file, how, result, 0));
  }
  let failed = results.filter((r) => r.status !== 0).length;
  if (to) {
    // A failed run has its error already; the others' problems fail the call here, before fork-ci-result.
    for (const r of results.filter((each) => each.status === 0))
      for (const problem of recordProblems(r)) {
        failed += 1;
        console.log(`::error file=${file}::${problem}`);
      }
    writeRecords(to, `run-${file}`, {
      sha: headSha(),
      call: { run: file },
      all: null,
      owed: results.map((r) => r.key),
      results,
    });
  }
  const summary = process.env.GITHUB_STEP_SUMMARY;
  if (summary)
    fs.appendFileSync(
      summary,
      [
        `### Fork test own runs (${file})`,
        "",
        "| Run | Result | Tests | Skipped | Time |",
        "| --- | --- | --- | --- | --- |",
        ...results.map(
          (r) =>
            `| \`${r.key}\` | ${r.status === 0 ? "passed" : "**failed**"} | ${r.tests ?? "?"} | ${r.skipped ?? "?"} | ${r.seconds} s |`,
        ),
        "",
      ].join("\n") + "\n",
    );
  if (failed) process.exitCode = 1;
}

// `runTest` and `now` are the real ones unless a test hands in fakes (a shard's time, its slow warning).
async function main(
  parts = process.argv.slice(2),
  { runTest: run = runTest, now = Date.now } = {},
) {
  if (parts[0] === "--run") return runOwn(parts.slice(1), { runTest: run });
  const to = recordsTo();
  const { index, count } = readShard();
  const listed = readList(fs.readFileSync(path.join(ROOT, LIST), "utf8"));
  const entries = listed.filter((entry) => inPart(entry.file, parts));
  const everyPresent = presentTests();
  const present = everyPresent.filter((file) => inPart(file, parts));
  const result = plan(entries, present, everInHistory, readFile);
  const weight = new Map(entries.map((entry) => [entry.file, entry.weight]));
  const rows = [];
  let failed =
    result.unlisted.length + result.deleted.length + result.noRunner.length + result.notRun.length;
  // A `fork-ci:` line that cannot be read fails the part the test is in (every shard of it).
  const declared = new Map(
    entries
      .filter((entry) => present.includes(entry.file))
      .map((entry) => [entry.file, declarations(readFile(entry.file) ?? "", entry.file)]),
  );
  for (const [file, { problems }] of declared)
    for (const problem of problems) {
      failed += 1;
      console.log(`::error file=${file}::${problem}`);
    }
  const mine = shard(result.run, entries, index, count);
  const jobs = jobCount();
  const started = now();
  const runs = await runAll(order(mine, entries), jobs, (file) => run(file));
  const seconds = (now() - started) / 1000;
  const judged = judge(runs, weight);
  const { speed } = judged;
  const stale = judged.stale.map(({ note }) => note);
  failed += judged.failed.length + stale.length;
  if (to) {
    const records = runs.map((r) =>
      record(runKey(r.file), r.file, null, r, declared.get(r.file)?.skips ?? 0),
    );
    // A failed test has its error already; the others' problems fail the part here, before fork-ci-result.
    for (const r of records.filter((each) => each.status === 0))
      for (const problem of recordProblems(r)) {
        failed += 1;
        console.log(`::error file=${r.test}::${problem}`);
      }
    writeRecords(to, `part-${index}of${count}-${parts.join("_") || "all"}`, {
      sha: headSha(),
      call: { parts, shard: `${index}/${count}` },
      all: expectedKeys(listed, everyPresent, readFile),
      owed: mine.map((file) => runKey(file)).toSorted(),
      results: records,
    });
  }
  for (const { file, note } of judged.stale)
    console.log(`::error file=${file}::${note} in ${LIST}`);
  const slow = slowNote(mine, weight, jobs, seconds);
  if (slow) console.log(`::warning::${slow}`);
  for (const run of runs) {
    rows.push(
      `| \`${run.file}\` | ${run.status === 0 ? "passed" : "**failed**"} | ${run.seconds} s | ${weight.get(run.file) ? `~${weight.get(run.file)}s` : ""} |`,
    );
  }
  for (const file of result.unlisted)
    console.log(`::error file=${file}::${file} is not in ${LIST}; add it there`);
  for (const file of result.deleted)
    console.log(`::error::${file} is listed in ${LIST} but was deleted; restore it or unlist it`);
  for (const file of result.noRunner)
    console.log(
      `::error file=${file}::${file}: no runner for this extension (${Object.keys(RUNNER).join(", ")}); ` +
        `rename it or list it as "run by <file>"`,
    );
  for (const { file, why } of result.notRun)
    console.log(`::error file=${file}::${file} is not run: ${why}`);
  // A part that runs nothing is a wrong argument (a typo, a renamed folder): its tests would never run.
  const empty = (parts.length > 0 || count > 1) && mine.length === 0;
  const named = [...parts, ...(count > 1 ? [`shard ${index}/${count}`] : [])].join(" ");
  if (empty) {
    failed += 1;
    console.log(
      `::error::the part ${named} runs no test here: fix the argument of this fork-ci-tests.cjs call ` +
        `(a typo or a renamed folder?) or its number of shards, or its tests never run.`,
    );
  }
  for (const { file, runBy } of result.elsewhere)
    rows.push(`| \`${file}\` | run by \`${runBy}\` | | |`);
  for (const file of result.pending)
    rows.push(`| \`${file}\` | not present: its PR has not merged | | |`);
  for (const file of result.unlisted) rows.push(`| \`${file}\` | **not in ${LIST}** | | |`);
  for (const file of result.deleted) rows.push(`| \`${file}\` | **deleted while listed** | | |`);
  for (const file of result.noRunner)
    rows.push(`| \`${file}\` | **no runner for its extension** | | |`);
  for (const { file, why } of result.notRun) rows.push(`| \`${file}\` | **not run: ${why}** | | |`);

  const summary = process.env.GITHUB_STEP_SUMMARY;
  if (summary)
    fs.appendFileSync(
      summary,
      [
        `### Fork script tests${named ? ` (${named})` : ""}`,
        "",
        ...(empty ? [`**No test runs in this part: check its argument.**`, ""] : []),
        ...(slow ? [`**Slow:** ${slow}.`, ""] : []),
        ...(stale.length
          ? [
              `**Weights to update in \`${LIST}\`** (this runner's speed: ${speed.toFixed(2)}x the weights):`,
              "",
              ...stale.map((note) => `- ${note}`),
              "",
            ]
          : []),
        "| Test | Result | Time | Weight |",
        "| --- | --- | --- | --- |",
        ...rows,
        "",
      ].join("\n") + "\n",
    );
  if (failed) process.exitCode = 1;
}

module.exports = {
  plan,
  jsCode,
  readList,
  order,
  shard,
  readShard,
  inPart,
  runSpeed,
  weightNote,
  judge,
  expectedSeconds,
  slowNote,
  findTests,
  presentTests,
  runAll,
  jobCount,
  declarations,
  ownRuns,
  runKey,
  expectedKeys,
  countTests,
  recordProblems,
  record,
  recordsTo,
  writeRecords,
  command,
  runOwn,
  main,
};

if (require.main === module)
  main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
