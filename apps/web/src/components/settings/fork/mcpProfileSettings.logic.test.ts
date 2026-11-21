import { ServerSettings, ServerSettingsPatch } from "@t3tools/contracts";
import { forkSettingsWriteProblem } from "@t3tools/shared/forkSettingsPatch";
import { applyServerSettingsPatch } from "@t3tools/shared/serverSettings";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  clearThreadPicksPatch,
  deleteMcpProfilePatch,
  mcpProfileChoices,
  mcpProfileRows,
  mcpProfileUsers,
  mcpThreadPickRows,
  notSetUpNotes,
  saveMcpProfilePatch,
  shownProfileRef,
  threadPickPatch,
  threadPickProblem,
} from "./mcpProfileSettings.logic";

const fromFile = Schema.decodeUnknownSync(ServerSettings);
const isPatch = Schema.is(ServerSettingsPatch);
const NO_LABELS = { projects: new Map<string, string>(), threads: new Map<string, string>() };

/** The server's save: the patch must decode, apply, and pass the fork save check. */
function serverSave(settings: ServerSettings, patch: unknown): ServerSettings {
  expect(isPatch(patch), JSON.stringify(patch)).toBe(true);
  const next = applyServerSettingsPatch(settings, patch as ServerSettingsPatch);
  expect(forkSettingsWriteProblem(settings, next)).toBeUndefined();
  return next;
}

const LEAN = { servers: ["docs"], t3Tools: ["threads", "orchestration"] };

describe("MCP profile list", () => {
  it("shows all and t3-only read-only from an old settings file, and nothing else", () => {
    const old = fromFile({});
    const rows = mcpProfileRows(old, null);
    expect(rows.map((row) => [row.name, row.kind, row.state])).toEqual([
      ["all", "built-in", "ok"],
      ["t3-only", "built-in", "ok"],
    ]);
    expect(mcpProfileChoices(old)).toEqual(["all", "t3-only"]);
    // The default and the rule show as their defaults; nothing to write.
    expect(shownProfileRef(old.mcpProfile, "all")).toBe("all");
    expect(shownProfileRef(old.mcpSubagentProfile, "inherit")).toBe("inherit");
    // A project's blank value (not set) shows the server's, never a wider fallback.
    expect(shownProfileRef("", "t3-only")).toBe("t3-only");
  });

  it("shows an entry with fields from a newer build as broken, not as a profile", () => {
    const settings = fromFile({
      mcpProfiles: {
        lean: LEAN,
        newer: { servers: "all", t3Tools: "all", hosts: ["a"] },
        groups: { servers: [], t3Tools: ["threads", "future-group"] },
      },
    });
    const rows = mcpProfileRows(settings, null);
    const newer = rows.find((row) => row.name === "newer")!;
    expect(newer.state).toBe("broken");
    expect(newer.state !== "ok" && newer.issue).toMatch(
      /fields this version does not know \(hosts\)/,
    );
    // Not offered as a pick, and never shown as `all` (its servers and t3Tools say all).
    expect(mcpProfileChoices(settings)).not.toContain("newer");
    expect("profile" in newer).toBe(false);
    // Unknown T3 tool groups keep nothing: not editable here, but a thread may still use it.
    const groups = rows.find((row) => row.name === "groups")!;
    expect(groups.state).toBe("foreign");
    expect(mcpProfileChoices(settings)).toContain("groups");
  });

  it("shows a hand entry under a built-in name as breaking that built-in, removable", () => {
    const settings = fromFile({ mcpProfiles: { all: { servers: [], t3Tools: "all" } } });
    const all = mcpProfileRows(settings, null).find((row) => row.name === "all")!;
    expect(all).toMatchObject({ kind: "built-in", state: "broken", removable: true });
    const result = deleteMcpProfilePatch({ ...settings }, "all", NO_LABELS);
    expect(result).toEqual({ patch: { mcpProfiles: { all: null } } });
    expect(serverSave(settings, "patch" in result ? result.patch : null).mcpProfiles).toEqual({});
  });

  it("notes a server that is not set up", () => {
    expect(notSetUpNotes("lean", ["docs", "mail"], ["docs"])).toEqual([
      "`lean` names `mail`, which is not set up.",
    ]);
    expect(notSetUpNotes("lean", "all", [])).toEqual([]);
    // A client that cannot list the servers claims nothing.
    expect(notSetUpNotes("lean", ["mail"], null)).toEqual([]);
  });
});

describe("saving and deleting a profile", () => {
  it("adds, edits and deletes one entry, keeping the others", () => {
    let settings = fromFile({ mcpProfiles: { other: { servers: "all", t3Tools: "all" } } });
    const add = saveMcpProfilePatch(settings, null, {
      name: " lean ",
      servers: ["docs", " docs", ""],
      t3Tools: ["threads", "orchestration"],
    });
    // Only the new entry is sent; the groups go in the shared list's order.
    expect(add).toEqual({
      patch: {
        mcpProfiles: { lean: { servers: ["docs"], t3Tools: ["orchestration", "threads"] } },
      },
    });
    settings = serverSave(settings, "patch" in add ? add.patch : null);
    expect(Object.keys(settings.mcpProfiles).toSorted()).toEqual(["lean", "other"]);

    const edit = saveMcpProfilePatch(settings, "lean", {
      name: "",
      servers: "all",
      t3Tools: "all",
    });
    expect(edit).toEqual({ patch: { mcpProfiles: { lean: { servers: "all", t3Tools: "all" } } } });
    settings = serverSave(settings, "patch" in edit ? edit.patch : null);
    expect(settings.mcpProfiles.other).toEqual({ servers: "all", t3Tools: "all" });

    const remove = deleteMcpProfilePatch({ ...settings }, "lean", NO_LABELS);
    expect(remove).toEqual({ patch: { mcpProfiles: { lean: null } } });
    settings = serverSave(settings, "patch" in remove ? remove.patch : null);
    expect(Object.keys(settings.mcpProfiles)).toEqual(["other"]);
  });

  it("refuses built-in names, inherit, bad names and a name taken", () => {
    const settings = fromFile({ mcpProfiles: { lean: LEAN } });
    const draft = { servers: [] as string[], t3Tools: "all" as const };
    for (const name of ["all", "t3-only"]) {
      expect(saveMcpProfilePatch(settings, null, { ...draft, name })).toEqual({
        problem: `"${name}" is a built-in profile; pick another name.`,
      });
    }
    expect(saveMcpProfilePatch(settings, null, { ...draft, name: "inherit" })).toHaveProperty(
      "problem",
    );
    expect(saveMcpProfilePatch(settings, null, { ...draft, name: "Lean Mode" })).toHaveProperty(
      "problem",
    );
    expect(saveMcpProfilePatch(settings, null, { ...draft, name: "" })).toEqual({
      problem: "Enter a name.",
    });
    expect(saveMcpProfilePatch(settings, null, { ...draft, name: "lean" })).toEqual({
      problem: 'A profile named "lean" already exists.',
    });
  });

  it("refuses to delete a profile in use, naming each user, and offers to clear the thread picks", () => {
    const projectId = "project-1";
    const settings = fromFile({
      mcpProfiles: { lean: LEAN },
      mcpProfile: "lean",
      mcpSubagentProfile: "lean",
      mcpThreadProfiles: { "thread-2": "lean", "thread-1": "lean", "thread-3": "all" },
      projectSettingsOverrides: { [projectId]: { mcpProfile: "lean" } },
    });
    const labels = {
      projects: new Map([[projectId, "Website"]]),
      threads: new Map([["thread-1", "Fix login"]]),
    };
    const result = deleteMcpProfilePatch(settings, "lean", labels);
    expect(result).toEqual({
      problem:
        '"lean" is in use by the default profile; the sub-agent rule; project Website\'s default profile; the picks of 2 threads: Fix login, thread-2. Change those first.',
      threadIds: ["thread-1", "thread-2"],
    });
    // Clearing those picks sends only theirs; the other thread's pick stays.
    const cleared = serverSave(settings, clearThreadPicksPatch(["thread-1", "thread-2"]));
    expect(cleared.mcpThreadProfiles).toEqual({ "thread-3": "all" });
    expect(mcpProfileUsers(cleared, "lean", labels).threadIds).toEqual([]);
  });
});

describe("thread picks", () => {
  it("a pick writes one key and keeps the other threads' picks; clear writes null", () => {
    const settings = fromFile({
      mcpProfiles: { lean: LEAN },
      mcpThreadProfiles: { "thread-a": "t3-only", "thread-b": "lean" },
    });
    const pick = threadPickPatch(settings.mcpThreadProfiles, "thread-c", "lean");
    expect(pick).toEqual({ mcpThreadProfiles: { "thread-c": "lean" } });
    const picked = serverSave(settings, pick);
    expect(picked.mcpThreadProfiles).toEqual({
      "thread-a": "t3-only",
      "thread-b": "lean",
      "thread-c": "lean",
    });
    // Another client cleared thread-a's pick since this one read the picks: a pick for thread-b
    // made from the old read must not bring thread-a's back.
    const elsewhere = serverSave(picked, { mcpThreadProfiles: { "thread-a": null } });
    const change = threadPickPatch(settings.mcpThreadProfiles, "thread-b", "all");
    expect(change).toEqual({ mcpThreadProfiles: { "thread-b": "all" } });
    expect(serverSave(elsewhere, change).mcpThreadProfiles).toEqual({
      "thread-b": "all",
      "thread-c": "lean",
    });

    const clear = threadPickPatch(picked.mcpThreadProfiles, "thread-a", null);
    expect(clear).toEqual({ mcpThreadProfiles: { "thread-a": null } });
    const cleared = serverSave(picked, clear);
    expect(cleared.mcpThreadProfiles).toEqual({ "thread-b": "lean", "thread-c": "lean" });
    // Runs again: the pick is gone, so a second clear (or the same pick again) writes nothing.
    expect(threadPickPatch(cleared.mcpThreadProfiles, "thread-a", null)).toBeNull();
    expect(threadPickPatch(cleared.mcpThreadProfiles, "thread-b", "lean")).toBeNull();
  });
});

describe("adding a thread pick", () => {
  const settings = fromFile({ mcpProfiles: { lean: LEAN } });

  it("saves nothing until a profile is chosen: a typed id alone never falls back to all", () => {
    expect(threadPickProblem(settings, "thread-1", null)).toBe("Choose a profile for this thread.");
    expect(threadPickProblem(settings, "  ", "lean")).toBe("Enter a thread id.");
    expect(threadPickProblem(settings, " thread-1 ", "lean")).toBeNull();
  });

  it("shows a blank pick from a hand edit as broken, not as a profile", () => {
    const blank = fromFile({ mcpThreadProfiles: { "thread-1": "  ", "thread-2": "lean" } });
    expect(mcpThreadPickRows(blank.mcpThreadProfiles, new Map())).toEqual([
      { threadId: "thread-1", title: null, pick: null },
      { threadId: "thread-2", title: null, pick: "lean" },
    ]);
  });
});
