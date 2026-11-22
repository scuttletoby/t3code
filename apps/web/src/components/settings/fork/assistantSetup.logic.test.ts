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
  assistantThreadState,
  makeAssistantPatch,
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

  it("one save names the thread the assistant and gives it the assistant profile", () => {
    const decode = Schema.decodeUnknownSync(ServerSettings);
    const before = decode({ mcpThreadProfiles: { old: "t3-only" } });
    const thread = ThreadId.make("thread-new");
    const after = applyServerSettingsPatch(before, makeAssistantPatch(thread));
    expect(after.threadRoles.assistant).toBe("thread-new");
    expect(after.mcpThreadProfiles).toEqual({ old: "t3-only", "thread-new": "assistant" });
    expect(forkSettingsWriteProblem(before, after)).toBeUndefined();
    const resolved = resolveMcpProfile(after, [{ threadId: thread, delegated: false }]);
    expect(resolved._tag === "Resolved" && resolved.profile.builtinTools).toBe("none");
  });

  it("shows a deleted or archived assistant thread as gone or archived", () => {
    const threads = [{ id: "a", title: "Assistant", archivedAt: null }];
    expect(assistantThreadState(null, threads)).toEqual({ kind: "none" });
    expect(assistantThreadState("a", threads)).toMatchObject({ kind: "ok", title: "Assistant" });
    expect(assistantThreadState("b", threads)).toEqual({ kind: "gone", threadId: "b" });
    expect(
      assistantThreadState("a", [{ ...threads[0]!, archivedAt: "2026-01-01T00:00:00Z" }]).kind,
    ).toBe("archived");
  });
});
