/**
 * Fork (playtest): the one playtest service of a server. A thread's sessions claim and push in its
 * worktree, else in its project's folder, and renew and release in `<state>/playtest`; an archived
 * or deleted thread starts none. Renew and the limits
 * come with the worker.
 */
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import type { ThreadId } from "@t3tools/contracts";

import { ServerConfig } from "../config.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as ProcessRunner from "../processRunner.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as PlaytestService from "./PlaytestService.ts";
import { PLAYTEST_STORE_FILE } from "./PlaytestStore.ts";

export class Playtest extends Context.Service<Playtest, PlaytestService.PlaytestService>()(
  "t3/playtest/PlaytestLayer/Playtest",
) {}

const makePlaceOf = Effect.fn("PlaytestLayer.makePlaceOf")(function* () {
  const threads = yield* ThreadManagement.ThreadManagementService;
  const projects = yield* ProjectService.ProjectService;
  return (threadId: ThreadId): Effect.Effect<PlaytestService.PlaytestPlace> =>
    Effect.gen(function* () {
      const shell = yield* threads.getThreadShell(threadId).pipe(Effect.option);
      if (shell._tag === "None") {
        return { type: "unavailable", message: "The thread cannot be read now." } as const;
      }
      const thread = shell.value;
      if (thread === null || thread.archivedAt !== null || thread.deletedAt !== null) {
        return { type: "unavailable", message: "The thread is archived or gone." } as const;
      }
      const project = yield* projects.getById(thread.projectId).pipe(Effect.option);
      if (project._tag === "None" || project.value._tag === "None") {
        return {
          type: "unavailable",
          message: "The thread's project cannot be read now.",
        } as const;
      }
      return {
        type: "ok",
        projectId: thread.projectId,
        worktree: thread.worktreePath ?? project.value.value.workspaceRoot,
      } as const;
    });
});

export const layer = Layer.effect(
  Playtest,
  Effect.gen(function* () {
    const config = yield* ServerConfig;
    const path = yield* Path.Path;
    return yield* PlaytestService.make({
      filePath: path.join(config.stateDir, PLAYTEST_STORE_FILE),
      stepsFolder: path.join(config.stateDir, "playtest"),
      placeOf: yield* makePlaceOf(),
    });
  }),
).pipe(Layer.provide(ProcessRunner.layer));
