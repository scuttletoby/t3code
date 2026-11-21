import { ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  buildThreadActionMenuItems,
  type ThreadActionMenuState,
} from "../../threadActionMenu.logic";
import { type McpProfileShell, mcpProfileMenuState } from "./threadMcpProfile.logic";
import { runIfMcpProfileMenuAction } from "./threadMcpProfileMenu";

const fromFile = Schema.decodeUnknownSync(ServerSettings);

const base: ThreadActionMenuState = {
  canOperate: true,
  branch: null,
  projectFilter: null,
  isPinned: false,
  isSettled: false,
  autoSettleEnabled: true,
  isSnoozed: false,
  canSnoozeNow: true,
  isRegeneratingTitle: false,
  isRunning: false,
  supports: {
    settlement: true,
    autoSettleOptOut: true,
    snooze: true,
    pinning: true,
    titleRegeneration: true,
  },
  snoozePresets: [],
};

const thread = {
  id: "t1",
  projectId: "project-1",
  lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: "t1" },
  delegatedFrom: null,
} as unknown as McpProfileShell;

describe("the thread menu's MCP profile entry", () => {
  it("is absent for a caller that reads no MCP profile state", () => {
    const ids = buildThreadActionMenuItems(base).map((item) => item.id);
    expect(ids).not.toContain("mcp-profile");
  });

  it("shows the effective profile before Copy, with the picks for an owner", () => {
    const settings = fromFile({ mcpThreadProfiles: { t1: "t3-only" } });
    const items = buildThreadActionMenuItems({
      ...base,
      forkMcpProfile: mcpProfileMenuState(settings, "t1", [thread], true),
    });
    const ids = items.map((item) => item.id);
    expect(ids.indexOf("mcp-profile")).toBe(ids.indexOf("copy") - 1);
    const entry = items.find((item) => item.id === "mcp-profile")!;
    expect(entry.label).toBe("MCP profile: t3-only");
    expect(entry.disabled).toBeFalsy();
    expect(entry.children!.find((child) => child.checked)!.id).toBe("mcp-profile:pick:t3-only");
  });

  it("is disabled, with its picks, for a client that may not change threads", () => {
    const settings = fromFile({});
    const items = buildThreadActionMenuItems({
      ...base,
      canOperate: false,
      forkMcpProfile: mcpProfileMenuState(settings, "t1", [thread], true),
    });
    const entry = items.find((item) => item.id === "mcp-profile")!;
    expect(entry.disabled).toBe(true);
    expect(entry.children!.every((child) => child.disabled === true)).toBe(true);
  });

  it("takes only its own clicks, so the menu's other actions run as before", () => {
    const ref = { environmentId: "env-a", threadId: "t1" } as never;
    expect(runIfMcpProfileMenuAction(ref, "pin")).toBe(false);
    expect(runIfMcpProfileMenuAction(ref, "snooze:hour")).toBe(false);
    expect(runIfMcpProfileMenuAction(ref, "mcp-profile")).toBe(true);
  });
});
