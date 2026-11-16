import { ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  MCP_PROFILE_MAX_LINEAGE,
  type McpProfileResolution,
  type McpProfileThread,
  isEveryMcpTool,
  resolveMcpProfile,
} from "./mcpProfile.ts";

/** The settings as a load decodes them from a settings file's JSON. */
const decodeSettingsFile = Schema.decodeSync(ServerSettings);
const fromFile = (file: Record<string, unknown>) => decodeSettingsFile(file);

const top = (threadId: string, project?: McpProfileThread["project"]): McpProfileThread => ({
  threadId,
  delegated: false,
  ...(project === undefined ? {} : { project }),
});
const child = (threadId: string, project?: McpProfileThread["project"]): McpProfileThread => ({
  ...top(threadId, project),
  delegated: true,
});

const PROFILES = {
  lean: { servers: [], t3Tools: ["orchestration", "threads", "pull-requests"] },
  review: { servers: ["docs"], t3Tools: ["threads"] },
  docs: { servers: ["docs", "game"], t3Tools: "all" },
};

const resolved = (result: McpProfileResolution) => {
  if (result._tag !== "Resolved") throw new Error(`expected Resolved, got ${result.message}`);
  return result;
};
const refused = (result: McpProfileResolution) => {
  if (result._tag !== "CannotApply") throw new Error(`expected CannotApply, got ${result.name}`);
  return result;
};

describe("MCP profile resolver: old and empty state", () => {
  it.each([
    ["no settings file (no keys)", {}],
    [
      "empty values",
      { mcpProfiles: {}, mcpProfile: "", mcpSubagentProfile: "", mcpThreadProfiles: {} },
    ],
    ["the defaults written out", { mcpProfile: "all", mcpSubagentProfile: "inherit" }],
  ])("%s: every thread gets everything, as today", (_name, file) => {
    const settings = fromFile(file);
    for (const lineage of [[top("t1")], [child("c1"), top("t1")], [top("f1"), top("t1")]]) {
      const result = resolved(resolveMcpProfile(settings, lineage));
      expect(result.name).toBe("all");
      expect(isEveryMcpTool(result.profile)).toBe(true);
      expect(result.narrowedByParent).toBe(false);
    }
  });

  it("knows the built-ins with no profiles defined", () => {
    const settings = fromFile({ mcpProfile: "t3-only" });
    expect(resolved(resolveMcpProfile(settings, [top("t1")])).profile).toEqual({
      servers: [],
      t3Tools: "all",
    });
  });

  it("reads an empty keep-list as none, not as all", () => {
    const settings = fromFile({
      mcpProfiles: { bare: { servers: [], t3Tools: [] } },
      mcpProfile: "bare",
    });
    expect(resolved(resolveMcpProfile(settings, [top("t1")])).profile).toEqual({
      servers: [],
      t3Tools: [],
    });
  });

  it.each(["all", "t3-only"])(
    "refuses %s when a hand edit put a profile of that name in the file",
    (name) => {
      const file = {
        mcpProfiles: { [name]: { servers: [], t3Tools: [] }, lean: PROFILES.lean },
        mcpProfile: name,
      };
      const settings = fromFile(file);
      const result = refused(resolveMcpProfile(settings, [top("t1")]));
      expect(result.problem).toBe("broken");
      expect(result.profileName).toBe(name);
      expect(result.message).toBe(
        `MCP profile "${name}" cannot be read by this version (mcpProfiles also has an entry named "${name}", a built-in profile's name), so this thread cannot start: remove or rename that entry in the settings file.`,
      );
      // An owner's pick of the same built-in is refused too; another profile still works.
      expect(
        refused(
          resolveMcpProfile(fromFile({ ...file, mcpThreadProfiles: { t2: name } }), [top("t2")]),
        ).problem,
      ).toBe("broken");
      const lean = fromFile({ ...file, mcpThreadProfiles: { t3: "lean" } });
      expect(resolved(resolveMcpProfile(lean, [top("t3")])).name).toBe("lean");
    },
  );
});

describe("MCP profile resolver: who decides", () => {
  const settings = fromFile({
    mcpProfiles: PROFILES,
    mcpProfile: "docs",
    mcpSubagentProfile: "review",
    mcpThreadProfiles: { picked: "lean", blank: "  " },
  });

  it.each([
    ["the environment default", [top("t1")], "docs", "environment"],
    ["a project's override", [top("t1", { mcpProfile: "lean" })], "lean", "project"],
    ["an empty project override", [top("t1", { mcpProfile: "" })], "docs", "environment"],
    [
      "the sub-agent rule for a delegated child",
      [child("c1"), top("t1")],
      "review",
      "subagent-rule",
    ],
    [
      "a project's sub-agent rule",
      [child("c1", { mcpSubagentProfile: "lean" }), top("t1")],
      "lean",
      "subagent-rule",
    ],
    [
      "a project's `inherit` over the server's rule",
      [child("c1", { mcpSubagentProfile: "inherit", mcpProfile: "docs" }), top("t1")],
      "docs",
      "project",
    ],
    ["the owner's pick", [top("picked")], "lean", "thread-pick"],
    [
      "the owner's pick over the sub-agent rule",
      [child("picked"), top("t1")],
      "lean",
      "thread-pick",
    ],
    ["a blank pick as no pick", [top("blank")], "docs", "environment"],
  ] as const)("%s", (_name, lineage, name, source) => {
    const result = resolved(resolveMcpProfile(settings, lineage));
    expect([result.name, result.source]).toEqual([name, source]);
  });

  it("does not apply the sub-agent rule to a fork", () => {
    // A fork has a parent but is not delegated: its role default is `mcpProfile`.
    expect(resolved(resolveMcpProfile(settings, [top("f1"), top("t1")])).name).toBe("docs");
  });
});

describe("MCP profile resolver: a child is never wider than its parent", () => {
  const settings = fromFile({
    mcpProfiles: PROFILES,
    mcpProfile: "all",
    mcpThreadProfiles: { "lean-parent": "lean", "review-parent": "review", "wide-child": "all" },
  });

  it("cuts an inheriting child down to its parent", () => {
    const result = resolved(resolveMcpProfile(settings, [child("c1"), top("lean-parent")]));
    expect(result.name).toBe("all");
    expect(result.profile).toEqual(PROFILES.lean);
    expect(result.narrowedByParent).toBe(true);
  });

  it("cuts both keep-lists to what both keep", () => {
    const narrow = fromFile({
      mcpProfiles: PROFILES,
      mcpSubagentProfile: "docs",
      mcpThreadProfiles: { "review-parent": "review" },
    });
    const result = resolved(resolveMcpProfile(narrow, [child("c1"), top("review-parent")]));
    expect(result.profile).toEqual({ servers: ["docs"], t3Tools: ["threads"] });
  });

  it("cuts a fork down like a child", () => {
    const result = resolved(resolveMcpProfile(settings, [top("f1"), top("review-parent")]));
    expect(result.profile).toEqual(PROFILES.review);
  });

  it("cuts across every generation", () => {
    const result = resolved(
      resolveMcpProfile(settings, [child("grandchild"), child("c1"), top("review-parent")]),
    );
    expect(result.profile).toEqual(PROFILES.review);
  });

  it("lets the owner's pick for the child stand", () => {
    const result = resolved(resolveMcpProfile(settings, [child("wide-child"), top("lean-parent")]));
    expect(isEveryMcpTool(result.profile)).toBe(true);
    expect(result.narrowedByParent).toBe(false);
  });

  it("grows a child back to its own default when the parent widens, not beyond", () => {
    const lineage = [child("c1"), top("p1")];
    const narrowSettings = fromFile({
      mcpProfiles: PROFILES,
      mcpSubagentProfile: "docs",
      mcpThreadProfiles: { p1: "lean" },
    });
    expect(resolved(resolveMcpProfile(narrowSettings, lineage)).profile).toEqual({
      servers: [],
      t3Tools: ["orchestration", "threads", "pull-requests"],
    });
    // The owner widens the parent to `all`: the child gets its own default, `docs`, not `all`.
    const widened = fromFile({
      mcpProfiles: PROFILES,
      mcpSubagentProfile: "docs",
      mcpThreadProfiles: { p1: "all" },
    });
    expect(resolved(resolveMcpProfile(widened, lineage)).profile).toEqual(PROFILES.docs);
  });
});

describe("MCP profile resolver: never falls back to all", () => {
  it.each([
    ["the environment names a missing profile", { mcpProfile: "gone" }, [top("t1")], "gone"],
    ["a project names a missing profile", {}, [top("t1", { mcpProfile: "renamed" })], "renamed"],
    [
      "the sub-agent rule names a missing profile",
      { mcpSubagentProfile: "gone" },
      [child("c1"), top("t1")],
      "gone",
    ],
    [
      "a thread pick names a missing profile",
      { mcpThreadProfiles: { t1: "gone" } },
      [top("t1")],
      "gone",
    ],
    ["a name no profile may have", { mcpProfile: "Not A Name" }, [top("t1")], "Not A Name"],
    ["`inherit` as a thread's profile", { mcpProfile: "inherit" }, [top("t1")], "inherit"],
    // Plain-object keys must not resolve to Object.prototype's members.
    ["a prototype key", { mcpProfile: "constructor" }, [top("t1")], "constructor"],
    // Values that are not text read as a marker no profile can have, so they never widen to all.
    ["a default that is a number", { mcpProfile: 5 }, [top("t1")], "(unreadable number)"],
    [
      "a sub-agent rule that is a list",
      { mcpSubagentProfile: ["lean"] },
      [child("c1"), top("t1")],
      "(unreadable list)",
    ],
    [
      "a thread pick that is an object",
      { mcpThreadProfiles: { t1: { profile: "lean" } } },
      [top("t1")],
      "(unreadable object)",
    ],
    [
      "a project reference that is a number",
      {},
      [top("t1", { mcpProfile: "(unreadable number)" })],
      "(unreadable number)",
    ],
  ] as const)("%s: refuses and names it", (_name, file, lineage, name) => {
    const result = refused(
      resolveMcpProfile(fromFile({ mcpProfiles: PROFILES, ...file }), lineage),
    );
    expect(result.problem).toBe("missing");
    expect(result.profileName).toBe(name);
    expect(result.message).toContain(`"${name}"`);
    expect(result.message).toContain('pick "all" for this thread');
  });

  it.each([
    [
      "an unknown field (written by a newer build)",
      { servers: [], t3Tools: [], builtinTools: "none" },
    ],
    ["a missing field", { servers: [] }],
    ["a wrong servers value", { servers: "docs", t3Tools: "all" }],
    ["a blank server name", { servers: [" "], t3Tools: "all" }],
    ["a non-string group", { servers: "all", t3Tools: [1] }],
    ["not an object", ["docs"]],
    ["null", null],
  ])("a profile with %s is broken, never wider", (_name, entry) => {
    const settings = fromFile({ mcpProfiles: { odd: entry }, mcpProfile: "odd" });
    const result = refused(resolveMcpProfile(settings, [top("t1")]));
    expect(result.problem).toBe("broken");
    expect(result.profileName).toBe("odd");
  });

  it("keeps one broken profile from touching the others", () => {
    const settings = fromFile({
      mcpProfiles: { ...PROFILES, odd: { servers: [], t3Tools: [], future: true } },
      mcpProfile: "lean",
    });
    expect(resolved(resolveMcpProfile(settings, [top("t1")])).profile).toEqual(PROFILES.lean);
  });

  it("refuses a child whose parent's profile cannot be applied", () => {
    const settings = fromFile({ mcpProfiles: PROFILES, mcpThreadProfiles: { p1: "gone" } });
    const result = refused(resolveMcpProfile(settings, [child("c1"), top("p1")]));
    expect([result.problem, result.profileName, result.threadId]).toEqual(["parent", "gone", "c1"]);
  });

  it("still starts a child the owner picked for, whatever the parent has", () => {
    const settings = fromFile({
      mcpProfiles: PROFILES,
      mcpThreadProfiles: { p1: "gone", c1: "lean" },
    });
    expect(resolved(resolveMcpProfile(settings, [child("c1"), top("p1")])).name).toBe("lean");
  });

  it.each([
    ["repeats a thread", [child("c1"), top("p1"), child("c1")]],
    [
      "is too deep",
      Array.from({ length: MCP_PROFILE_MAX_LINEAGE + 1 }, (_, index) => child(`t${index}`)),
    ],
  ])("refuses a lineage that %s", (_name, lineage) => {
    expect(refused(resolveMcpProfile(fromFile({}), lineage)).problem).toBe("lineage");
  });
});

describe("MCP profile resolver: names that match nothing", () => {
  it("keeps a T3 tool group this build does not know as nothing, and says so", () => {
    const settings = fromFile({
      mcpProfiles: { future: { servers: [], t3Tools: ["threads", "mail", "threads"] } },
      mcpProfile: "future",
    });
    const result = resolved(resolveMcpProfile(settings, [top("t1")]));
    expect(result.profile.t3Tools).toEqual(["threads"]);
    expect(result.unknownToolGroups).toEqual(["mail"]);
  });

  it("keeps a server name as written; whether it is set up is the provider's question", () => {
    const settings = fromFile({
      mcpProfiles: { one: { servers: [" docs ", "docs", "not-set-up"], t3Tools: "all" } },
      mcpProfile: "one",
    });
    expect(resolved(resolveMcpProfile(settings, [top("t1")])).profile.servers).toEqual([
      "docs",
      "not-set-up",
    ]);
  });

  it("ignores a stale pick for a thread that no longer exists", () => {
    const settings = fromFile({ mcpProfiles: PROFILES, mcpThreadProfiles: { deleted: "lean" } });
    expect(resolved(resolveMcpProfile(settings, [top("t1")])).name).toBe("all");
  });
});

describe("MCP profile resolver: runs again", () => {
  it("gives the same answer for the same input and changes nothing", () => {
    const file = {
      mcpProfiles: PROFILES,
      mcpSubagentProfile: "docs",
      mcpThreadProfiles: { p1: "review" },
    };
    const settings = fromFile(structuredClone(file));
    const lineage = [child("c1"), top("p1")];
    const mcpKeys = () => ({
      mcpProfiles: settings.mcpProfiles,
      mcpProfile: settings.mcpProfile,
      mcpSubagentProfile: settings.mcpSubagentProfile,
      mcpThreadProfiles: settings.mcpThreadProfiles,
    });
    const before = structuredClone({ settings: mcpKeys(), lineage });
    const first = resolveMcpProfile(settings, lineage);
    const second = resolveMcpProfile(settings, lineage);
    expect(second).toEqual(first);
    expect({ settings: mcpKeys(), lineage }).toEqual(before);
  });
});
