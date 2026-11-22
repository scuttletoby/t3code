import {
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
  assistantModelSelection,
  assistantProfilePatch,
  assistantRolePatch,
  assistantThreadState,
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
