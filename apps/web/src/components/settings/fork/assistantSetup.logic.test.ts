import {
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  ServerSettings,
  ThreadId,
  resolveMcpProfile,
  type ServerProvider,
} from "@t3tools/contracts";
import { forkSettingsWriteProblem } from "@t3tools/shared/forkSettingsPatch";
import { applyServerSettingsPatch } from "@t3tools/shared/serverSettings";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  type MakeAssistantSteps,
  assistantModelSelection,
  assistantPickRemovalPatch,
  assistantProfilePatch,
  assistantRolePatch,
  assistantThreadState,
  makeAssistantThread,
} from "./assistantSetup.logic";

const provider = (
  instanceId: string,
  driver: string,
  models: ReadonlyArray<{ slug: string; isDefault?: boolean }>,
  extra: Partial<ServerProvider> = {},
): ServerProvider =>
  ({
    instanceId: ProviderInstanceId.make(instanceId),
    driver: ProviderDriverKind.make(driver),
    enabled: true,
    availability: "available",
    models: models.map((model) => ({ ...model, name: model.slug, isCustom: false })),
    ...extra,
  }) as unknown as ServerProvider;

describe("Make assistant", () => {
  it("picks an on, available Claude instance (the default first) and Claude's default model", () => {
    const providers = [
      provider("codex", "codex", [{ slug: "gpt" }]),
      provider("claudeWork", "claudeAgent", [{ slug: "claude-x", isDefault: true }]),
      provider("claudeAgent", "claudeAgent", [{ slug: "claude-x" }, { slug: "claude-opus-5-5" }]),
    ];
    expect(assistantModelSelection(providers)).toEqual({
      instanceId: "claudeAgent",
      model: "claude-opus-5-5",
    });
    expect(assistantModelSelection(providers.slice(0, 2))).toEqual({
      instanceId: "claudeWork",
      model: "claude-x",
    });
  });

  it("offers nothing without a usable Claude: no other provider can drop built-in tools", () => {
    expect(assistantModelSelection([provider("codex", "codex", [{ slug: "gpt" }])])).toBeNull();
    const off = provider("claudeAgent", "claudeAgent", [{ slug: "c" }], { enabled: false });
    const gone = provider("claudeAgent", "claudeAgent", [{ slug: "c" }], {
      availability: "unavailable",
    } as Partial<ServerProvider>);
    expect(assistantModelSelection([off, gone])).toBeNull();
  });

  it("picks the profile first, before the thread exists, then names the assistant", () => {
    const decode = Schema.decodeUnknownSync(ServerSettings);
    const before = decode({ mcpThreadProfiles: { old: "t3-only" } });
    const thread = ThreadId.make("thread-new");
    // Saved before the thread is made: if setup stops here, no wide "Assistant" thread exists.
    const picked = applyServerSettingsPatch(before, assistantProfilePatch(thread));
    expect(forkSettingsWriteProblem(before, picked)).toBeUndefined();
    expect(picked.threadRoles.assistant).toBeNull();
    const resolved = resolveMcpProfile(picked, [{ threadId: thread, delegated: false }]);
    expect(resolved._tag === "Resolved" && resolved.profile.builtinTools).toBe("none");
    const named = applyServerSettingsPatch(picked, assistantRolePatch(thread));
    expect(forkSettingsWriteProblem(picked, named)).toBeUndefined();
    expect(named.threadRoles.assistant).toBe("thread-new");
    expect(named.mcpThreadProfiles).toEqual({ old: "t3-only", "thread-new": "assistant" });
  });

  it("shows an assistant thread that is not active (archived or deleted) as none", () => {
    const active = [{ id: "a", title: "Assistant" }];
    expect(assistantThreadState(null, active)).toEqual({ kind: "none" });
    expect(assistantThreadState("a", active)).toMatchObject({ kind: "ok", title: "Assistant" });
    expect(assistantThreadState("b", active)).toEqual({ kind: "inactive", threadId: "b" });
  });
});

const decodeServerSettings = Schema.decodeSync(ServerSettings);

describe("Make assistant's run (#953)", () => {
  const NEW = ThreadId.make("thread-new");

  /** Steps that log what they did; `overrides` make one fail, refuse or throw. */
  const stepsWith = (overrides: Partial<MakeAssistantSteps> = {}) => {
    const log: Array<string> = [];
    const steps: MakeAssistantSteps = {
      openScratchProject: async () => ProjectId.make("scratch"),
      newThreadId: () => NEW,
      save: async (patch) => {
        log.push(`save ${JSON.stringify(patch)}`);
        return true;
      },
      createThread: async () => {
        log.push("create");
        return true;
      },
      waitForThread: async () => true,
      discardThread: async () => {
        log.push("discard");
        return true;
      },
      ...overrides,
    };
    return { steps, log };
  };
  const pick = `save ${JSON.stringify(assistantProfilePatch(NEW))}`;
  const role = `save ${JSON.stringify(assistantRolePatch(NEW))}`;
  const removal = `save ${JSON.stringify(assistantPickRemovalPatch(NEW))}`;

  it("saves the pick, makes the thread, then the role, and cleans up nothing", async () => {
    const { steps, log } = stepsWith();
    expect(await makeAssistantThread(steps)).toEqual({ _tag: "Made", threadId: NEW });
    expect(log).toEqual([pick, "create", role]);
  });

  it("always ends with an outcome when a step throws or rejects, so the button leaves busy", async () => {
    const throwing: ReadonlyArray<Partial<MakeAssistantSteps>> = [
      { openScratchProject: () => Promise.reject(new Error("scratch")) },
      {
        save: () => {
          throw new Error("save threw");
        },
      },
      { createThread: () => Promise.reject(new Error("rpc")) },
      { waitForThread: () => Promise.reject(new Error("wait")) },
    ];
    for (const overrides of throwing) {
      const outcome = await makeAssistantThread(stepsWith(overrides).steps);
      expect(outcome._tag).toBe("Stopped");
      expect(outcome._tag === "Stopped" ? outcome.error : null).toContain("Try again");
    }
  });

  it("a refused role removes the empty thread, then its pick, so retries leave no picks", async () => {
    let first = true;
    const { steps, log } = stepsWith({
      save: async (patch) => {
        log.push(`save ${JSON.stringify(patch)}`);
        if (patch.threadRoles === undefined) return true;
        return (first = false);
      },
    });
    const outcome = await makeAssistantThread(steps);
    expect(outcome._tag).toBe("Stopped");
    expect(first).toBe(false);
    expect(log).toEqual([pick, "create", role, "discard", removal]);
  });

  it("each stop after the pick cleans up; a refused pick has nothing to clean up", async () => {
    const notMade = stepsWith({ createThread: async () => false });
    await makeAssistantThread(notMade.steps);
    expect(notMade.log).toEqual([pick, "discard", removal]);
    const notSeen = stepsWith({ waitForThread: async () => false });
    await makeAssistantThread(notSeen.steps);
    expect(notSeen.log).toEqual([pick, "create", "discard", removal]);
    const threw = stepsWith({ waitForThread: () => Promise.reject(new Error("wait")) });
    await makeAssistantThread(threw.steps);
    expect(threw.log).toEqual([pick, "create", "discard", removal]);
    const refused = stepsWith();
    refused.steps = {
      ...refused.steps,
      save: async (patch) => {
        refused.log.push(`save ${JSON.stringify(patch)}`);
        return false;
      },
    };
    await makeAssistantThread(refused.steps);
    expect(refused.log).toEqual([pick]);
  });

  it("a thread that stays (someone wrote in it, or the delete failed) keeps its narrow pick", async () => {
    const kept = stepsWith({
      waitForThread: async () => false,
      discardThread: async () => false,
    });
    await makeAssistantThread(kept.steps);
    expect(kept.log).toEqual([pick, "create"]);
    // A clean-up that throws leaves the pick and still ends with the outcome.
    const broken = stepsWith({
      waitForThread: async () => false,
      discardThread: () => Promise.reject(new Error("delete")),
    });
    expect((await makeAssistantThread(broken.steps))._tag).toBe("Stopped");
    expect(broken.log).toEqual([pick, "create"]);
  });

  it("the removal clears only this thread's pick", () => {
    const settings = decodeServerSettings({
      mcpThreadProfiles: { [NEW]: "assistant", other: "assistant" },
    });
    expect(
      applyServerSettingsPatch(settings, assistantPickRemovalPatch(NEW)).mcpThreadProfiles,
    ).toEqual({ other: "assistant" });
  });
});
