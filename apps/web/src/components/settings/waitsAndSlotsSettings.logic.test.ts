import { ProviderInstanceId, ServerSettings, ServerSettingsPatch } from "@t3tools/contracts";
import { AgentSlotsSettings, WaitCheck, WaitForSettings } from "@t3tools/contracts/waitFor";
import { applyServerSettingsPatch } from "@t3tools/shared/serverSettings";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  AGENT_SLOTS_NUMBER_FIELDS,
  WAIT_FOR_NUMBER_FIELDS,
  agentSlotAccountRows,
  agentSlotsDescription,
  agentSlotsSummary,
  emptyWaitCheckDraft,
  parseWaitCheckDraft,
  targetsDiffer,
  waitCheckCommandLine,
  waitCheckToDraft,
  waitForDescription,
  waitForSummary,
  withAccountLimit,
  withWaitCheck,
  withoutWaitCheck,
} from "./waitsAndSlotsSettings.logic";

const decodeSettings = Schema.decodeUnknownSync(ServerSettings);
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const decodeWaitCheck = Schema.decodeSync(WaitCheck);
const decodeAgentSlots = Schema.decodeSync(AgentSlotsSettings);
const decodeWaitFor = Schema.decodeSync(WaitForSettings);
const check = (argv: ReadonlyArray<string>, extra: Partial<WaitCheck> = {}): WaitCheck =>
  decodeWaitCheck({ argv: [...argv], ...extra });
const claude = ProviderInstanceId.make("claude");
const codex = ProviderInstanceId.make("codex");

describe("old, newer and empty settings files", () => {
  it("shows both features off for a settings file written before they existed", () => {
    const settings = decodeSettings({});
    expect(waitForSummary(settings.waitFor)).toBe("Off");
    expect(agentSlotsSummary(settings.agentSlots)).toBe("Off");
    expect(settings.waitChecks).toEqual({});
    expect(agentSlotAccountRows([], settings.agentSlots.maxPerAccount)).toEqual([]);
  });

  it("reads a file from a newer build: unknown keys are ignored, bad checks dropped, the rest shown", () => {
    const settings = decodeSettings({
      waitFor: { enabled: true, prSweepSeconds: 60, laterKey: 1 },
      agentSlots: { enabled: true, maxPerMachine: 3, laterKey: "x" },
      waitChecks: {
        "build-done": { argv: ["test", "-f", "/done"], laterField: true },
        "Bad Name": { argv: ["true"] },
        empty: { argv: [] },
      },
    });
    expect(waitForSummary(settings.waitFor)).toBe("On");
    expect(settings.waitFor.prSweepSeconds).toBe(60);
    expect(agentSlotsSummary(settings.agentSlots)).toBe("On: 3 per machine");
    expect(Object.keys(settings.waitChecks)).toEqual(["build-done"]);
    // A save from the panel sends only known fields; the server keeps what it knows.
    expect(waitCheckToDraft("build-done", settings.waitChecks["build-done"]!).command).toBe(
      "test\n-f\n/done",
    );
  });

  it("shows empty limits as no limit", () => {
    const slots = decodeAgentSlots({ enabled: true });
    expect(agentSlotsSummary(slots)).toBe("On, no limit set");
    expect(agentSlotsDescription(slots)).toContain("no limit is set");
    expect(
      agentSlotsSummary({ ...slots, maxPerAccount: { [claude]: 0 } as Record<string, number> }),
    ).toBe("On, no limit set");
  });
});

describe("turning a feature off while it runs", () => {
  const running = applyServerSettingsPatch(decodeSettings({}), {
    waitFor: { enabled: true, maxPerThread: 5 },
    agentSlots: { enabled: true, maxPerMachine: 4, maxPerAccount: { [claude]: 2 } },
  });

  it("shows Off at once and keeps every number for when it is turned on again", () => {
    const off = applyServerSettingsPatch(running, {
      waitFor: { enabled: false },
      agentSlots: { enabled: false },
    });
    expect(waitForSummary(off.waitFor)).toBe("Off");
    expect(waitForDescription(off.waitFor)).toContain("Waits already stored are kept");
    expect(agentSlotsSummary(off.agentSlots)).toBe("Off");
    expect(agentSlotsDescription(off.agentSlots)).toContain("not affected");
    expect(off.waitFor.maxPerThread).toBe(5);
    expect(off.agentSlots.maxPerAccount).toEqual({ [claude]: 2 });

    const on = applyServerSettingsPatch(off, { agentSlots: { enabled: true } });
    expect(agentSlotsSummary(on.agentSlots)).toBe("On: 4 per machine · 1 account limited");
  });
});

describe("number fields", () => {
  const numberKeys = (fields: Record<string, unknown>, value: Record<string, unknown>) =>
    Object.keys(fields)
      .filter((key) => typeof value[key] === "number")
      .toSorted();

  it("lists every number the contract has, so a new one cannot go without a control", () => {
    expect(WAIT_FOR_NUMBER_FIELDS.map((field) => field.key).toSorted()).toEqual(
      numberKeys(WaitForSettings.fields, decodeWaitFor({})),
    );
    expect(AGENT_SLOTS_NUMBER_FIELDS.map((field) => field.key).toSorted()).toEqual(
      numberKeys(AgentSlotsSettings.fields, decodeAgentSlots({})),
    );
  });

  it("never offers a value below what the settings patch accepts", () => {
    for (const field of WAIT_FOR_NUMBER_FIELDS) {
      expect(() => decodePatch({ waitFor: { [field.key]: field.min } })).not.toThrow();
      expect(() => decodePatch({ waitFor: { [field.key]: field.min - 1 } })).toThrow();
    }
    for (const field of AGENT_SLOTS_NUMBER_FIELDS) {
      expect(() => decodePatch({ agentSlots: { [field.key]: field.min } })).not.toThrow();
      expect(() => decodePatch({ agentSlots: { [field.key]: field.min - 1 } })).toThrow();
    }
  });
});

describe("per-account limits", () => {
  it("shows one row per provider instance and keeps limits for instances this server lost", () => {
    const old = ProviderInstanceId.make("old-account");
    expect(
      agentSlotAccountRows(
        [
          { instanceId: claude, displayName: "Claude" },
          { instanceId: codex },
          { instanceId: claude, displayName: "Claude again" },
        ],
        { [claude]: 2, [old]: 1 },
      ),
    ).toEqual([
      { instanceId: claude, label: "Claude", limit: 2, known: true },
      { instanceId: codex, label: "codex", limit: 0, known: true },
      { instanceId: old, label: "old-account", limit: 1, known: false },
    ]);
  });

  it("sends the whole map and removes an entry set to 0", () => {
    const map = withAccountLimit({ [claude]: 2, [codex]: 3 }, claude, 0);
    expect(map).toEqual({ [codex]: 3 });
    const saved = applyServerSettingsPatch(
      applyServerSettingsPatch(decodeSettings({}), {
        agentSlots: { maxPerAccount: { [claude]: 2, [codex]: 3 } },
      }),
      { agentSlots: { maxPerAccount: map } },
    );
    expect(saved.agentSlots.maxPerAccount).toEqual({ [codex]: 3 });
    expect(withAccountLimit({}, codex, 4)).toEqual({ [codex]: 4 });
  });
});

describe("wait check drafts", () => {
  it("reports every empty or wrong field at once and saves nothing", () => {
    const result = parseWaitCheckDraft({
      name: "",
      command: "  \n \n",
      maxArgs: "",
      argPattern: "(",
      intervalSeconds: "4",
      timeoutSeconds: "601",
      description: "",
    });
    expect(result.ok).toBe(false);
    expect(Object.keys(result.ok ? {} : result.errors).toSorted()).toEqual([
      "argPattern",
      "command",
      "intervalSeconds",
      "maxArgs",
      "name",
      "timeoutSeconds",
    ]);
  });

  it("refuses names the server would drop", () => {
    for (const name of ["Build", "-build", "a".repeat(41), "build done", "build_done"]) {
      const result = parseWaitCheckDraft({ ...emptyWaitCheckDraft(), name, command: "true" });
      expect(result.ok, name).toBe(false);
    }
  });

  it("reads one argument per line, drops blank lines and fills the defaults", () => {
    const result = parseWaitCheckDraft({
      ...emptyWaitCheckDraft(),
      name: " deploy-done ",
      command: "systemctl\n\n  is-active  \n--quiet\n",
      maxArgs: "1",
      description: "  A unit is active.  ",
    });
    expect(result).toEqual({
      ok: true,
      name: "deploy-done",
      check: check(["systemctl", "is-active", "--quiet"], {
        maxArgs: 1,
        description: "A unit is active.",
      }),
    });
  });

  it("round-trips a stored check through the form unchanged", () => {
    const stored = check(["gh", "run", "view"], {
      maxArgs: 2,
      argPattern: "^[0-9]+$",
      intervalSeconds: 60,
      timeoutSeconds: 30,
      description: "Run finished",
    });
    expect(parseWaitCheckDraft(waitCheckToDraft("ci-run", stored))).toEqual({
      ok: true,
      name: "ci-run",
      check: stored,
    });
    expect(waitCheckCommandLine(stored)).toBe("gh run view <arg1> <arg2>");
  });
});

describe("saving one check", () => {
  const checks = { a: check(["a"]), b: check(["b"]), c: check(["c"]) };

  it("adds a new check at the end and leaves the others as they were", () => {
    const result = withWaitCheck(checks, null, "d", check(["d"]));
    expect(result.ok && Object.keys(result.checks)).toEqual(["a", "b", "c", "d"]);
    expect(result.ok && result.checks.a).toBe(checks.a);
  });

  it("renames in place and refuses a name another check has", () => {
    const renamed = withWaitCheck(checks, "b", "bee", check(["bee"]));
    expect(renamed.ok && Object.keys(renamed.checks)).toEqual(["a", "bee", "c"]);
    expect(withWaitCheck(checks, "b", "c", check(["c"]))).toEqual({
      ok: false,
      error: 'A check named "c" already exists.',
    });
    expect(withWaitCheck(checks, null, "a", check(["a"])).ok).toBe(false);
  });

  it("removes one check, and removing the last leaves an empty map", () => {
    expect(Object.keys(withoutWaitCheck(checks, "b"))).toEqual(["a", "c"]);
    expect(withoutWaitCheck({ a: check(["a"]) }, "a")).toEqual({});
    expect(withoutWaitCheck(checks, "missing")).toEqual(checks);
  });

  it("is accepted whole by the settings patch, which replaces the stored map", () => {
    const next = withoutWaitCheck(checks, "a");
    const saved = applyServerSettingsPatch(
      applyServerSettingsPatch(decodeSettings({}), { waitChecks: checks }),
      decodePatch({ waitChecks: next }),
    );
    expect(Object.keys(saved.waitChecks)).toEqual(["b", "c"]);
  });
});

describe("several environments selected", () => {
  it("marks only the part that differs as mixed", () => {
    const one = decodeSettings({ agentSlots: { enabled: true, maxPerMachine: 2 } });
    const two = decodeSettings({ agentSlots: { enabled: true, maxPerMachine: 4 } });
    const targets = [{ settings: one }, { settings: two }];
    expect(targetsDiffer(targets, (settings) => settings.agentSlots.enabled)).toBe(false);
    expect(targetsDiffer(targets, (settings) => settings.agentSlots.maxPerMachine)).toBe(true);
    expect(targetsDiffer([], (settings: typeof one) => settings.waitFor.enabled)).toBe(false);
    // Separately decoded but equal maps are the same value.
    expect(
      targetsDiffer(
        [{ settings: decodeSettings({}) }, { settings: decodeSettings({}) }],
        (settings) => settings.waitChecks,
      ),
    ).toBe(false);
  });
});
