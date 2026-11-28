// Fork (issue 1156, phase 1): the `lanes` settings block and the lane contracts. An old or bad file
// reads as off; a bad lane drops alone; a save is strict; old records decode.
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  DEFAULT_LANE_DEFINITIONS,
  findLaneDefinition,
  formatLaneItemRef,
  LaneClaim,
  LaneEscalation,
  LaneNote,
  parseLaneItemRef,
} from "./forkLanes.ts";
import { FORK_PERSON_ONLY_KEYS } from "./forkSettings.ts";
import { ServerSettings, ServerSettingsPatch } from "./settings.ts";

const decodeServer = Schema.decodeUnknownSync(ServerSettings);
const encodeServer = Schema.encodeSync(ServerSettings);
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);

const PR_QUEUE = DEFAULT_LANE_DEFINITIONS["pr-queue"]!;

describe("lanes settings", () => {
  it("reads an old file, or anything but an object, as off with the built-in lanes", () => {
    for (const lanes of [undefined, null, true, "on", [], [{ enabled: true }]]) {
      const settings = decodeServer({ lanes, enableAgentBrowserAccess: false });
      expect(settings.lanes).toEqual({ enabled: false, definitions: DEFAULT_LANE_DEFINITIONS });
      expect(settings.enableAgentBrowserAccess).toBe(false);
    }
    expect(Object.keys(DEFAULT_LANE_DEFINITIONS)).toEqual(["pr-queue", "issues"]);
  });

  it("is on only for enabled: true, and keeps the built-in lanes when definitions is left out", () => {
    expect(decodeServer({ lanes: { enabled: true } }).lanes).toEqual({
      enabled: true,
      definitions: DEFAULT_LANE_DEFINITIONS,
    });
    for (const enabled of ["yes", 1, null]) {
      expect(decodeServer({ lanes: { enabled } }).lanes.enabled).toBe(false);
    }
  });

  it("defines no lane for definitions that is not an object, and an empty one as none", () => {
    for (const definitions of [{}, null, [], "pr-queue"]) {
      expect(decodeServer({ lanes: { enabled: true, definitions } }).lanes).toEqual({
        enabled: true,
        definitions: {},
      });
    }
  });

  it("drops a bad lane name or definition alone and keeps the others", () => {
    const settings = decodeServer({
      lanes: {
        enabled: true,
        definitions: {
          "pr-queue": { description: "Pull requests.", owns: ["pr"] },
          ops: { description: "Machines and CI.", owns: [], driver: "agent", later: 1 },
          "PR Queue": { description: "Not a lane name.", owns: ["pr"] },
          "-lead": { description: "Not a lane name.", owns: ["pr"] },
          ["x".repeat(41)]: { description: "Too long a name.", owns: ["pr"] },
          program: { description: "A driver not built yet.", owns: ["pr"], driver: "program" },
          kinds: { description: "An unknown item kind.", owns: ["commit"] },
          blank: { description: "  ", owns: ["issue"] },
          bare: "a lane",
        },
      },
    });
    expect(settings.lanes.definitions).toEqual({
      "pr-queue": { description: "Pull requests.", owns: ["pr"], driver: "agent" },
      ops: { description: "Machines and CI.", owns: [], driver: "agent" },
    });
  });

  it("writes the value it read back to the file", () => {
    const settings = decodeServer({
      lanes: { enabled: true, definitions: { "pr-queue": PR_QUEUE } },
    });
    expect(decodeServer(encodeServer(settings)).lanes).toEqual(settings.lanes);
  });

  it("finds a defined lane and refuses a name the settings do not define", () => {
    const { lanes } = decodeServer({});
    expect(findLaneDefinition(lanes, "pr-queue")).toEqual(PR_QUEUE);
    for (const name of ["ops", "toString", "constructor", "PR-QUEUE", ""]) {
      expect(findLaneDefinition(lanes, name)).toBeUndefined();
    }
  });

  it("takes a good save, refuses a bad one, and is person-only", () => {
    expect(decodePatch({ lanes: { enabled: true } }).lanes).toEqual({ enabled: true });
    expect(
      decodePatch({
        lanes: { definitions: { ops: { description: "Machines.", owns: [] }, issues: null } },
      }).lanes,
    ).toEqual({
      definitions: { ops: { description: "Machines.", owns: [], driver: "agent" }, issues: null },
    });
    for (const lanes of [
      { enabled: "yes" },
      { definitions: { "PR Queue": PR_QUEUE } },
      { definitions: { ops: { description: "", owns: [] } } },
      { definitions: { ops: { description: "Machines.", owns: ["commit"] } } },
      { definitions: { ops: { description: "Machines.", owns: [], driver: "program" } } },
      { definitions: { ops: { owns: [] } } },
    ]) {
      expect(() => decodePatch({ lanes })).toThrow();
    }
    expect(FORK_PERSON_ONLY_KEYS).toContain("lanes");
  });
});

describe("lane items", () => {
  it("reads and writes a pull request or issue reference", () => {
    expect(parseLaneItemRef("pr:owner/repo#12")).toEqual({
      kind: "pr",
      repo: "owner/repo",
      number: 12,
    });
    expect(parseLaneItemRef("issue:some-org/a.repo_x#1")).toEqual({
      kind: "issue",
      repo: "some-org/a.repo_x",
      number: 1,
    });
    expect(formatLaneItemRef({ kind: "issue", repo: "owner/repo", number: 7 })).toBe(
      "issue:owner/repo#7",
    );
    for (const text of [
      "pr:repo#1",
      "pr:owner/repo#0",
      "pr:owner/repo#01",
      "pr:owner/repo",
      "commit:owner/repo#1",
      "PR:owner/repo#1",
      " pr:owner/repo#1",
      "pr:owner/repo/x#1",
      "pr:owner/repo#12345678901",
    ]) {
      expect(parseLaneItemRef(text)).toBeNull();
    }
  });
});

const owner = {
  projectId: "project-1",
  lane: "pr-queue",
  generation: 3,
  threadId: "thread-lane",
};

describe("lane records", () => {
  const decodeClaim = Schema.decodeUnknownSync(LaneClaim);
  const decodeNote = Schema.decodeUnknownSync(LaneNote);
  const decodeEscalation = Schema.decodeUnknownSync(LaneEscalation);

  it("decodes a claim, and refuses one without an item, generation or a good lane", () => {
    const claim = { item: "pr:owner/repo#12", ...owner, claimedAt: "2026-11-28T10:00:00.000Z" };
    expect(decodeClaim(claim)).toEqual(claim);
    expect(() => decodeClaim({ ...claim, item: "owner/repo#12" })).toThrow();
    expect(() => decodeClaim({ ...claim, generation: -1 })).toThrow();
    expect(() => decodeClaim({ ...claim, generation: undefined })).toThrow();
    expect(() => decodeClaim({ ...claim, lane: "PR Queue" })).toThrow();
  });

  it("keeps a record's lane name that the settings no longer define", () => {
    const claim = {
      item: "issue:owner/repo#4",
      ...owner,
      lane: "campaign-docs",
      claimedAt: "2026-11-28T10:00:00.000Z",
    };
    expect(decodeClaim(claim).lane).toBe("campaign-docs");
  });

  it("decodes a note without items as about no item, and ignores a field a newer build adds", () => {
    const note = { ...owner, at: "2026-11-28T10:00:00.000Z", text: "Two PRs merged." };
    expect(decodeNote({ ...note, later: true })).toEqual({ ...note, items: [] });
    expect(() => decodeNote({ ...note, text: "" })).toThrow();
    expect(() => decodeNote({ ...note, text: "x".repeat(2_001) })).toThrow();
    expect(() => decodeNote({ ...note, items: ["pr:owner/repo"] })).toThrow();
  });

  it("decodes an escalation, which needs an id to drop a repeat delivery", () => {
    const escalation = {
      id: "esc-1",
      ...owner,
      at: "2026-11-28T10:00:00.000Z",
      text: "PR 12 needs an owner decision.",
      items: ["pr:owner/repo#12"],
    };
    expect(decodeEscalation(escalation)).toEqual(escalation);
    expect(() => decodeEscalation({ ...escalation, id: undefined })).toThrow();
  });
});
