// Fork: a managed policy locks the fork's own settings like upstream's. The pc-agents host kit
// (fork-kit/pc-agents) writes a policy that locks the beta switch, automatic updates and the
// child ceiling (agentSlots, one field per dotted path); this pins that each of them is accepted
// and held, and that a partial object is refused (the kit refuses object values for that reason).

import {
  DEFAULT_SERVER_SETTINGS,
  ForkServerSettingsFields,
  ProviderDriverKind,
  ServerSettings,
} from "@t3tools/contracts";
import { assert, it } from "@effect/vitest";
import * as Exit from "effect/Exit";
import * as Schema from "effect/Schema";

import { FORK_WHOLE_MANAGED_SETTINGS } from "./forkManagedSettings.ts";
import { listManageableSettings, makeManagedSettings } from "./managedSettings.ts";

const KIT_POLICY = {
  linkedEnvironmentsEnabled: { value: true, locked: true },
  automaticUpdates: { value: false, locked: true },
  "agentSlots.enabled": { value: true, locked: true },
  "agentSlots.maxPerMachine": { value: 2, locked: true },
};

const managed = () => {
  const result = makeManagedSettings(KIT_POLICY, "full-access");
  if (result._tag !== "Valid") assert.fail(result.reason);
  return result.settings;
};

const userSettings: ServerSettings = {
  ...DEFAULT_SERVER_SETTINGS,
  linkedEnvironmentsEnabled: false,
  automaticUpdates: true,
  agentSlots: { ...DEFAULT_SERVER_SETTINGS.agentSlots, enabled: false, maxPerMachine: 9 },
};

it("accepts the pc-agents kit's locks on fork settings", () => {
  assert.deepEqual([...managed().lockedPaths].toSorted(), [
    "agentSlots.enabled",
    "agentSlots.maxPerMachine",
    "automaticUpdates",
    "linkedEnvironmentsEnabled",
  ]);
});

it("lays the locked fork values over the user's", () => {
  const applied = managed().applyLocks(userSettings);
  assert.strictEqual(applied.linkedEnvironmentsEnabled, true);
  assert.strictEqual(applied.automaticUpdates, false);
  assert.strictEqual(applied.agentSlots.enabled, true);
  assert.strictEqual(applied.agentSlots.maxPerMachine, 2);
});

it("refuses a write that raises the locked child ceiling or turns the beta off", () => {
  const settings = managed();
  const current = settings.applyLocks(userSettings);
  const raise = settings.vetWrite(
    current,
    { ...current, agentSlots: { ...current.agentSlots, maxPerMachine: 5 } },
    { agentSlots: { maxPerMachine: 5 } },
  );
  assert.deepEqual(raise, { _tag: "Locked", path: "agentSlots.maxPerMachine" });
  const off = settings.vetWrite(
    current,
    { ...current, linkedEnvironmentsEnabled: false },
    { linkedEnvironmentsEnabled: false },
  );
  assert.deepEqual(off, { _tag: "Locked", path: "linkedEnvironmentsEnabled" });
});

it("locks one field of a fork setting and leaves the others to the user", () => {
  const result = makeManagedSettings({ "agentSlots.maxPerMachine": { value: 2, locked: true } });
  if (result._tag !== "Valid") assert.fail(result.reason);
  assert.strictEqual(result.settings.applyLocks(userSettings).agentSlots.maxPerMachine, 2);
  assert.strictEqual(result.settings.applyLocks(userSettings).agentSlots.enabled, false);
});

it("takes an object only as a fork setting's complete value", () => {
  const partial = makeManagedSettings({
    agentSlots: { value: { maxPerMachine: 2 }, locked: true },
  });
  assert.strictEqual(partial._tag, "Invalid");
  const whole = makeManagedSettings({
    agentSlots: {
      value: { ...DEFAULT_SERVER_SETTINGS.agentSlots, maxPerMachine: 2 },
      locked: true,
    },
  });
  if (whole._tag !== "Valid") assert.fail(whole.reason);
  assert.strictEqual(whole.settings.applyLocks(userSettings).agentSlots.maxPerMachine, 2);
});

// The fork settings that read any bad value, null included, as off or a default: upstream refuses
// a policy path inside them, so a policy manages each only as a whole.
const encoded = Schema.encodeSync(ServerSettings)(DEFAULT_SERVER_SETTINGS) as Record<
  string,
  unknown
>;
const decodeSettings = Schema.decodeUnknownExit(ServerSettings);

it("names exactly the fork settings that read null as whole-only", () => {
  const readsNull = Object.keys(ForkServerSettingsFields).filter((key) => {
    const withNull: unknown = { ...encoded, [key]: null };
    return Exit.isSuccess(decodeSettings(withNull));
  });
  assert.deepEqual([...FORK_WHOLE_MANAGED_SETTINGS].toSorted(), readsNull.toSorted());
});

it("lists each whole-only fork setting as one path, and a policy can manage it", () => {
  const paths = listManageableSettings().map(([path]) => path);
  for (const key of FORK_WHOLE_MANAGED_SETTINGS) {
    assert.include(paths, key);
    assert.notOk(
      paths.some((path) => path.startsWith(`${key}.`)),
      key,
    );
    const whole = makeManagedSettings({ [key]: { value: encoded[key], locked: true } });
    assert.strictEqual(whole._tag, "Valid", key);
  }
});

it("refuses a policy path inside a whole-only fork setting, never leaving it unlocked", () => {
  for (const path of [
    "handoff.idleTokens",
    "modelPolicy.mode",
    "playtest.enabled",
    "reviewRules.preset",
  ]) {
    const result = makeManagedSettings({ [path]: { value: 0, locked: true } });
    assert.strictEqual(result._tag, "Invalid", path);
    if (result._tag === "Invalid") assert.include(result.reason, "as a whole", path);
  }
});

it("refuses a user's change to any field of a whole-locked handoff or modelPolicy", () => {
  const result = makeManagedSettings({
    handoff: { value: encoded.handoff, locked: true },
    modelPolicy: { value: { mode: "enforce", allow: [] }, locked: true },
  });
  if (result._tag !== "Valid") assert.fail(result.reason);
  const settings = result.settings;
  assert.deepEqual([...settings.lockedPaths].toSorted(), ["handoff", "modelPolicy"]);
  const current = settings.applyLocks({
    ...DEFAULT_SERVER_SETTINGS,
    modelPolicy: { mode: "off", allow: [] },
  });
  assert.deepEqual(current.modelPolicy, { mode: "enforce", allow: [] });
  const handoffChanges: ReadonlyArray<Partial<ServerSettings["handoff"]>> = [
    { mode: "handoff" },
    { source: "command", command: ["handoff-cmd"] },
    { instructions: "write a summary" },
    { sizeTokens: 200_000 },
    { idleTokens: 0 },
    { threads: { "thread-1": "handoff" } },
  ];
  for (const change of handoffChanges) {
    const handoff = { ...current.handoff, ...change };
    const write = settings.vetWrite(current, { ...current, handoff }, { handoff });
    assert.deepEqual(write, { _tag: "Locked", path: "handoff" }, JSON.stringify(change));
  }
  const modelPolicyChanges: ReadonlyArray<ServerSettings["modelPolicy"]> = [
    { mode: "off", allow: [] },
    { mode: "enforce", allow: [{ driver: ProviderDriverKind.make("codex"), model: "gpt-5.4" }] },
  ];
  for (const modelPolicy of modelPolicyChanges) {
    const write = settings.vetWrite(current, { ...current, modelPolicy }, { modelPolicy });
    assert.deepEqual(write, { _tag: "Locked", path: "modelPolicy" }, JSON.stringify(modelPolicy));
  }
});

// Placement (row P2): a policy locks it as a whole value, labels and never groups included, so no
// one but the policy's owner relabels the machine or switches the mode. A bad value (`null` too)
// reads as off, so the policy takes no lock of one field inside it, as for `modelPolicy`.
const placementUser: ServerSettings = {
  ...DEFAULT_SERVER_SETTINGS,
  placement: {
    ...DEFAULT_SERVER_SETTINGS.placement,
    mode: "enforce",
    labels: ["gpu"],
    never: [["ci-runner", "owner-logins"]],
  },
};

it("locks placement whole, so neither the mode nor a label can change", () => {
  const value: ServerSettings["placement"] = {
    mode: "advise",
    labels: ["ci-runner"],
    factsCommand: "",
    never: [["ci-runner", "owner-logins"]],
    weights: {},
  };
  const result = makeManagedSettings({ placement: { value, locked: true } });
  if (result._tag !== "Valid") assert.fail(result.reason);
  const applied = result.settings.applyLocks(placementUser);
  assert.deepEqual(applied.placement, value);
  for (const patch of [{ mode: "enforce" }, { labels: ["gpu"] }, { never: [] }] as const) {
    const write = result.settings.vetWrite(
      applied,
      { ...applied, placement: { ...applied.placement, ...patch } },
      { placement: patch },
    );
    assert.deepEqual(write, { _tag: "Locked", path: "placement" });
  }
});

it("refuses a lock of one placement field, naming the whole setting to lock", () => {
  const result = makeManagedSettings({ "placement.mode": { value: "off", locked: true } });
  assert.strictEqual(result._tag, "Invalid");
  if (result._tag === "Invalid") assert.match(result.reason, /manage "placement" as a whole/);
});
