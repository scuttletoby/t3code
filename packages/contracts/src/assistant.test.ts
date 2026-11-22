// Fork (assistant): the `assistant` settings block. Off in an old or bad file; a save is strict.
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { FORK_PERSON_ONLY_KEYS } from "./forkSettings.ts";
import { ServerSettings, ServerSettingsPatch } from "./settings.ts";

const decodeServer = Schema.decodeUnknownSync(ServerSettings);
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);

describe("assistant settings", () => {
  it("reads an old file, or anything but enabled: true, as off; keeps the other settings", () => {
    for (const assistant of [undefined, null, {}, { enabled: "yes" }, [true], true]) {
      const settings = decodeServer({ assistant, enableAgentBrowserAccess: false });
      expect(settings.assistant).toEqual({ enabled: false });
      expect(settings.enableAgentBrowserAccess).toBe(false);
    }
    expect(decodeServer({ assistant: { enabled: true } }).assistant).toEqual({ enabled: true });
  });

  it("takes a good save, refuses a bad one, and is person-only", () => {
    expect(decodePatch({ assistant: { enabled: true } }).assistant).toEqual({ enabled: true });
    expect(() => decodePatch({ assistant: { enabled: "yes" } })).toThrow();
    expect(FORK_PERSON_ONLY_KEYS).toContain("assistant");
  });
});
