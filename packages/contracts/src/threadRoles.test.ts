// Fork (calm sidebar): `threadRoles` and `calmSidebarEnabled` as the settings files hold them. An old
// file reads as no roles and the calm view off; a bad entry drops alone; a save's patch is strict.
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { FORK_AGENT_WRITABLE_KEYS, FORK_PERSON_ONLY_KEYS } from "./forkSettings.ts";
import {
  ClientSettingsPatch,
  ClientSettingsSchema,
  ServerSettings,
  ServerSettingsPatch,
} from "./settings.ts";
import { EMPTY_THREAD_ROLES, ThreadRolesFromSettingsFile } from "./threadRoles.ts";

const decodeServer = Schema.decodeUnknownSync(ServerSettings);
const encodeServer = Schema.encodeSync(ServerSettings);
const decodeClient = Schema.decodeUnknownSync(ClientSettingsSchema);
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const decodeRoles = Schema.decodeUnknownSync(ThreadRolesFromSettingsFile);

describe("threadRoles in the server's settings file", () => {
  it("reads an old file without the key as no roles", () => {
    expect(decodeServer({}).threadRoles).toEqual(EMPTY_THREAD_ROLES);
    expect(decodeServer({ enableAgentBrowserAccess: false }).threadRoles).toEqual({
      assistant: null,
      orchestrators: {},
    });
  });

  it("reads a good value as written, and writes it back the same", () => {
    const roles = { assistant: "thread-a", orchestrators: { "project-1": "thread-o" } };
    const settings = decodeServer({ threadRoles: roles });
    expect(settings.threadRoles).toEqual(roles);
    expect(encodeServer(settings).threadRoles).toEqual(roles);
  });

  it("drops a bad entry alone and keeps the rest of the file", () => {
    const settings = decodeServer({
      enableAgentBrowserAccess: false,
      threadRoles: {
        assistant: "thread-a",
        orchestrators: {
          "project-1": "thread-o",
          "project-2": 42,
          "project-3": "",
          "project-4": " padded ",
          "": "thread-x",
          "project-5": null,
        },
      },
    });
    expect(settings.enableAgentBrowserAccess).toBe(false);
    expect(settings.threadRoles).toEqual({
      assistant: "thread-a",
      orchestrators: { "project-1": "thread-o" },
    });
  });

  it("reads a bad assistant as none and keeps the orchestrators", () => {
    for (const assistant of [7, "", "  ", { id: "thread-a" }, ["thread-a"]]) {
      expect(decodeRoles({ assistant, orchestrators: { "project-1": "thread-o" } })).toEqual({
        assistant: null,
        orchestrators: { "project-1": "thread-o" },
      });
    }
  });

  it("reads a value that is not an object, or a bad orchestrators map, as no roles there", () => {
    for (const raw of [null, "thread-a", 3, [], true]) {
      expect(decodeRoles(raw)).toEqual(EMPTY_THREAD_ROLES);
    }
    expect(decodeRoles({ assistant: "thread-a", orchestrators: ["thread-o"] })).toEqual({
      assistant: "thread-a",
      orchestrators: {},
    });
  });
});

describe("threadRoles in a save's patch", () => {
  it("takes a field at a time, and null removes", () => {
    expect(decodePatch({ threadRoles: { assistant: null } }).threadRoles).toEqual({
      assistant: null,
    });
    expect(
      decodePatch({
        threadRoles: { orchestrators: { "project-1": "thread-o", "project-2": null } },
      }).threadRoles,
    ).toEqual({ orchestrators: { "project-1": "thread-o", "project-2": null } });
  });

  it("refuses a bad id instead of dropping it", () => {
    for (const threadRoles of [
      { assistant: "" },
      { assistant: 7 },
      { orchestrators: { "project-1": "" } },
      { orchestrators: { "project-1": 7 } },
      { orchestrators: { "": "thread-o" } },
      "thread-a",
    ]) {
      expect(() => decodePatch({ threadRoles })).toThrow();
    }
  });

  it("is person-only: agents cannot change it with the settings tool", () => {
    expect(FORK_PERSON_ONLY_KEYS).toContain("threadRoles");
    expect(FORK_AGENT_WRITABLE_KEYS).not.toContain("threadRoles");
  });
});

describe("calmSidebarEnabled in a client's settings", () => {
  it("is off in an old file and reads as written", () => {
    expect(decodeClient({}).calmSidebarEnabled).toBe(false);
    expect(decodeClient({ calmSidebarEnabled: true }).calmSidebarEnabled).toBe(true);
  });

  it("a client save can change it, and a save without it keeps the stored value", () => {
    const decodeClientPatch = Schema.decodeUnknownSync(ClientSettingsPatch);
    expect(decodeClientPatch({ calmSidebarEnabled: true })).toEqual({ calmSidebarEnabled: true });
    expect(decodeClientPatch({})).toEqual({});
    expect(() => decodeClientPatch({ calmSidebarEnabled: "yes" })).toThrow();
  });
});

describe("threadRoles as the settings writer saves it", () => {
  it("writes a value sparse, with a field at its default left out", () => {
    // The writer strips defaults before it encodes; each field alone must still encode and read back.
    const encodeRoles = Schema.encodeUnknownSync(ThreadRolesFromSettingsFile);
    for (const sparse of [
      { orchestrators: { "project-1": "thread-o" } },
      { assistant: "thread-a" },
      {},
    ]) {
      const encoded = encodeRoles(sparse);
      expect(encoded).toEqual(sparse);
      expect(decodeRoles(encoded)).toEqual({ ...EMPTY_THREAD_ROLES, ...sparse });
    }
  });
});
