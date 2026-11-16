// Fork: a managed policy locks the fork's own settings like upstream's. The pc-agents host kit
// (fork-kit/pc-agents) writes a policy that locks the beta switch, automatic updates and the
// child ceiling (agentSlots, one field per dotted path); this pins that each of them is accepted
// and held, and that a partial object is refused (the kit refuses object values for that reason).

import { DEFAULT_SERVER_SETTINGS, type ServerSettings } from "@t3tools/contracts";
import { assert, it } from "@effect/vitest";

import { makeManagedSettings } from "./managedSettings.ts";

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
