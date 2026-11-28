import {
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  type ServerProvider,
} from "@t3tools/contracts";
import { DEFAULT_UNIFIED_SETTINGS } from "@t3tools/contracts/settings";
import { createModelCapabilities } from "@t3tools/shared/model";
import { describe, expect, it } from "vite-plus/test";

import { draftThreadModelSelections, noEffortWarning } from "./noEffortWarning.logic";

const CODEX = ProviderInstanceId.make("codex");
const CLAUDE = ProviderInstanceId.make("claudeAgent");
const PROJECT = ProjectId.make("project-no-effort");
const NOW = Date.parse("2026-11-28T12:00:00.000Z");

const effortSelect = (id: string) => ({
  id,
  label: "Reasoning",
  type: "select" as const,
  currentValue: "medium",
  options: [
    { id: "low", label: "Low" },
    { id: "medium", label: "Medium" },
    { id: "high", label: "High" },
  ],
});

function provider(overrides: Partial<ServerProvider>): ServerProvider {
  return {
    instanceId: CODEX,
    driver: ProviderDriverKind.make("codex"),
    enabled: true,
    installed: true,
    version: null,
    status: "ready",
    auth: { status: "authenticated" },
    checkedAt: "2026-11-28T11:00:00.000Z",
    models: [
      {
        slug: "gpt-5.4",
        name: "GPT-5.4",
        isCustom: false,
        capabilities: createModelCapabilities({
          optionDescriptors: [effortSelect("reasoningEffort")],
        }),
      },
      { slug: "gpt-plain", name: "GPT Plain", isCustom: false, capabilities: null },
    ],
    slashCommands: [],
    skills: [],
    ...overrides,
  };
}

const codex = provider({});
const claude = provider({
  instanceId: CLAUDE,
  driver: ProviderDriverKind.make("claudeAgent"),
  models: [
    {
      slug: "claude-opus-5-5",
      name: "Claude Opus 5.5",
      isCustom: false,
      capabilities: createModelCapabilities({ optionDescriptors: [effortSelect("effort")] }),
    },
  ],
});

const bare = { instanceId: CODEX, model: "gpt-5.4" };
const withEffort = { ...bare, options: [{ id: "reasoningEffort", value: "high" }] };

describe("noEffortWarning", () => {
  it("warns while the policy is on and the turn names no effort", () => {
    for (const mode of ["advise", "enforce"] as const) {
      expect(noEffortWarning({ mode, selections: [bare], providers: [codex] })).toEqual({
        modelName: "GPT-5.4",
      });
    }
  });

  it("stays quiet while the policy is off", () => {
    expect(noEffortWarning({ mode: "off", selections: [bare], providers: [codex] })).toBeNull();
  });

  it("stays quiet for a model without an effort control", () => {
    expect(
      noEffortWarning({
        mode: "enforce",
        selections: [{ instanceId: CODEX, model: "gpt-plain" }],
        providers: [codex],
      }),
    ).toBeNull();
    // A model the catalog does not list has no control to pick either.
    expect(
      noEffortWarning({
        mode: "enforce",
        selections: [{ instanceId: CODEX, model: "gpt-unlisted" }],
        providers: [codex],
      }),
    ).toBeNull();
  });

  it("stays quiet once an effort is named, under the driver's own id", () => {
    expect(
      noEffortWarning({ mode: "advise", selections: [withEffort], providers: [codex] }),
    ).toBeNull();
    const opus = { instanceId: CLAUDE, model: "claude-opus-5-5" };
    expect(
      noEffortWarning({
        mode: "advise",
        selections: [{ ...opus, options: [{ id: "effort", value: "high" }] }],
        providers: [claude],
      }),
    ).toBeNull();
    expect(noEffortWarning({ mode: "advise", selections: [opus], providers: [claude] })).toEqual({
      modelName: "Claude Opus 5.5",
    });
  });

  it("reads the selection the turn sends, so a drafted effort wins over the thread's", () => {
    // The hook passes the composer's selection: the draft's options over the thread's stored ones.
    expect(
      noEffortWarning({ mode: "advise", selections: [withEffort], providers: [codex] }),
    ).toBeNull();
    // A fan-out warns for any of its models that names none.
    expect(
      noEffortWarning({
        mode: "advise",
        selections: [withEffort, { instanceId: CLAUDE, model: "claude-opus-5-5" }],
        providers: [codex, claude],
      }),
    ).toEqual({ modelName: "Claude Opus 5.5" });
  });
});

describe("draftThreadModelSelections", () => {
  it("gives a new draft the project's default model, which warns without an effort", () => {
    const selections = draftThreadModelSelections({
      settings: DEFAULT_UNIFIED_SETTINGS,
      project: { id: PROJECT, defaultModelSelection: bare },
      projectId: PROJECT,
      providers: [codex],
      now: NOW,
    });
    expect(selections).toEqual({ thread: bare, project: bare });
    expect(
      noEffortWarning({ mode: "advise", selections: [selections.thread], providers: [codex] }),
    ).toEqual({ modelName: "GPT-5.4" });
    // A project default that names an effort sends it.
    const named = draftThreadModelSelections({
      settings: DEFAULT_UNIFIED_SETTINGS,
      project: { id: PROJECT, defaultModelSelection: withEffort },
      projectId: PROJECT,
      providers: [codex],
      now: NOW,
    });
    expect(
      noEffortWarning({ mode: "advise", selections: [named.thread], providers: [codex] }),
    ).toBeNull();
  });

  it("gives the fallback model while the default's usage limit is reached", () => {
    const opus = { instanceId: CLAUDE, model: "claude-opus-5-5" };
    const limited = provider({
      usageLimits: {
        checkedAt: "2026-11-28T11:00:00.000Z",
        windows: [
          {
            id: "five_hour",
            kind: "session",
            label: "Session",
            usedPercent: 100,
            windowDurationMins: 300,
            resetsAt: "2026-11-28T14:00:00.000Z",
          },
        ],
      },
    });
    const selections = draftThreadModelSelections({
      settings: { ...DEFAULT_UNIFIED_SETTINGS, fallbackModelSelection: opus },
      project: { id: PROJECT, defaultModelSelection: withEffort },
      projectId: PROJECT,
      providers: [limited, claude],
      now: NOW,
    });
    expect(selections).toEqual({ thread: opus, project: opus });
    expect(
      noEffortWarning({
        mode: "advise",
        selections: [selections.thread],
        providers: [limited, claude],
      }),
    ).toEqual({ modelName: "Claude Opus 5.5" });
  });

  it("leaves a project without a default to the composer's own pick", () => {
    expect(
      draftThreadModelSelections({
        settings: DEFAULT_UNIFIED_SETTINGS,
        project: { id: PROJECT },
        projectId: PROJECT,
        providers: [codex],
        now: NOW,
      }),
    ).toEqual({
      thread: { instanceId: ProviderInstanceId.make("t3code_no_provider"), model: "" },
      project: null,
    });
  });
});
