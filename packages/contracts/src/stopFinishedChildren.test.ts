// Fork (issue 925): the `stopFinishedChildren` settings block. Off in an old or bad file; a save is strict.
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { FORK_PERSON_ONLY_KEYS } from "./forkSettings.ts";
import { ServerSettings, ServerSettingsPatch } from "./settings.ts";

const decodeServer = Schema.decodeUnknownSync(ServerSettings);
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);

describe("stopFinishedChildren settings", () => {
  it("reads an old file, or anything but enabled: true, as off with the default grace", () => {
    for (const stopFinishedChildren of [undefined, null, {}, { enabled: "yes" }, [true], true]) {
      const settings = decodeServer({ stopFinishedChildren, enableAgentBrowserAccess: false });
      expect(settings.stopFinishedChildren).toEqual({ enabled: false, graceSeconds: 60 });
      expect(settings.enableAgentBrowserAccess).toBe(false);
    }
    expect(
      decodeServer({ stopFinishedChildren: { enabled: true, graceSeconds: 5 } })
        .stopFinishedChildren,
    ).toEqual({ enabled: true, graceSeconds: 5 });
    // A bad grace keeps the switch and reads the default grace.
    expect(
      decodeServer({ stopFinishedChildren: { enabled: true, graceSeconds: -1 } })
        .stopFinishedChildren,
    ).toEqual({ enabled: true, graceSeconds: 60 });
  });

  it("takes a good save, refuses a bad one, and is person-only", () => {
    expect(decodePatch({ stopFinishedChildren: { graceSeconds: 0 } }).stopFinishedChildren).toEqual(
      { graceSeconds: 0 },
    );
    expect(() => decodePatch({ stopFinishedChildren: { enabled: "yes" } })).toThrow();
    expect(() => decodePatch({ stopFinishedChildren: { graceSeconds: 1.5 } })).toThrow();
    expect(FORK_PERSON_ONLY_KEYS).toContain("stopFinishedChildren");
  });
});
