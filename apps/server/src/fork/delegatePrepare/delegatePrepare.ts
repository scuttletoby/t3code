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
  /** The signal that ended it, when one did. */
  readonly signal: string | null;
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

export type FailedStep =
  | "checkout"
  | "clone"
  | "branch"
  | "config"
  | "merge"
  | "install"
  | "baseline";

export interface PrepareResult {
  readonly version: 1;
  /**
   * `running` is written first, so a prepare stopped half-way never leaves an older run's result
   * looking current; `pid` tells a reader whether that run is still alive. `no-config`: the project
   * has no prepare config; nothing but the asked-for clone and branch was done.
   */
  readonly status: "running" | "prepared" | "no-config" | "failed";
  readonly pid: number;
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
  /**
   * The CLI: on SIGINT, SIGTERM or SIGHUP, stop the running command's process group, write a
   * `failed` result and exit 128 + the signal's number.
   */
  readonly stopOnSignals?: boolean;
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

const LOCK_FILE = "t3-prepare.lock";
const STOP_SIGNALS = ["SIGINT", "SIGTERM", "SIGHUP"] as const;
/** After a command exits, how long a process that left its group may hold its output open. */
const EXIT_GRACE_MS = 1000;
/** Shell exit codes for a command that is not executable or not found. */
const COULD_NOT_RUN = new Set([126, 127]);

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
/** Paths from a `-z` listing: raw names, never C-quoted (non-ASCII ones included). */
const gitPaths = (cwd: string, args: ReadonlyArray<string>): Array<string> => {
  const run = git(cwd, args);
  return run.code === 0 ? run.stdout.split("\0").filter((path) => path.length > 0) : [];
};

const lastLines = (text: string): string =>
  text.trimEnd().split("\n").slice(-TAIL_LINES).join("\n");

/** POSIX shell quoting, for the `{files}` list. */
export const shellQuote = (value: string): string =>
  /^[\w./@%+=:,-]+$/.test(value) ? value : `'${value.replaceAll("'", "'\\''")}'`;

/** A touched file as the baseline command gets it: never read as an option (`-x` -> `./-x`). */
const fileArgument = (path: string) => shellQuote(path.startsWith("-") ? `./${path}` : path);

const isAlive = (pid: number) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
};

/**
 * Runs one shell command in its own process group (POSIX), so a timeout or a stopped prepare
 * stops what it started. Output goes to `log` as it comes; its tail is kept. `track` gets the
 * group's stop function while the command runs.
 */
function runCommand(
  command: string,
  cwd: string,
  timeoutMs: number,
  log: (text: string) => void,
  track: (stop: (() => void) | null) => void,
): Promise<CommandRun> {
  // oxlint-disable-next-line t3code/no-global-process-runtime -- A plain node script, no Effect runtime.
  const windows = NodeOS.platform() === "win32";
  return new Promise((resolve) => {
    let kept = "";
    let timedOut = false;
    let done = false;
    let grace: ReturnType<typeof setTimeout> | undefined;
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
    track(stop);
    const timer = setTimeout(() => {
      timedOut = true;
      stop();
    }, timeoutMs);
    const finish = (code: number | null, signal: string | null) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      clearTimeout(grace);
      track(null);
      // What the command left running in its group is stopped too.
      if (!timedOut) stop();
      resolve({
        command,
        exitCode: timedOut ? null : code,
        timedOut,
        signal: timedOut ? null : signal,
        tail: lastLines(kept),
      });
    };
    child.stdout.on("data", keep);
    child.stderr.on("data", keep);
    child.on("error", (error) => {
      kept += `\n${error.message}`;
    });
    child.on("close", (code, signal) => finish(code, signal));
    // A process that left the group (setsid) can hold the output open after the command exited.
    child.on("exit", (code, signal) => {
      grace = setTimeout(() => {
        child.stdout.destroy();
        child.stderr.destroy();
        finish(code, signal);
      }, EXIT_GRACE_MS);
    });
  });
}

const nothingDone = (dir: string, startedAt: string): PrepareResult => ({
  version: 1,
  status: "running",
  pid: process.pid,
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

const resolves = (top: string, ref: string) =>
  gitOk(top, ["rev-parse", "--verify", "--quiet", `${ref}^{commit}`]);

type ConfigRead =
  | { readonly ok: true; readonly config: PrepareConfig | null }
  | { readonly ok: false; readonly problem: string };

/** The config in the checkout's work tree as it is now; null when the project has none. */
const readConfig = (top: string): ConfigRead => {
  const path = NodePath.join(top, PREPARE_CONFIG_PATH);
  if (!NodeFS.existsSync(path)) return { ok: true, config: null };
  const parsed = parsePrepareConfig(NodeFS.readFileSync(path, "utf8"));
  return parsed.ok ? parsed : { ok: false, problem: `${PREPARE_CONFIG_PATH}: ${parsed.problem}` };
};

type Lock =
  | { readonly ok: true; readonly stalePid: number | null; readonly release: () => void }
  | { readonly ok: false; readonly pid: number };

/** One prepare per checkout. A lock whose process is gone is taken over and reported. */
const takeLock = (path: string): Lock => {
  let stalePid: number | null = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      NodeFS.writeFileSync(path, `${process.pid}\n`, { flag: "wx" });
      const release = () => {
        try {
          if (NodeFS.readFileSync(path, "utf8").trim() === String(process.pid)) {
            NodeFS.unlinkSync(path);
          }
        } catch {
          // Already gone.
        }
      };
      return { ok: true, stalePid, release };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
    }
    let pid = Number.NaN;
    try {
      pid = Number.parseInt(NodeFS.readFileSync(path, "utf8"), 10);
    } catch {
      // Removed meanwhile: try again.
    }
    if (Number.isInteger(pid) && pid > 0 && isAlive(pid)) return { ok: false, pid };
    stalePid = Number.isInteger(pid) && pid > 0 ? pid : null;
    NodeFS.rmSync(path, { force: true });
  }
  return { ok: false, pid: Number.NaN };
};

export async function runPrepare(options: PrepareOptions): Promise<PrepareOutcome> {
  const now = options.now ?? (() => new Date());
  const log = options.log ?? (() => {});
  const dir = NodePath.resolve(options.dir);
  let result: PrepareResult = nothingDone(dir, now().toISOString());
  let resultDir: string | null = null;
  const notes: Array<string> = [];
  let step: FailedStep = "checkout";
  let stopCommand: (() => void) | null = null;
  let release = () => {};

  const save = (next: PrepareResult): PrepareOutcome | null => {
    result = next;
    const text = formatPrepareResult(next, resultDir);
    if (resultDir !== null) {
      // The JSON is the one that counts: written last, so it never shows a step the text lacks.
      writeAtomic(NodePath.join(resultDir, RESULT_TEXT_FILE), text);
      writeAtomic(NodePath.join(resultDir, RESULT_JSON_FILE), `${JSON.stringify(next, null, 2)}\n`);
    }
    return next.status === "running"
      ? null
      : { result: next, text, exitCode: next.status === "failed" ? 1 : 0 };
  };
  const fail = (failed: FailedStep, message: string, tail?: string): PrepareOutcome =>
    save({
      ...result,
      status: "failed",
      finishedAt: now().toISOString(),
      failure: tail === undefined ? { step: failed, message } : { step: failed, message, tail },
      notes: [...notes],
    })!;

  const onSignal = (signal: (typeof STOP_SIGNALS)[number]) => {
    stopCommand?.();
    fail(step, `the prepare was stopped by ${signal} during ${step}`);
    release();
    process.exit(128 + (NodeOS.constants.signals[signal] ?? 0));
  };
  if (options.stopOnSignals === true) {
    for (const signal of STOP_SIGNALS) process.on(signal, onSignal);
  }
  try {
    return await prepareSteps();
  } finally {
    for (const signal of STOP_SIGNALS) process.off(signal, onSignal);
    release();
  }

  async function prepareSteps(): Promise<PrepareOutcome> {
    // 1. The checkout. A new clone is made beside it and renamed into place, so a clone stopped
    // half-way is never at `dir`.
    if (options.clone !== undefined) {
      step = "clone";
      let cloned = false;
      if (!NodeFS.existsSync(dir)) {
        log(`clone ${options.clone} -> ${dir}\n`);
        NodeFS.mkdirSync(NodePath.dirname(dir), { recursive: true });
        const partial = `${dir}.t3-prepare-clone-${process.pid}`;
        NodeFS.rmSync(partial, { recursive: true, force: true });
        const run = git(NodePath.dirname(dir), ["clone", "--quiet", "--", options.clone, partial]);
        if (run.code !== 0) {
          NodeFS.rmSync(partial, { recursive: true, force: true });
          return fail("clone", "git clone failed", lastLines(run.stderr));
        }
        try {
          NodeFS.renameSync(partial, dir);
          cloned = true;
        } catch (error) {
          NodeFS.rmSync(partial, { recursive: true, force: true });
          // Another prepare cloned into the same folder first: check it and reuse it below.
          if (!NodeFS.existsSync(dir)) {
            return fail("clone", `could not move the clone into place: ${String(error)}`);
          }
        }
      }
      if (!cloned) {
        const origin = gitLine(dir, ["remote", "get-url", "origin"]);
        if (origin === null || !sameRemote(origin, options.clone)) {
          return fail(
            "clone",
            `${dir} exists and is not a clone of ${options.clone}; nothing was changed there`,
          );
        }
        if (!resolves(dir, "HEAD")) {
          return fail(
            "clone",
            `${dir} is a clone with no commit checked out (a clone stopped half-way?); remove it and run again`,
          );
        }
        notes.push("reused the existing clone");
      }
    }
    step = "checkout";
    const top = NodeFS.existsSync(dir) ? gitLine(dir, ["rev-parse", "--show-toplevel"]) : null;
    if (top === null) return fail("checkout", `${dir} is not a git checkout`);
    const gitDir = gitLine(top, ["rev-parse", "--absolute-git-dir"]);
    if (gitDir === null) return fail("checkout", `${top} has no git folder`);
    result = { ...result, checkout: top };

    const lock = takeLock(NodePath.join(gitDir, LOCK_FILE));
    if (!lock.ok) {
      // The other prepare's result stays as it is: nothing is written.
      const refused: PrepareResult = {
        ...result,
        status: "failed",
        finishedAt: now().toISOString(),
        failure: {
          step: "checkout",
          message: `another prepare (pid ${lock.pid}) is running on this checkout`,
        },
      };
      return { result: refused, text: formatPrepareResult(refused, null), exitCode: 1 };
    }
    release = lock.release;
    resultDir = gitDir;
    if (lock.stalePid !== null) {
      notes.push(`an earlier prepare (pid ${lock.stalePid}) was stopped before it finished`);
    }
    save({ ...result, notes: [...notes] });

    // A merge or rebase someone left here is theirs to finish: never undo it.
    for (const marker of ["MERGE_HEAD", "rebase-merge", "rebase-apply"]) {
      if (NodeFS.existsSync(NodePath.join(gitDir, marker))) {
        return fail(
          "checkout",
          `a merge or rebase is in progress here (${marker}); finish or abort it first`,
        );
      }
    }

    // 2. The branch. The config is not read yet: it is the branch's own. Only a new branch's start
    // comes from the config the checkout has now.
    step = "branch";
    const remotes = gitLines(top, ["remote"]);
    const remoteOf = (ref: string) => remotes.find((name) => ref.startsWith(`${name}/`)) ?? null;
    const fetched = new Set<string>();
    const fetchRemote = (remote: string | null) => {
      if (remote === null || fetched.has(remote)) return;
      fetched.add(remote);
      if (!gitOk(top, ["fetch", "--quiet", remote])) {
        notes.push(`could not fetch ${remote}; its branches may be behind`);
      }
    };
    const peeked = readConfig(top);
    const startBase = options.base ?? (peeked.ok ? peeked.config?.base : undefined) ?? DEFAULT_BASE;
    const branchRemote =
      remoteOf(startBase) ?? (remotes.includes("origin") ? "origin" : (remotes[0] ?? null));
    fetchRemote(branchRemote);
    if (options.branch !== undefined) {
      const branch = options.branch;
      const current = gitLine(top, ["symbolic-ref", "--quiet", "--short", "HEAD"]);
      if (current !== branch) {
        if (hasLocalChanges(top)) {
          return fail("branch", `local changes here; not switching to ${branch}`);
        }
        if (
          current === null &&
          resolves(top, "HEAD") &&
          gitLines(top, ["for-each-ref", "--contains", "HEAD", "refs/heads", "refs/remotes"])
            .length === 0
        ) {
          return fail(
            "branch",
            `HEAD is detached at ${gitLine(top, ["rev-parse", "--short", "HEAD"])} with commits on no branch; not switching to ${branch}, which would leave them behind. Put them on a branch first.`,
          );
        }
        const remoteBranch = branchRemote === null ? null : `${branchRemote}/${branch}`;
        const args = gitOk(top, ["show-ref", "--verify", "--quiet", `refs/heads/${branch}`])
          ? ["checkout", "--quiet", branch]
          : remoteBranch !== null && resolves(top, remoteBranch)
            ? ["checkout", "--quiet", "-b", branch, "--track", remoteBranch]
            : resolves(top, startBase)
              ? ["checkout", "--quiet", "--no-track", "-b", branch, startBase]
              : null;
        if (args === null) {
          return fail("branch", `${branch} is not here and the base ${startBase} is not found`);
        }
        const switched = git(top, args);
        if (switched.code !== 0) {
          return fail("branch", `could not switch to ${branch}`, lastLines(switched.stderr));
        }
      }
    }
    const updated = bringUpToRemote(top, branchRemote, fetchRemote);
    if (updated.failure !== null) return fail("branch", updated.failure);
    if (updated.note !== null) notes.push(updated.note);
    const describe = () => ({
      branch: gitLine(top, ["symbolic-ref", "--quiet", "--short", "HEAD"]),
      head: gitLine(top, ["rev-parse", "HEAD"]),
    });
    result = { ...result, ...describe() };

    // 3. The config, as the branch being prepared has it.
    step = "config";
    const read = readConfig(top);
    if (!read.ok) return fail("config", read.problem);
    const config = read.config;
    if (config === null) {
      notes.push(`no ${PREPARE_CONFIG_PATH}: no merge, install or baseline`);
      return save({ ...result, status: "no-config", finishedAt: now().toISOString(), notes })!;
    }
    const base = options.base ?? config.base;
    fetchRemote(remoteOf(base));

    // 4. The base merged in.
    step = "merge";
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
        const conflicts = gitPaths(top, ["diff", "--name-only", "-z", "--diff-filter=U"]);
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
    result = { ...result, ...describe(), merge, notes: [...notes] };
    save(result);

    // 5. Install.
    step = "install";
    const timeoutMs = config.timeoutMinutes * 60_000;
    const track = (stop: (() => void) | null) => {
      stopCommand = stop;
    };
    const installs: Array<CommandRun> = [];
    for (const command of config.install) {
      log(`$ ${command}\n`);
      const run = await runCommand(command, top, timeoutMs, log, track);
      installs.push(run);
      result = { ...result, install: [...installs] };
      if (run.exitCode !== 0) {
        return fail(
          "install",
          run.timedOut
            ? `\`${command}\` was stopped after ${config.timeoutMinutes} min`
            : `\`${command}\` exited ${run.exitCode ?? `on ${run.signal ?? "a signal"}`}`,
          run.tail,
        );
      }
    }

    // 6. Baseline tests of the touched files.
    step = "baseline";
    const files = touchedFiles(top, base, options.paths ?? []);
    let baseline: BaselineOutcome;
    if (config.baseline === undefined) {
      baseline = { status: "skipped", reason: "no baseline command" };
    } else if (config.baseline.includes(FILES_PLACEHOLDER) && files.length === 0) {
      baseline = { status: "skipped", reason: "no touched files" };
    } else {
      const list = files.map(fileArgument).join(" ");
      // A function, so `$&` or `$'` in a file name is never read as a replacement pattern.
      const command = config.baseline.replaceAll(FILES_PLACEHOLDER, () => list);
      log(`$ ${command}\n`);
      const run = await runCommand(command, top, timeoutMs, log, track);
      if (run.exitCode !== null && COULD_NOT_RUN.has(run.exitCode)) {
        return fail(
          "baseline",
          `the baseline command could not run (exit ${run.exitCode}: not found or not executable), so no tests ran: \`${command}\``,
          run.tail,
        );
      }
      if (run.exitCode === null && !run.timedOut) {
        return fail(
          "baseline",
          `the baseline command was killed${run.signal === null ? "" : ` by ${run.signal}`} before it finished, so no result came back: \`${command}\``,
          run.tail,
        );
      }
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
}

/**
 * Brings the checked-out branch up to the same name on `remote`: fast-forwards a clean branch that
 * is behind; refuses a diverged one, or one behind with local changes, so a prepare never reports
 * stale code as prepared. A configured upstream is not used: it is often the base, which the merge
 * step brings in.
 */
function bringUpToRemote(
  top: string,
  remote: string | null,
  fetchRemote: (remote: string | null) => void,
): { readonly failure: string | null; readonly note: string | null } {
  const none = { failure: null, note: null };
  const branch = gitLine(top, ["symbolic-ref", "--quiet", "--short", "HEAD"]);
  if (branch === null) return none;
  if (remote === null) return none;
  const upstream = `${remote}/${branch}`;
  fetchRemote(remote);
  if (!resolves(top, upstream) || !resolves(top, "HEAD")) return none;
  const counts = (gitLine(top, ["rev-list", "--left-right", "--count", `HEAD...${upstream}`]) ?? "")
    .split(/\s+/)
    .map(Number);
  const ahead = counts[0] ?? 0;
  const behind = counts[1] ?? 0;
  if (behind === 0) {
    return ahead === 0
      ? none
      : { failure: null, note: `${branch} has ${ahead} commit(s) not on ${upstream}` };
  }
  if (ahead > 0) {
    return {
      failure: `${branch} and ${upstream} have diverged (${ahead} local, ${behind} remote commit(s)); merge or rebase it first`,
      note: null,
    };
  }
  if (hasLocalChanges(top)) {
    return {
      failure: `${branch} is ${behind} commit(s) behind ${upstream} and has local changes; not updated`,
      note: null,
    };
  }
  const forwarded = git(top, ["merge", "--ff-only", "--quiet", upstream]);
  if (forwarded.code !== 0) {
    return {
      failure: `could not fast-forward ${branch} to ${upstream}: ${lastLines(forwarded.stderr)}`,
      note: null,
    };
  }
  return { failure: null, note: `fast-forwarded ${branch} by ${behind} commit(s) to ${upstream}` };
}

/** The branch's own changes against the base, plus `extra`, that still exist (relative to `top`). */
function touchedFiles(top: string, base: string, extra: ReadonlyArray<string>): Array<string> {
  const changed = resolves(top, base)
    ? gitPaths(top, ["diff", "--name-only", "-z", "--no-renames", `${base}...HEAD`])
    : [];
  const asked = extra.map((path) => NodePath.relative(top, NodePath.resolve(top, path)));
  const inside = (path: string) =>
    path.length > 0 &&
    path !== ".." &&
    !path.startsWith(`..${NodePath.sep}`) &&
    !NodePath.isAbsolute(path);
  return [...new Set([...changed, ...asked])]
    .filter((path) => inside(path) && NodeFS.existsSync(NodePath.join(top, path)))
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
