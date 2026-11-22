/// <reference types="vite-plus/client" />

// Fork (calm sidebar): a save's `threadRoles` patch changes the roles field by field, and each
// project's orchestrator entry on its own, so two saves for different projects keep both.
import {
  DEFAULT_SERVER_SETTINGS,
  ServerSettingsPatch,
  type ServerSettings,
} from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { applyServerSettingsPatch } from "./serverSettings.ts";

const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const save = (current: ServerSettings, json: unknown): ServerSettings =>
  applyServerSettingsPatch(current, decodePatch(json));

const withRoles = (threadRoles: unknown): ServerSettings =>
  save(DEFAULT_SERVER_SETTINGS, { threadRoles });

describe("threadRoles patch", () => {
  const start = withRoles({
    assistant: "thread-a",
    orchestrators: { "project-1": "thread-1", "project-2": "thread-2" },
  });

  it("starts from no roles", () => {
    expect(DEFAULT_SERVER_SETTINGS.threadRoles).toEqual({ assistant: null, orchestrators: {} });
    expect(start.threadRoles).toEqual({
      assistant: "thread-a",
      orchestrators: { "project-1": "thread-1", "project-2": "thread-2" },
    });
  });

  it("keeps both of two saves for different projects", () => {
    const first = save(start, { threadRoles: { orchestrators: { "project-3": "thread-3" } } });
    // The second save was made from the same start; it names only its own project.
    const second = save(first, { threadRoles: { orchestrators: { "project-1": "thread-9" } } });
    expect(second.threadRoles).toEqual({
      assistant: "thread-a",
      orchestrators: { "project-1": "thread-9", "project-2": "thread-2", "project-3": "thread-3" },
    });
  });

  it("removes one entry with null and leaves the others", () => {
    const next = save(start, { threadRoles: { orchestrators: { "project-2": null } } });
    expect(next.threadRoles).toEqual({
      assistant: "thread-a",
      orchestrators: { "project-1": "thread-1" },
    });
  });

  it("replaces or removes the assistant without touching the orchestrators", () => {
    expect(save(start, { threadRoles: { assistant: "thread-b" } }).threadRoles).toEqual({
      ...start.threadRoles,
      assistant: "thread-b",
    });
    expect(save(start, { threadRoles: { assistant: null } }).threadRoles).toEqual({
      ...start.threadRoles,
      assistant: null,
    });
  });

  it("changes nothing when the patch leaves it out, or sends it empty", () => {
    expect(save(start, { enableAgentBrowserAccess: false }).threadRoles).toEqual(start.threadRoles);
    expect(save(start, { threadRoles: {} }).threadRoles).toEqual(start.threadRoles);
  });
});
