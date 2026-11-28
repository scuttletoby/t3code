/**
 * Fork: the "Default: …" end of every Fork settings row's help, read from the code's defaults
 * (`DEFAULT_SERVER_SETTINGS` and the contract constants built the same way), so the help cannot name
 * another default than the one a new machine gets. forkDefaultText.logic.test.ts fails on a row that
 * writes its default by hand.
 */
import {
  DEFAULT_SERVER_SETTINGS,
  DEFAULT_USAGE_PACING_INSTANCE_SETTINGS,
} from "@t3tools/contracts";

import { HANDOFF_MODE_OPTIONS } from "./handoffSettings.logic";
import { MODEL_POLICY_MODE_OPTIONS } from "./modelPolicySettings.logic";
import { formatLowerEffort, USAGE_PACING_GATE_OPTIONS } from "./usagePacingSettings.logic";

const D = DEFAULT_SERVER_SETTINGS;
const onOff = (on: boolean) => (on ? "on" : "off");
const textOrEmpty = (text: string) => (text === "" ? "empty" : text);
const labelOf = (
  options: ReadonlyArray<{ readonly value: string; readonly label: string }>,
  value: string,
) => options.find((option) => option.value === value)?.label ?? value;
const pacingAccount = DEFAULT_USAGE_PACING_INSTANCE_SETTINGS;

export const FORK_DEFAULT_TEXT = {
  automaticUpdates: onOff(D.automaticUpdates),
  automaticUpdatesQuietCommand: textOrEmpty(D.automaticUpdatesQuietCommand),
  usagePacingGate: labelOf(USAGE_PACING_GATE_OPTIONS, D.usagePacingGate),
  usagePacingLowerEffort: textOrEmpty(formatLowerEffort(D.usagePacingLowerEffort)),
  usagePacingAccount: `${pacingAccount.reservePercent}% kept, ${
    pacingAccount.burnFirst ? "use this account first" : "no account first"
  }`,
  accountOrder: textOrEmpty(D.accountOrder.join(", ")),
  waitFor: onOff(D.waitFor.enabled),
  agentSlots: onOff(D.agentSlots.enabled),
  // No account named: every account's limit is 0, no limit.
  agentSlotsPerAccount: Object.keys(D.agentSlots.maxPerAccount).length === 0 ? "0" : "set",
  modelPolicy: labelOf(MODEL_POLICY_MODE_OPTIONS, D.modelPolicy.mode),
  handoff: labelOf(HANDOFF_MODE_OPTIONS, D.handoff.mode),
  handoffInstructions: textOrEmpty(D.handoff.instructions),
  reviewRules: "preset" in D.reviewRules ? String(D.reviewRules.preset) : "off",
  playtest: onOff(D.playtest.enabled),
  assistant: onOff(D.assistant.enabled),
  stopFinishedChildren: onOff(D.stopFinishedChildren.enabled),
  mcpProfile: D.mcpProfile,
  mcpSubagentProfile: D.mcpSubagentProfile,
  mcpThreadProfiles: Object.keys(D.mcpThreadProfiles).length === 0 ? "none" : "set",
} as const;

export type ForkDefaultTextKey = keyof typeof FORK_DEFAULT_TEXT;

/** `Default: off.`, for the setting's row help. */
export const defaultIs = (key: ForkDefaultTextKey) => `Default: ${FORK_DEFAULT_TEXT[key]}.`;
