/**
 * Fork: off means off. `lens_start` is listed and callable only for a thread whose project's review rules are on
 * (Settings: review rules, or the project's own); the T3 tool rule (fork/mcpT3ToolRule.ts) asks this on every
 * `tools/list` and `tools/call`, and `tool_search` keeps it out unless the thread's last check found the rules on.
 */
import { effectiveReviewRules, type ThreadId } from "@t3tools/contracts";
import { resolveProjectSettings } from "@t3tools/shared/projectSettings";
import * as Clock from "effect/Clock";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as SqlClient from "effect/sql/SqlClient";

import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ServerSettingsService from "../../serverSettings.ts";

export const LENS_START_TOOL = "lens_start";
export const LENS_START_OFF =
  'T3 refused "lens_start": review rules are off for this thread\'s project (Settings: review rules). Ask the user to turn them on; nothing else changes.';

/** How long one thread's answer is reused: a list request asks once per tool. */
const REUSE_MS = 5_000;
const MAX_THREADS = 1024;
/** Each thread's last answer; `tool_search` reads it (no answer yet: hidden). */
const answers = new Map<string, { readonly on: boolean; readonly until: number }>();

/** `tool_search`'s filter for lens_start: only once a check found the thread's rules on. */
export const lensStartSearchable = (threadId: string): boolean =>
  answers.get(threadId)?.on === true;

/** The check of one thread: its project's effective review rules are on. A thread the store cannot give: off. */
export const makeLensStartOn = Effect.gen(function* () {
  const settingsService = yield* Effect.serviceOption(ServerSettingsService.ServerSettingsService);
  const sql = yield* Effect.serviceOption(SqlClient.SqlClient);
  const store = Option.isSome(sql)
    ? yield* ProjectionStore.ProjectionStoreV2.pipe(
        Effect.provide(ProjectionStore.layer),
        Effect.provideService(SqlClient.SqlClient, sql.value),
      )
    : undefined;
  return Effect.fnUntraced(function* (threadId: ThreadId) {
    const now = yield* Clock.currentTimeMillis;
    const recent = answers.get(threadId);
    if (recent !== undefined && recent.until > now) return recent.on;
    let on = false;
    if (store !== undefined && Option.isSome(settingsService)) {
      const thread = yield* store.findThread(threadId).pipe(Effect.orElseSucceed(() => null));
      const settings = yield* settingsService.value.getSettings.pipe(Effect.option);
      if (thread !== null && Option.isSome(settings)) {
        const value = resolveProjectSettings(settings.value, thread.projectId).settings.reviewRules;
        on = effectiveReviewRules(value ?? undefined).preset === "recommended";
      }
    }
    answers.delete(threadId);
    answers.set(threadId, { on, until: now + REUSE_MS });
    for (const oldest of answers.keys()) {
      if (answers.size <= MAX_THREADS) break;
      answers.delete(oldest);
    }
    return on;
  });
});

export const __testing = { clear: () => answers.clear() };
