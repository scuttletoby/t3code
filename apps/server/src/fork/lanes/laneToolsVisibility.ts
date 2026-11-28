/**
 * Fork (issue 1156): off means off. The lane tools are listed and callable only while
 * `server.lanes.enabled` is on; the T3 tool rule (fork/mcpT3ToolRule.ts) asks this on every
 * `tools/list` and `tools/call` (a `tool_search` call too, so a search sees the setting as it is
 * then), and `tool_search` keeps them out unless that check found lanes on.
 */
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import * as ServerSettingsService from "../../serverSettings.ts";

export const LANE_TOOLS: ReadonlySet<string> = new Set([
  "lane_start",
  "lane_stop",
  "lane_board",
  "lane_claim",
  "lane_release",
]);

export const LANE_TOOLS_OFF = (tool: string) =>
  `T3 refused "${tool}": lanes are off on this server (Settings: server.lanes.enabled). Ask the user to turn them on; nothing else changes.`;

/** The last check's answer; `tool_search` reads it (no check yet: hidden). */
let lastOn = false;

/** `tool_search`'s filter for the lane tools: only once a check found lanes on. */
export const laneToolsSearchable = (): boolean => lastOn;

/** The check: lanes on in the server settings. Settings that cannot be read: off. */
export const makeLaneToolsOn = Effect.gen(function* () {
  const settingsService = yield* Effect.serviceOption(ServerSettingsService.ServerSettingsService);
  return Effect.gen(function* () {
    if (Option.isNone(settingsService)) return (lastOn = false);
    const settings = yield* settingsService.value.getSettings.pipe(Effect.option);
    lastOn = Option.isSome(settings) && settings.value.lanes.enabled;
    return lastOn;
  });
});

export const __testing = { clear: () => (lastOn = false) };
