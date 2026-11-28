// Fork (calm sidebar): the thread menu hook's entry module. It loads without the app graph (so
// upstream's hook test, which mocks react bare, still loads), and delegates to the runtime once
// the app installs it. Each test names the question it answers.
import { EnvironmentId, ThreadId } from "@t3tools/contracts";
import { describe, expect, it, vi } from "vite-plus/test";

import {
  installThreadRoleMenuRuntime,
  readThreadRoleMenuState,
  runIfThreadRoleMenuAction,
} from "./threadRoleMenu";
import { MAKE_ASSISTANT, REMOVE_ROLE } from "./threadRoleMenu.logic";

const ref = { environmentId: EnvironmentId.make("local"), threadId: ThreadId.make("a") };

describe("thread menu Role entry module", () => {
  it("before the app installs its runtime: no entry, and a Role pick is still claimed, not run elsewhere", () => {
    expect(readThreadRoleMenuState(ref)).toBeUndefined();
    expect(runIfThreadRoleMenuAction(ref, MAKE_ASSISTANT)).toBe(true);
    expect(runIfThreadRoleMenuAction(ref, "copy")).toBe(false);
  });

  it("once installed: reads and runs go to the runtime; other menu ids stay the caller's", () => {
    const state = {
      isAssistant: false,
      isOrchestrator: false,
      holdsOtherProjectRole: false,
      orchestratorHolder: null,
      assistantHolder: null,
    };
    const run = vi.fn();
    installThreadRoleMenuRuntime({ read: () => state, run });
    expect(readThreadRoleMenuState(ref)).toBe(state);
    expect(runIfThreadRoleMenuAction(ref, REMOVE_ROLE)).toBe(true);
    expect(run).toHaveBeenCalledWith(ref, REMOVE_ROLE);
    expect(runIfThreadRoleMenuAction(ref, "archive")).toBe(false);
    expect(run).toHaveBeenCalledTimes(1);
  });
});
