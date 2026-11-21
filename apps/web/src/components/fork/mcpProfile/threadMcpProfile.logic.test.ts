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
