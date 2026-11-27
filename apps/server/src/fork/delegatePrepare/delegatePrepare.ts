// @effect-diagnostics nodeBuiltinImport:off globalDate:off globalTimers:off - a plain node script, outside the Effect runtime.
/**
 * Fork: before a delegated agent starts, a script merges the project's base, installs and runs the
 * baseline tests of the touched files, by the project's `.t3code/prepare.json`. Plain node, node
 * built-ins only (`prepareCli.ts`). Docs: docs/fork/delegate-prepare.md.
 */
import * as NodeChildProcess from "node:child_process";
import * as NodeFS from "node:fs";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";

/** The project's prepare config, relative to the checkout's top folder. */
const PREPARE_CONFIG_PATH = ".t3code/prepare.json";
/** The result, in the checkout's own git folder (per worktree), so it is never committed. */
export const RESULT_JSON_FILE = "t3-prepare.json";
export const RESULT_TEXT_FILE = "t3-prepare.md";
const DEFAULT_BASE = "origin/main";
const DEFAULT_TIMEOUT_MINUTES = 30;
const MAX_TIMEOUT_MINUTES = 240;
/** Lines of a command's output kept in the result. */
const TAIL_LINES = 40;
const TAIL_KEEP_BYTES = 64 * 1024;
const CONFIG_KEYS = new Set(["base", "install", "baseline", "timeoutMinutes"]);
const FILES_PLACEHOLDER = "{files}";

export interface PrepareConfig {
  /** The ref merged into the checkout before anything runs. */
  readonly base: string;
  /** Shell commands run in order in the checkout's top folder; the first failure stops the prepare. */
  readonly install: ReadonlyArray<string>;
  /** One shell command; `{files}` is replaced by the touched files. A red run is reported, not a failure. */
  readonly baseline: string | undefined;
  /** Per command; a command still running then is stopped and counts as failed (install) or red. */
  readonly timeoutMinutes: number;
}

export type ParsedConfig =
  | { readonly ok: true; readonly config: PrepareConfig }
  | { readonly ok: false; readonly problem: string };

const isNonEmptyString = (value: unknown): value is string =>
  typeof value === "string" && value.trim().length > 0;

/** Parses `.t3code/prepare.json`. Fails closed: an unknown key (a typo such as `instal`) is refused. */
export function parsePrepareConfig(text: string): ParsedConfig {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (error) {
    return { ok: false, problem: `not JSON (${error instanceof Error ? error.message : error})` };
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return { ok: false, problem: "must be a JSON object" };
  }
  const record = raw as Record<string, unknown>;
  const unknown = Object.keys(record).filter((key) => !CONFIG_KEYS.has(key));
  if (unknown.length > 0) {
    return { ok: false, problem: `unknown key(s) ${unknown.join(", ")}` };
  }
  if (record.base !== undefined && !isNonEmptyString(record.base)) {
    return { ok: false, problem: "base must be a non-empty string" };
  }
  if (
    record.install !== undefined &&
    (!Array.isArray(record.install) || !record.install.every(isNonEmptyString))
  ) {
    return { ok: false, problem: "install must be a list of non-empty commands" };
  }
  if (record.baseline !== undefined && !isNonEmptyString(record.baseline)) {
    return { ok: false, problem: "baseline must be a non-empty command" };
  }
  const timeout = record.timeoutMinutes;
  if (
    timeout !== undefined &&
    (typeof timeout !== "number" ||
      !Number.isFinite(timeout) ||
      timeout <= 0 ||
      timeout > MAX_TIMEOUT_MINUTES)
  ) {
    return {
      ok: false,
      problem: `timeoutMinutes must be a number above 0, at most ${MAX_TIMEOUT_MINUTES}`,
    };
  }
  return {
    ok: true,
    config: {
      base: (record.base as string | undefined)?.trim() ?? DEFAULT_BASE,
      install: (record.install as Array<string> | undefined) ?? [],
      baseline: record.baseline as string | undefined,
      timeoutMinutes: (timeout as number | undefined) ?? DEFAULT_TIMEOUT_MINUTES,
    },
  };
}

export interface CommandRun {
  readonly command: string;
  readonly exitCode: number | null;
  readonly timedOut: boolean;
  /** The last lines of its output (stdout and stderr together). */
  readonly tail: string;
}

export type MergeOutcome =
  | { readonly status: "merged" | "up-to-date"; readonly base: string }
  | {
      readonly status: "conflicts";
      readonly base: string;
      readonly conflicts: ReadonlyArray<string>;
    }
  | { readonly status: "skipped"; readonly base: string | null; readonly reason: string };

export type BaselineOutcome =
  | ({ readonly status: "green" | "red"; readonly files: ReadonlyArray<string> } & CommandRun)
  | { readonly status: "skipped"; readonly reason: string };

export type FailedStep = "checkout" | "clone" | "branch" | "config" | "merge" | "install";

export interface PrepareResult {
  readonly version: 1;
  /**
   * `running` is written first, so a prepare stopped half-way never leaves an older run's result
   * looking current. `no-config`: the project has no prepare config; nothing but the asked-for
   * clone and branch was done.
   */
  readonly status: "running" | "prepared" | "no-config" | "failed";
  readonly checkout: string;
  readonly branch: string | null;
  readonly head: string | null;
  readonly startedAt: string;
  readonly finishedAt: string | null;
  readonly merge: MergeOutcome | null;
  readonly install: ReadonlyArray<CommandRun>;
  readonly baseline: BaselineOutcome | null;
  readonly failure: {
    readonly step: FailedStep;
    readonly message: string;
    readonly tail?: string;
  } | null;
  readonly notes: ReadonlyArray<string>;
}

export interface PrepareOptions {
  /** The checkout to prepare, or where `clone` puts it. */
  readonly dir: string;
  /** Clone this URL into `dir` when it does not exist; reuse `dir` when it is a clone of it. */
  readonly clone?: string;
  /** Switch to this branch: the local one, else the remote's, else a new one from the base. */
  readonly branch?: string;
  /** Overrides the config's base (also the start of a new branch when there is no config). */
  readonly base?: string;
  /** Files the work will touch, besides the ones the branch already changed against the base. */
  readonly paths?: ReadonlyArray<string>;
  readonly now?: () => Date;
  /** Progress and command output, as it comes (the CLI sends it to stderr). */
  readonly log?: (text: string) => void;
}

export interface PrepareOutcome {
  readonly result: PrepareResult;
  /** The summary for the brief (also written as `t3-prepare.md`). */
  readonly text: string;
  /** 0 prepared (red baseline and conflicts included) or no config; 1 failed. */
  readonly exitCode: 0 | 1;
}

interface GitRun {
  readonly code: number | null;
  readonly stdout: string;
  readonly stderr: string;
}

const git = (cwd: string, args: ReadonlyArray<string>): GitRun => {
  const run = NodeChildProcess.spawnSync("git", args, {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    maxBuffer: 16 * 1024 * 1024,
  });
  return {
    code: run.error === undefined ? run.status : null,
    stdout: run.stdout ?? "",
    stderr: run.stderr ?? run.error?.message ?? "",
  };
};

const gitOk = (cwd: string, args: ReadonlyArray<string>) => git(cwd, args).code === 0;
const gitLine = (cwd: string, args: ReadonlyArray<string>): string | null => {
  const run = git(cwd, args);
  return run.code === 0 ? run.stdout.trim() : null;
};
const gitLines = (cwd: string, args: ReadonlyArray<string>): Array<string> =>
  (gitLine(cwd, args) ?? "").split("\n").filter((line) => line.length > 0);

const lastLines = (text: string): string =>
  text.trimEnd().split("\n").slice(-TAIL_LINES).join("\n");

/** POSIX shell quoting, for the `{files}` list. */
export const shellQuote = (value: string): string =>
  /^[\w./@%+=:,-]+$/.test(value) ? value : `'${value.replaceAll("'", "'\\''")}'`;

/**
 * Runs one shell command in its own process group (POSIX), so a timeout stops what it started.
 * Output goes to `log` as it comes; its tail is kept.
 */
function runCommand(
  command: string,
  cwd: string,
  timeoutMs: number,
  log: (text: string) => void,
): Promise<CommandRun> {
  // oxlint-disable-next-line t3code/no-global-process-runtime -- A plain node script, no Effect runtime.
  const windows = NodeOS.platform() === "win32";
  return new Promise((resolve) => {
    let kept = "";
    let timedOut = false;
    const keep = (chunk: Buffer) => {
      const text = chunk.toString("utf8");
      log(text);
      kept = (kept + text).slice(-TAIL_KEEP_BYTES);
    };
    const child = NodeChildProcess.spawn(command, {
      cwd,
      shell: true,
      stdio: ["ignore", "pipe", "pipe"],
      detached: !windows,
    });
    const stop = () => {
      if (child.pid === undefined) return;
      try {
        if (windows) child.kill("SIGKILL");
        else process.kill(-child.pid, "SIGKILL");
      } catch {
        // Already gone.
      }
    };
    const timer = setTimeout(() => {
      timedOut = true;
      stop();
    }, timeoutMs);
    child.stdout.on("data", keep);
    child.stderr.on("data", keep);
    child.on("error", (error) => {
      kept += `\n${error.message}`;
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      // What the command left running in its group is stopped too.
      if (!timedOut) stop();
      resolve({ command, exitCode: timedOut ? null : code, timedOut, tail: lastLines(kept) });
    });
  });
}

const nothingDone = (dir: string, startedAt: string): PrepareResult => ({
  version: 1,
  status: "running",
  checkout: dir,
  branch: null,
  head: null,
  startedAt,
  finishedAt: null,
  merge: null,
  install: [],
  baseline: null,
  failure: null,
  notes: [],
});

const sameRemote = (left: string, right: string) =>
  left.replace(/\/+$/, "").replace(/\.git$/, "") ===
  right.replace(/\/+$/, "").replace(/\.git$/, "");

const hasLocalChanges = (top: string) =>
  (gitLine(top, ["status", "--porcelain", "--untracked-files=no"]) ?? "x").length > 0;

/** Fetches the remote a base like `origin/main` names, when there is one. False: it failed. */
const fetchBaseRemote = (
  top: string,
  base: string,
): { fetched: boolean; remote: string | null } => {
  const remote = gitLines(top, ["remote"]).find((name) => base.startsWith(`${name}/`)) ?? null;
  if (remote === null) return { fetched: true, remote: null };
  return { fetched: gitOk(top, ["fetch", "--quiet", remote]), remote };
};

const resolves = (top: string, ref: string) =>
  gitOk(top, ["rev-parse", "--verify", "--quiet", `${ref}^{commit}`]);

export async function runPrepare(options: PrepareOptions): Promise<PrepareOutcome> {
  const now = options.now ?? (() => new Date());
  const log = options.log ?? (() => {});
  const dir = NodePath.resolve(options.dir);
  let result: PrepareResult = nothingDone(dir, now().toISOString());
  let resultDir: string | null = null;
  const notes: Array<string> = [];

  const save = (next: PrepareResult): PrepareOutcome | null => {
    result = next;
    const text = formatPrepareResult(next, resultDir);
    if (resultDir !== null) {
      writeAtomic(NodePath.join(resultDir, RESULT_JSON_FILE), `${JSON.stringify(next, null, 2)}\n`);
      writeAtomic(NodePath.join(resultDir, RESULT_TEXT_FILE), text);
    }
    return next.status === "running"
      ? null
      : { result: next, text, exitCode: next.status === "failed" ? 1 : 0 };
  };
  const fail = (step: FailedStep, message: string, tail?: string): PrepareOutcome =>
    save({
      ...result,
      status: "failed",
      finishedAt: now().toISOString(),
      failure: tail === undefined ? { step, message } : { step, message, tail },
      notes: [...notes],
    })!;

  // 1. The checkout.
  if (options.clone !== undefined) {
    if (!NodeFS.existsSync(dir)) {
      log(`clone ${options.clone} -> ${dir}\n`);
      NodeFS.mkdirSync(NodePath.dirname(dir), { recursive: true });
      const cloned = git(NodePath.dirname(dir), ["clone", "--quiet", options.clone, dir]);
      if (cloned.code !== 0) return fail("clone", "git clone failed", lastLines(cloned.stderr));
    } else {
      const origin = gitLine(dir, ["remote", "get-url", "origin"]);
      if (origin === null || !sameRemote(origin, options.clone)) {
        return fail(
          "clone",
          `${dir} exists and is not a clone of ${options.clone}; nothing was changed there`,
        );
      }
      notes.push("reused the existing clone");
    }
  }
  const top = NodeFS.existsSync(dir) ? gitLine(dir, ["rev-parse", "--show-toplevel"]) : null;
  if (top === null) return fail("checkout", `${dir} is not a git checkout`);
  resultDir = gitLine(top, ["rev-parse", "--absolute-git-dir"]);
  result = { ...result, checkout: top };
  save(result);

  // A merge or rebase someone left here is theirs to finish: never undo it.
  for (const marker of ["MERGE_HEAD", "rebase-merge", "rebase-apply"]) {
    if (resultDir !== null && NodeFS.existsSync(NodePath.join(resultDir, marker))) {
      return fail(
        "checkout",
        `a merge or rebase is in progress here (${marker}); finish or abort it first`,
      );
    }
  }

  // 2. The config, as the checkout has it now.
  const configPath = NodePath.join(top, PREPARE_CONFIG_PATH);
  let config: PrepareConfig | null = null;
  if (NodeFS.existsSync(configPath)) {
    const parsed = parsePrepareConfig(NodeFS.readFileSync(configPath, "utf8"));
    if (!parsed.ok) return fail("config", `${PREPARE_CONFIG_PATH}: ${parsed.problem}`);
    config = parsed.config;
  }
  const base = options.base ?? config?.base ?? DEFAULT_BASE;

  // 3. The branch.
  const fetch = fetchBaseRemote(top, base);
  if (!fetch.fetched) notes.push(`could not fetch ${fetch.remote}; the base may be behind`);
  if (options.branch !== undefined) {
    const branch = options.branch;
    const current = gitLine(top, ["symbolic-ref", "--quiet", "--short", "HEAD"]);
    if (current !== branch) {
      if (hasLocalChanges(top)) {
        return fail("branch", `local changes here; not switching to ${branch}`);
      }
      const remoteBranch = fetch.remote === null ? null : `${fetch.remote}/${branch}`;
      const args = gitOk(top, ["show-ref", "--verify", "--quiet", `refs/heads/${branch}`])
        ? ["checkout", "--quiet", branch]
        : remoteBranch !== null && resolves(top, remoteBranch)
          ? ["checkout", "--quiet", "-b", branch, "--track", remoteBranch]
          : resolves(top, base)
            ? ["checkout", "--quiet", "--no-track", "-b", branch, base]
            : null;
      if (args === null)
        return fail("branch", `${branch} is not here and the base ${base} is not found`);
      const switched = git(top, args);
      if (switched.code !== 0) {
        return fail("branch", `could not switch to ${branch}`, lastLines(switched.stderr));
      }
    }
  }
  const describe = () => ({
    branch: gitLine(top, ["symbolic-ref", "--quiet", "--short", "HEAD"]),
    head: gitLine(top, ["rev-parse", "HEAD"]),
  });
  result = { ...result, ...describe() };

  if (config === null) {
    notes.push(`no ${PREPARE_CONFIG_PATH}: no merge, install or baseline`);
    return save({ ...result, status: "no-config", finishedAt: now().toISOString(), notes })!;
  }

  // 4. The base merged in.
  let merge: MergeOutcome;
  if (!resolves(top, base)) {
    merge = { status: "skipped", base, reason: `base ${base} not found` };
  } else if (hasLocalChanges(top)) {
    merge = { status: "skipped", base, reason: "local changes here" };
  } else {
    const before = gitLine(top, ["rev-parse", "HEAD"]);
    log(`merge ${base}\n`);
    const merged = git(top, ["merge", "--no-edit", "--quiet", base]);
    if (merged.code === 0) {
      merge = {
        status: gitLine(top, ["rev-parse", "HEAD"]) === before ? "up-to-date" : "merged",
        base,
      };
    } else {
      const conflicts = gitLines(top, ["diff", "--name-only", "--diff-filter=U"]);
      // Undo it either way: the agent starts on a clean checkout and merges with thought.
      git(top, ["merge", "--abort"]);
      if (conflicts.length === 0) {
        result = { ...result, merge: { status: "skipped", base, reason: "merge failed" } };
        return fail(
          "merge",
          `git merge ${base} failed`,
          lastLines(`${merged.stdout}${merged.stderr}`),
        );
      }
      merge = { status: "conflicts", base, conflicts };
    }
  }
  result = { ...result, ...describe(), merge };
  save(result);

  // 5. Install.
  const timeoutMs = config.timeoutMinutes * 60_000;
  const installs: Array<CommandRun> = [];
  for (const command of config.install) {
    log(`$ ${command}\n`);
    const run = await runCommand(command, top, timeoutMs, log);
    installs.push(run);
    result = { ...result, install: [...installs] };
    if (run.exitCode !== 0) {
      return fail(
        "install",
        run.timedOut
          ? `\`${command}\` was stopped after ${config.timeoutMinutes} min`
          : `\`${command}\` exited ${run.exitCode ?? "without a code"}`,
        run.tail,
      );
    }
  }

  // 6. Baseline tests of the touched files.
  const files = touchedFiles(top, base, options.paths ?? []);
  let baseline: BaselineOutcome;
  if (config.baseline === undefined) {
    baseline = { status: "skipped", reason: "no baseline command" };
  } else if (config.baseline.includes(FILES_PLACEHOLDER) && files.length === 0) {
    baseline = { status: "skipped", reason: "no touched files" };
  } else {
    const command = config.baseline.replaceAll(FILES_PLACEHOLDER, files.map(shellQuote).join(" "));
    log(`$ ${command}\n`);
    const run = await runCommand(command, top, timeoutMs, log);
    baseline = { ...run, command, status: run.exitCode === 0 ? "green" : "red", files };
  }
  return save({
    ...result,
    status: "prepared",
    finishedAt: now().toISOString(),
    baseline,
    notes,
  })!;
}

/** The branch's own changes against the base, plus `extra`, that still exist (relative to `top`). */
function touchedFiles(top: string, base: string, extra: ReadonlyArray<string>): Array<string> {
  const changed = resolves(top, base)
    ? gitLines(top, ["diff", "--name-only", "--no-renames", `${base}...HEAD`])
    : [];
  const asked = extra.map((path) => NodePath.relative(top, NodePath.resolve(top, path)));
  return [...new Set([...changed, ...asked])]
    .filter((path) => !path.startsWith("..") && NodeFS.existsSync(NodePath.join(top, path)))
    .sort();
}

const writeAtomic = (path: string, text: string) => {
  const temporary = `${path}.${process.pid}.tmp`;
  NodeFS.writeFileSync(temporary, text);
  NodeFS.renameSync(temporary, path);
};

const fenced = (text: string) => `\n\n\`\`\`\n${text.replaceAll("```", "'''")}\n\`\`\``;

/** The summary for the brief: short, the facts an agent needs before it starts. */
function formatPrepareResult(result: PrepareResult, resultDir: string | null): string {
  const lines: Array<string> = [];
  const at = [
    result.branch === null ? null : `branch ${result.branch}`,
    result.head === null ? null : `at ${result.head.slice(0, 10)}`,
  ].filter((part) => part !== null);
  const where = at.length === 0 ? result.checkout : `${result.checkout} (${at.join(", ")})`;
  const title =
    result.status === "failed"
      ? `Prepare FAILED at ${result.failure?.step}: ${result.failure?.message}`
      : result.status === "running"
        ? "Prepare is running (or was stopped half-way)"
        : result.status === "no-config"
          ? "Prepare: no project config, nothing prepared"
          : "Prepared before you started";
  lines.push(`${title}`, "", `- Checkout: ${where}`);
  const merge = result.merge;
  if (merge !== null) {
    lines.push(
      merge.status === "conflicts"
        ? `- Merge of ${merge.base}: CONFLICTS in ${merge.conflicts.join(", ")}. The merge was undone; merge it yourself.`
        : merge.status === "skipped"
          ? `- Merge of ${merge.base ?? "the base"}: skipped (${merge.reason})`
          : `- Merge of ${merge.base}: ${merge.status === "merged" ? "merged" : "already up to date"}`,
    );
  }
  if (result.install.length > 0) {
    const passed = result.install.filter((run) => run.exitCode === 0).length;
    lines.push(`- Install: ${passed} of ${result.install.length} command(s) passed`);
  }
  const baseline = result.baseline;
  if (baseline !== null) {
    lines.push(
      baseline.status === "skipped"
        ? `- Baseline tests: skipped (${baseline.reason})`
        : baseline.status === "green"
          ? `- Baseline tests: green on ${baseline.files.length} touched file(s): \`${baseline.command}\``
          : `- Baseline tests: RED before any change (${baseline.timedOut ? "stopped at the time limit" : `exit ${baseline.exitCode}`}): \`${baseline.command}\`. These were red already; do not "fix" them as yours unless the task says so.${fenced(baseline.tail)}`,
    );
  }
  for (const note of result.notes) lines.push(`- Note: ${note}`);
  if (result.failure?.tail !== undefined && result.failure.tail.length > 0) {
    lines.push(`- Last output:${fenced(result.failure.tail)}`);
  }
  if (resultDir !== null)
    lines.push(`- Result file: ${NodePath.join(resultDir, RESULT_JSON_FILE)}`);
  return `${lines.join("\n")}\n`;
}
