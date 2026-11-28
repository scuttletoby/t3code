// Fork (issue 1156, phase 1): the `lanes` settings block and the lane contracts. An old or bad file
// reads as off; a bad lane drops alone; a save is strict; old records decode.
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  BUILT_IN_LANE_DEFINITIONS,
  findLaneDefinition,
  formatLaneItemRef,
  LaneClaim,
  laneDefinitions,
  LaneEscalation,
  LaneItemRef,
  LaneNote,
  parseLaneItemRef,
} from "./forkLanes.ts";
import { FORK_PERSON_ONLY_KEYS } from "./forkSettings.ts";
import { ServerSettings, ServerSettingsPatch } from "./settings.ts";

const decodeServer = Schema.decodeUnknownSync(ServerSettings);
const encodeServer = Schema.encodeSync(ServerSettings);
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const decodeItemRef = Schema.decodeUnknownExit(LaneItemRef);

const PR_QUEUE = BUILT_IN_LANE_DEFINITIONS["pr-queue"]!;
const ISSUES = BUILT_IN_LANE_DEFINITIONS["issues"]!;
const lanesOf = (lanes: unknown) => laneDefinitions(decodeServer({ lanes }).lanes);

describe("lanes settings", () => {
  it("reads an old file, or anything but an object, as off with only the built-in lanes", () => {
    for (const lanes of [undefined, null, true, "on", [], [{ enabled: true }]]) {
      const settings = decodeServer({ lanes, enableAgentBrowserAccess: false });
      expect(settings.lanes).toEqual({ enabled: false, definitions: {} });
      expect(laneDefinitions(settings.lanes)).toEqual(BUILT_IN_LANE_DEFINITIONS);
      expect(settings.enableAgentBrowserAccess).toBe(false);
    }
    expect(Object.keys(BUILT_IN_LANE_DEFINITIONS)).toEqual(["pr-queue", "issues"]);
  });

  it("is on only for enabled: true", () => {
    expect(decodeServer({ lanes: { enabled: true } }).lanes).toEqual({
      enabled: true,
      definitions: {},
    });
    for (const enabled of ["yes", 1, null]) {
      expect(decodeServer({ lanes: { enabled } }).lanes.enabled).toBe(false);
    }
  });

  it("stores nothing for definitions that is not an object, so only the built-in lanes exist", () => {
    for (const definitions of [{}, null, [], "pr-queue"]) {
      expect(lanesOf({ enabled: true, definitions })).toEqual(BUILT_IN_LANE_DEFINITIONS);
    }
  });

  it("adds, changes and removes lanes over the built-in ones", () => {
    const mine = { description: "Mine.", owns: ["pr", "issue"], driver: "agent" };
    const lanes = lanesOf({
      definitions: {
        ops: { description: "Ops.", owns: ["issue"] },
        "pr-queue": mine,
        issues: false,
      },
    });
    expect(lanes).toEqual({
      "pr-queue": mine,
      ops: { description: "Ops.", owns: ["issue"], driver: "agent" },
    });
    expect(Object.keys(lanes)).toEqual(["pr-queue", "ops"]);
  });

  it("drops a bad lane name or definition alone and keeps the others", () => {
    const settings = decodeServer({
      lanes: {
        enabled: true,
        definitions: {
          ops: { description: "Machines and CI.", owns: ["issue"], driver: "agent", later: 1 },
          "PR Queue": { description: "Not a lane name.", owns: ["pr"] },
          "-lead": { description: "Not a lane name.", owns: ["pr"] },
          ["x".repeat(41)]: { description: "Too long a name.", owns: ["pr"] },
          program: { description: "A driver not built yet.", owns: ["pr"], driver: "program" },
          kinds: { description: "An unknown item kind.", owns: ["commit"] },
          none: { description: "Owns nothing.", owns: [] },
          twice: { description: "Owns pr twice.", owns: ["pr", "pr"] },
          blank: { description: "  ", owns: ["issue"] },
          bare: "a lane",
          custom: false,
          issues: false,
        },
      },
    });
    expect(settings.lanes.definitions).toEqual({
      ops: { description: "Machines and CI.", owns: ["issue"], driver: "agent" },
      issues: false,
    });
  });

  it("writes the value it read back to the file", () => {
    const settings = decodeServer({
      lanes: { enabled: true, definitions: { "pr-queue": PR_QUEUE, issues: false } },
    });
    expect(decodeServer(encodeServer(settings)).lanes).toEqual(settings.lanes);
  });

  it("finds a defined lane and refuses a name the settings do not define", () => {
    const { lanes } = decodeServer({ lanes: { definitions: { issues: false } } });
    expect(findLaneDefinition(lanes, "pr-queue")).toEqual(PR_QUEUE);
    expect(findLaneDefinition(decodeServer({}).lanes, "issues")).toEqual(ISSUES);
    for (const name of ["issues", "ops", "toString", "constructor", "__proto__", "PR-QUEUE", ""]) {
      expect(findLaneDefinition(lanes, name)).toBeUndefined();
    }
  });

  it("takes a good save, refuses a bad one, and is person-only", () => {
    expect(decodePatch({ lanes: { enabled: true } }).lanes).toEqual({ enabled: true });
    expect(
      decodePatch({
        lanes: {
          definitions: {
            ops: { description: "Machines.", owns: ["issue"] },
            issues: false,
            x: null,
          },
        },
      }).lanes,
    ).toEqual({
      definitions: {
        ops: { description: "Machines.", owns: ["issue"], driver: "agent" },
        issues: false,
        x: null,
      },
    });
    for (const lanes of [
      { enabled: "yes" },
      { definitions: { "PR Queue": PR_QUEUE } },
      { definitions: { ops: { description: "", owns: ["pr"] } } },
      { definitions: { ops: { description: "Machines.", owns: ["commit"] } } },
      { definitions: { ops: { description: "Machines.", owns: [] } } },
      { definitions: { ops: { description: "Machines.", owns: ["pr", "pr"] } } },
      { definitions: { ops: { description: "Machines.", owns: ["pr"], driver: "program" } } },
      { definitions: { ops: { owns: ["pr"] } } },
      { definitions: { ops: false } },
      { definitions: { issues: true } },
    ]) {
      expect(() => decodePatch({ lanes })).toThrow();
    }
    expect(FORK_PERSON_ONLY_KEYS).toContain("lanes");
  });
});

describe("lane items", () => {
  it("reads a pull request or issue reference, owner and repository in lower case", () => {
    expect(parseLaneItemRef("pr:owner/repo#12")).toEqual({
      kind: "pr",
      repo: "owner/repo",
      number: 12,
    });
    expect(parseLaneItemRef("PR:Some-Org/A.Repo_x#1")).toEqual({
      kind: "pr",
      repo: "some-org/a.repo_x",
      number: 1,
    });
  });

  it("gives one reference per item, whatever the case it was written in", () => {
    const refs = ["pr:Owner/Repo#1", "pr:owner/repo#1", "PR:OWNER/REPO#1"].map((text) =>
      formatLaneItemRef(parseLaneItemRef(text)!),
    );
    expect(new Set(refs)).toEqual(new Set(["pr:owner/repo#1"]));
    expect(formatLaneItemRef({ kind: "issue", repo: "Owner/Repo", number: 7 })).toBe(
      "issue:owner/repo#7",
    );
    expect(decodeItemRef("pr:Owner/repo#1")._tag).toBe("Failure");
  });

  it("refuses text that is not a reference, and . or .. as owner or repository", () => {
    for (const text of [
      "pr:repo#1",
      "pr:owner/repo#0",
      "pr:owner/repo#01",
      "pr:owner/repo",
      "commit:owner/repo#1",
      " pr:owner/repo#1",
      "pr:owner/repo/x#1",
      "pr:owner/repo#12345678901",
      "pr:../..#1",
      "pr:./repo#1",
      "pr:owner/..#1",
      "pr:owner/.#1",
    ]) {
      expect(parseLaneItemRef(text)).toBeNull();
    }
    // A dot inside a name is fine.
    expect(parseLaneItemRef("pr:owner/.github#3")?.repo).toBe("owner/.github");
    expect(parseLaneItemRef("pr:owner/...#3")?.repo).toBe("owner/...");
    expect(formatLaneItemRef({ kind: "pr", repo: "../..", number: 1 })).toBeNull();
    expect(formatLaneItemRef({ kind: "pr", repo: "owner/repo", number: 0 })).toBeNull();
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
