import { ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  type McpProfileShell,
  isMcpProfileMenuAction,
  mcpProfileMenuItems,
  mcpProfileMenuPatch,
  mcpProfileMenuState,
  threadMcpProfile,
  threadMcpProfileText,
  threadPickEffect,
} from "./threadMcpProfile.logic";

const fromFile = Schema.decodeUnknownSync(ServerSettings);

const shell = (
  id: string,
  parent: string | null = null,
  extra: Partial<McpProfileShell> = {},
): McpProfileShell =>
  ({
    id,
    projectId: "project-1",
    lineage: {
      parentThreadId: parent,
      relationshipToParent: parent === null ? null : "subagent",
      rootThreadId: id,
    },
    ...extra,
  }) as McpProfileShell;

const PROFILES = {
  docs: { servers: ["docs", "mail"], t3Tools: "all" },
  narrow: { servers: ["docs"], t3Tools: "all" },
};

describe("a thread's effective MCP profile", () => {
  it("is all from an old settings file, with nothing picked", () => {
    const state = mcpProfileMenuState(fromFile({}), "t1", [shell("t1")], true);
    expect(state.profile).toMatchObject({ _tag: "Resolved", name: "all", source: "environment" });
    expect(state.pick).toBeNull();
    expect(mcpProfileMenuItems(state)[0]!.label).toBe("MCP profile: all");
  });

  it("shows a child's set cut down to its parent's", () => {
    const settings = fromFile({
      mcpProfiles: PROFILES,
      mcpSubagentProfile: "docs",
      mcpThreadProfiles: { parent: "narrow" },
    });
    const shells = [shell("parent"), shell("child", "parent")];
    const profile = threadMcpProfile(settings, "child", shells);
    expect(profile).toMatchObject({
      _tag: "Resolved",
      name: "docs",
      source: "subagent-rule",
      narrowedByParent: true,
      profile: { servers: ["docs"], t3Tools: "all" },
    });
    const [item] = mcpProfileMenuItems(mcpProfileMenuState(settings, "child", shells, true));
    expect(item!.label).toBe("MCP profile: docs, cut down to the parent's");
    expect(threadMcpProfileText(profile)).toBe(
      "docs (the sub-agent rule), cut down to the parent's: Servers: docs · T3 tools: all",
    );
  });

  it("cuts a task another machine delegated down to the profile it carried", () => {
    const settings = fromFile({ mcpProfiles: PROFILES, mcpSubagentProfile: "docs" });
    const carried = { name: "narrow", servers: ["docs"], t3Tools: "all" };
    const task = shell("task", null, {
      delegatedFrom: { environmentId: "env-b", threadId: "far", title: "Far", mcpProfile: carried },
    } as unknown as Partial<McpProfileShell>);
    expect(threadMcpProfile(settings, "task", [task])).toMatchObject({
      name: "docs",
      source: "subagent-rule",
      narrowedByParent: true,
      profile: { servers: ["docs"] },
    });
    // The client's shells hold null for a thread nobody delegated: a top-level thread.
    const local = shell("local", null, { delegatedFrom: null });
    expect(threadMcpProfile(settings, "local", [local])).toMatchObject({
      name: "all",
      source: "environment",
    });
  });

  it("does not guess when a parent is not loaded or a profile is missing", () => {
    const settings = fromFile({ mcpProfiles: PROFILES, mcpProfile: "narrow" });
    expect(threadMcpProfile(settings, "child", [shell("child", "gone")])._tag).toBe("Unknown");
    // The owner's pick stands without the parent, as on the server.
    const picked = fromFile({
      mcpProfiles: PROFILES,
      mcpProfile: "narrow",
      mcpThreadProfiles: { child: "docs" },
    });
    expect(threadMcpProfile(picked, "child", [shell("child", "gone")])).toMatchObject({
      name: "docs",
      source: "thread-pick",
    });
    const missing = fromFile({ mcpThreadProfiles: { t1: "lean" } });
    const profile = threadMcpProfile(missing, "t1", [shell("t1")]);
    expect(profile._tag).toBe("CannotApply");
    expect(
      mcpProfileMenuItems(mcpProfileMenuState(missing, "t1", [shell("t1")], true))[0]!.label,
    ).toBe("MCP profile: cannot apply");
  });
});

describe("the thread menu's MCP profile entry", () => {
  const settings = fromFile({
    mcpProfiles: PROFILES,
    mcpThreadProfiles: { t1: "narrow", other: "docs" },
  });

  it("offers each profile with the pick checked, and clear with the next-message note", () => {
    const [item] = mcpProfileMenuItems(mcpProfileMenuState(settings, "t1", [shell("t1")], true));
    expect(item!.children!.map((child) => [child.id, child.checked ?? false])).toEqual([
      ["mcp-profile:pick:all", false],
      ["mcp-profile:pick:t3-only", false],
      ["mcp-profile:pick:docs", false],
      ["mcp-profile:pick:narrow", true],
      ["mcp-profile:clear", false],
    ]);
    expect(item!.children!.at(-1)!.detail).toBe("A change applies from the next message.");
  });

  it("a pick writes this thread's key alone; clear writes null", () => {
    const picks = settings.mcpThreadProfiles;
    expect(mcpProfileMenuPatch(picks, "t1", "mcp-profile:pick:docs")).toEqual({
      mcpThreadProfiles: { t1: "docs" },
    });
    expect(mcpProfileMenuPatch(picks, "t1", "mcp-profile:clear")).toEqual({
      mcpThreadProfiles: { t1: null },
    });
    // Picking what is already picked, or opening the entry, writes nothing.
    expect(mcpProfileMenuPatch(picks, "t1", "mcp-profile:pick:narrow")).toBeNull();
    expect(mcpProfileMenuPatch(picks, "t1", "mcp-profile")).toBeNull();
    expect(mcpProfileMenuPatch(picks, "new", "mcp-profile:clear")).toBeNull();
    expect(isMcpProfileMenuAction("mcp-profile:pick:docs")).toBe(true);
    expect(isMcpProfileMenuAction("pin")).toBe(false);
  });

  it("only shows the profile to a client that cannot write settings", () => {
    const items = mcpProfileMenuItems(mcpProfileMenuState(settings, "t1", [shell("t1")], false));
    expect(items).toEqual([
      expect.objectContaining({ disabled: true, label: "MCP profile: narrow" }),
    ]);
    expect(items[0]!.children).toBeUndefined();
    expect(mcpProfileMenuItems(undefined)).toEqual([]);
  });
});

describe("what a pick gives a thread", () => {
  // A delegated child that runs cut down to its parent's `narrow` (docs only).
  const settings = fromFile({
    mcpProfiles: PROFILES,
    mcpSubagentProfile: "docs",
    mcpThreadProfiles: { parent: "narrow" },
  });
  const shells = [shell("parent"), shell("child", "parent")];

  it("says when a pick gives a limited child more than its parent has", () => {
    const wide = threadPickEffect(settings, "child", "all", shells);
    expect(wide).toEqual({
      text: "With this pick the thread gets Servers: all · T3 tools: all. That is more than the parent's narrow (Servers: docs · T3 tools: all): a pick is never cut down to the parent's, so this thread gets the wider set.",
      widerThanParent: true,
    });
    // The text matches what the resolver then gives the thread: the pick, not cut down.
    const picked = fromFile({
      mcpProfiles: PROFILES,
      mcpSubagentProfile: "docs",
      mcpThreadProfiles: { parent: "narrow", child: "all" },
    });
    expect(threadMcpProfile(picked, "child", shells)).toMatchObject({
      source: "thread-pick",
      narrowedByParent: false,
      profile: { servers: "all", t3Tools: "all" },
    });
  });

  it("says nothing more for a pick within the parent's set, or for a top thread", () => {
    expect(threadPickEffect(settings, "child", "narrow", shells)).toEqual({
      text: "With this pick the thread gets Servers: docs · T3 tools: all.",
      widerThanParent: false,
    });
    expect(threadPickEffect(settings, "parent", "all", shells).widerThanParent).toBe(false);
  });

  it("cuts a carried profile's task the same way", () => {
    const carried = { name: "narrow", servers: ["docs"], t3Tools: "all" };
    const task = shell("task", null, {
      delegatedFrom: { environmentId: "env-b", threadId: "far", title: "Far", mcpProfile: carried },
    } as unknown as Partial<McpProfileShell>);
    expect(threadPickEffect(settings, "task", "docs", [task])).toMatchObject({
      widerThanParent: true,
      text: expect.stringContaining(
        "more than narrow (sent by the machine that delegated it) (Servers: docs · T3 tools: all)",
      ),
    });
  });

  it("does not guess for a thread or parent not loaded here, or a pick that cannot apply", () => {
    expect(threadPickEffect(settings, "elsewhere", "all", shells)).toEqual({
      text: "With this pick the thread gets Servers: all · T3 tools: all. This thread is not loaded here, so whether that is more than its parent has cannot be told; a pick is never cut down to the parent's.",
      widerThanParent: false,
    });
    expect(threadPickEffect(settings, "child", "all", [shell("child", "gone")]).text).toMatch(
      /cannot be told \(its parent's profile cannot be worked out here\)/,
    );
    const missing = threadPickEffect(settings, "child", "lean", shells);
    expect(missing.text).toMatch(
      /^With this pick the thread cannot start: MCP profile "lean" is not set up/,
    );
  });
});

describe("a blank pick from a hand edit", () => {
  it("shows as no pick, as the resolver ignores it, and can be cleared", () => {
    const settings = fromFile({ mcpProfiles: PROFILES, mcpThreadProfiles: { t1: "  " } });
    const state = mcpProfileMenuState(settings, "t1", [shell("t1")], true);
    expect(state).toMatchObject({ pick: null, blankPick: true });
    expect(state.profile).toMatchObject({ name: "all", source: "environment" });
    const [item] = mcpProfileMenuItems(state);
    expect(item!.children!.filter((child) => child.checked)).toEqual([]);
    const clear = item!.children!.at(-1)!;
    expect(clear.disabled).toBe(false);
    expect(clear.detail).toMatch(/^Its pick in settings.json is blank, so it is ignored\./);
    expect(mcpProfileMenuPatch(settings.mcpThreadProfiles, "t1", "mcp-profile:clear")).toEqual({
      mcpThreadProfiles: { t1: null },
    });
    // A thread with no entry at all: nothing to clear.
    const none = mcpProfileMenuState(settings, "t2", [shell("t2")], true);
    expect(none.blankPick).toBe(false);
    expect(mcpProfileMenuItems(none)[0]!.children!.at(-1)!.disabled).toBe(true);
  });
});
