/**
 * Fork: the handoff's `command` source (part 2). The person's program runs in the project folder on
 * the wait checks' runner; its output is the handoff. Anything else is a failure, never a partial
 * handoff (docs/fork/handoff.md).
 */
import type { ProjectId, ThreadId } from "@t3tools/contracts";
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
    const ran = yield* runProgram({
      argv: input.command,
      timeoutSeconds,
      cwd: project.value.value.workspaceRoot,
      maxOutputBytes: HANDOFF_COMMAND_MAX_BYTES,
      env: { [HANDOFF_THREAD_ENV]: input.threadId, [HANDOFF_PROJECT_ENV]: input.projectId },
    });
    if (ran.type === "needs-shell")
      return failed("its program needs a shell (a .cmd or .bat file)");
    if (ran.type === "error") return failed(ran.message.replace(/\.$/u, "").toLowerCase());
    // Killed at its limit: what it printed so far is not a handoff.
    if (ran.result.timedOut) return failed(`timed out after ${timeoutSeconds}s`);
    if (ran.result.code === null || Number(ran.result.code) !== 0) {
      return failed(`exit code ${ran.result.code ?? "none"}`);
    }
    const text = ran.result.stdout.trim();
    return text === "" ? failed("it printed nothing") : ({ ok: true, text } as const);
  });
  return HandoffCommand.of({ run });
});

export const layer = Layer.effect(HandoffCommand, make()).pipe(Layer.provide(ProcessRunner.layer));
