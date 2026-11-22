/**
 * Fork: the handoff's `command` source (part 2). The person's program runs in the project folder on
 * the wait checks' runner; its output is the handoff. Anything else is a failure, never a partial
 * handoff (docs/fork/handoff.md).
 */
import type { ProjectId, ThreadId } from "@t3tools/contracts";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as ProjectStore from "../../orchestration-v2/ProjectStore.ts";
import * as ProcessRunner from "../../processRunner.ts";
import { makeSettingsProgramRunner } from "../../waits/WaitJudges.ts";

export const HANDOFF_COMMAND_TIMEOUT_SECONDS = 60;
export const HANDOFF_COMMAND_MAX_BYTES = 32 * 1024;
/** The thread's id, as provider tools get it. */
export const HANDOFF_THREAD_ENV = "T3CODE_THREAD_ID";
export const HANDOFF_PROJECT_ENV = "T3CODE_PROJECT_ID";
/**
 * All the command gets of the server's environment, besides the two ids: enough to find programs
 * and the person's own config, not the server's tokens and keys. A program that needs more sets it
 * itself (a script outside any repository).
 */
export const HANDOFF_COMMAND_ENV_KEYS = [
  "PATH",
  "HOME",
  "USER",
  "LOGNAME",
  "SHELL",
  "LANG",
  "LC_ALL",
  "LC_CTYPE",
  "TZ",
  "TMPDIR",
  "XDG_CONFIG_HOME",
  "XDG_DATA_HOME",
  "XDG_CACHE_HOME",
  "XDG_RUNTIME_DIR",
] as const;
/**
 * POSIX `env -i` starts the command with the list only: the runner the command shares with wait
 * checks adds to the server's whole environment (upstream's `processRunner.ts`). Windows has no
 * such program, so there the command gets the whole environment.
 */
const ENV_PROGRAM = "/usr/bin/env";

/** The handoff the command printed, or why there is none. */
export type HandoffCommandResult =
  | { readonly ok: true; readonly text: string }
  | { readonly ok: false; readonly reason: string };

export interface HandoffCommandInput {
  /** From the person's handoff settings only (`resolveHandoffSettings`). */
  readonly command: ReadonlyArray<string>;
  readonly threadId: ThreadId;
  readonly projectId: ProjectId;
}

export class HandoffCommand extends Context.Service<
  HandoffCommand,
  { readonly run: (input: HandoffCommandInput) => Effect.Effect<HandoffCommandResult> }
>()("t3/fork/handoff/handoffCommand") {}

const failed = (reason: string): HandoffCommandResult => ({ ok: false, reason });

export const make = Effect.fn("HandoffCommand.make")(function* (
  options: { readonly timeoutSeconds?: number } = {},
) {
  const projects = yield* ProjectStore.ProjectStoreV2;
  const runProgram = yield* makeSettingsProgramRunner();
  const timeoutSeconds = options.timeoutSeconds ?? HANDOFF_COMMAND_TIMEOUT_SECONDS;
  const run = Effect.fn("HandoffCommand.run")(function* (input: HandoffCommandInput) {
    const project = yield* projects.get(input.projectId).pipe(Effect.option);
    if (project._tag === "None" || project.value._tag === "None") {
      return failed("its project cannot be read");
    }
    const ids = { [HANDOFF_THREAD_ENV]: input.threadId, [HANDOFF_PROJECT_ENV]: input.projectId };
    let argv = input.command;
    if ((yield* HostProcess.Platform) !== "win32") {
      // `env` reads a first word with "=" as a setting, not as the program.
      if (input.command[0]?.includes("=") === true)
        return failed('its program\'s path has an "=", which env reads as a setting');
      const server = yield* HostProcess.Environment;
      const kept = HANDOFF_COMMAND_ENV_KEYS.flatMap((key) =>
        server[key] === undefined ? [] : [`${key}=${server[key]}`],
      );
      argv = [
        ENV_PROGRAM,
        "-i",
        ...kept,
        ...Object.entries(ids).map(([key, value]) => `${key}=${value}`),
        ...input.command,
      ];
    }
    const ran = yield* runProgram({
      argv,
      timeoutSeconds,
      cwd: project.value.value.workspaceRoot,
      maxOutputBytes: HANDOFF_COMMAND_MAX_BYTES,
      env: ids,
      // Its stdin is closed: a program that reads it when given no file ends instead of waiting.
      stdin: "",
    });
    if (ran.type === "needs-shell")
      return failed("its program needs a shell (a .cmd or .bat file)");
    if (ran.type === "error") return failed(ran.message.replace(/\.$/u, "").toLowerCase());
    // Killed at its limit: what it printed so far is not a handoff.
    if (ran.result.timedOut) return failed(`timed out after ${timeoutSeconds}s`);
    const code = ran.result.code === null ? null : Number(ran.result.code);
    // 126 and 127 are how a launcher says the program could not start: on Linux the program runs
    // under `timeout`, which starts fine and then exits 127 for a missing program.
    if (code === 126 || code === 127)
      return failed(`its program could not start (exit code ${code})`);
    if (code !== 0) return failed(`exit code ${code ?? "none"}`);
    const text = ran.result.stdout.trim();
    return text === "" ? failed("it printed nothing") : ({ ok: true, text } as const);
  });
  return HandoffCommand.of({ run });
});

export const layer = Layer.effect(HandoffCommand, make()).pipe(Layer.provide(ProcessRunner.layer));
