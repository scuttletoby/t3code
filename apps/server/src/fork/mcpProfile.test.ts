import { ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  MCP_PROFILE_MAX_LINEAGE,
  type McpProfileResolution,
  type McpProfileSettings,
  type McpProfileThread,
  carryMcpProfile,
  isEveryMcpTool,
  resolveMcpProfile,
  isWithinMcpProfile,
  readCarried,
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

  it.each(["all", "t3-only", "assistant"])(
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
    ["an unknown field (written by a newer build)", { servers: [], t3Tools: [], hosts: ["a"] }],
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

  it("never looks up an empty name: a blank pick or default falls through (#661)", () => {
    // So a "missing" refusal always has a name to show.
    const settings = { ...fromFile({}), mcpProfile: " ", mcpThreadProfiles: { t1: "" } };
    expect(resolved(resolveMcpProfile(settings, [top("t1")])).name).toBe("all");
    const sub = { ...settings, mcpSubagentProfile: "" };
    expect(resolved(resolveMcpProfile(sub, [child("t1"), top("p1")])).name).toBe("all");
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

describe("MCP profile resolver: a profile another machine carried with its task (#532)", () => {
  const carried = (servers: unknown, t3Tools: unknown = "all", name = "remote") => ({
    name,
    servers,
    t3Tools,
  });

  it("cuts a task delegated here to its remote parent's t3-only, with nothing set here", () => {
    const result = resolved(
      resolveMcpProfile(fromFile({}), [child("task")], carried([], "all", "t3-only")),
    );
    expect(result.name).toBe("all");
    expect(result.profile).toEqual({ servers: [], t3Tools: "all" });
    expect(result.narrowedByParent).toBe(true);
  });

  it("keeps only the servers both machines' profiles name", () => {
    const settings = fromFile({ mcpProfiles: PROFILES, mcpSubagentProfile: "docs" });
    const result = resolved(
      resolveMcpProfile(settings, [child("task")], carried(["docs", "github"])),
    );
    expect(result.profile.servers).toEqual(["docs"]);
  });

  it("gives a carried list as is to a task whose own profile here is all", () => {
    const result = resolved(resolveMcpProfile(fromFile({}), [child("task")], carried(["docs"])));
    expect(result.profile.servers).toEqual(["docs"]);
  });

  it("never widens past this machine's own profile for the task's role", () => {
    const settings = fromFile({ mcpSubagentProfile: "t3-only" });
    const result = resolved(
      resolveMcpProfile(settings, [child("task")], carried("all", "all", "all")),
    );
    expect(result.name).toBe("t3-only");
    expect(result.profile.servers).toEqual([]);
    expect(result.narrowedByParent).toBe(false);
  });

  it("cuts a sub-agent of the delegated task through its parent here", () => {
    const result = resolved(
      resolveMcpProfile(fromFile({}), [child("sub"), child("task")], carried([], "all", "t3-only")),
    );
    expect(result.profile.servers).toEqual([]);
  });

  it("lets the owner's pick for the task here stand", () => {
    const settings = fromFile({ mcpThreadProfiles: { task: "all" } });
    const result = resolved(resolveMcpProfile(settings, [child("task")], carried([])));
    expect(result.source).toBe("thread-pick");
    expect(isEveryMcpTool(result.profile)).toBe(true);
  });

  it("keeps a T3 tool group this build does not know as nothing", () => {
    const result = resolved(
      resolveMcpProfile(fromFile({}), [child("task")], carried("all", ["threads", "newer"])),
    );
    expect(result.profile.t3Tools).toEqual(["threads"]);
  });

  it.each([
    ["a field this version does not know", { ...carried([]), resources: [] }, "resources"],
    ["no name", { servers: [], t3Tools: "all" }, "it has no name"],
    ["not an object", "t3-only", "it is not an object"],
    ["servers that are not a list", carried("none"), "`servers`"],
  ])("refuses, never widens, a carried profile with %s", (_name, value, issue) => {
    const result = refused(resolveMcpProfile(fromFile({}), [child("task")], value));
    expect(result.problem).toBe("carried");
    expect(result.message).toContain(issue);
    expect(result.message).toContain("update T3 here");
    // A sub-agent of that task is refused with it.
    const sub = refused(resolveMcpProfile(fromFile({}), [child("sub"), child("task")], value));
    expect(sub.problem).toBe("parent");
  });

  it("gives a task with nothing carried (an older sender) this machine's profile, as before", () => {
    const settings = fromFile({ mcpSubagentProfile: "t3-only" });
    expect(resolved(resolveMcpProfile(settings, [child("task")], undefined)).name).toBe("t3-only");
    expect(isEveryMcpTool(resolved(resolveMcpProfile(fromFile({}), [child("task")])).profile)).toBe(
      true,
    );
  });

  it("gives the same answer when it runs again", () => {
    const value = carried(["docs"]);
    const first = resolveMcpProfile(fromFile({}), [child("task")], value);
    expect(resolveMcpProfile(fromFile({}), [child("task")], value)).toEqual(first);
    expect(value).toEqual(carried(["docs"]));
  });
});

describe("MCP profile resolver: a machine set to refuse carried profiles (#640)", () => {
  const carried = (servers: unknown, t3Tools: unknown = "all", name = "remote") => ({
    name,
    servers,
    t3Tools,
  });
  const REFUSE = { mcpCarriedProfiles: "refuse" };

  it("refuses a task that carries a narrower profile, and its sub-agents with it", () => {
    const settings = fromFile(REFUSE);
    const task = refused(resolveMcpProfile(settings, [child("task")], carried([])));
    expect(task.problem).toBe("carried-refused");
    expect(task.message).toContain('"remote"');
    expect(task.message).toContain('set mcpCarriedProfiles to "apply" here');
    const sub = refused(resolveMcpProfile(settings, [child("sub"), child("task")], carried([])));
    expect(sub.problem).toBe("parent");
    // A T3-tool cut alone is narrower too.
    const tools = refused(
      resolveMcpProfile(settings, [child("task")], carried("all", ["threads"])),
    );
    expect(tools.problem).toBe("carried-refused");
  });

  it("refuses a carried profile it cannot read, never giving the task all", () => {
    const settings = fromFile(REFUSE);
    for (const value of [{ ...carried([]), resources: [] }, "t3-only", carried("none")]) {
      expect(refused(resolveMcpProfile(settings, [child("task")], value)).problem).toBe(
        "carried-refused",
      );
    }
  });

  it("names no empty profile in a refusal: it says the profile cannot be read (#653)", () => {
    // Not an object, or no name: the carried value has no name to show.
    for (const value of [
      "t3-only",
      7,
      null,
      { servers: [], t3Tools: "all" },
      carried([], "all", " "),
    ]) {
      for (const file of [REFUSE, {}]) {
        const settings = fromFile(file);
        const task = refused(resolveMcpProfile(settings, [child("task")], value));
        const sub = refused(resolveMcpProfile(settings, [child("sub"), child("task")], value));
        for (const message of [task.message, sub.message]) {
          expect(message).not.toMatch(/profile\s*"\s*"/);
          expect(message).not.toContain('""');
        }
        expect(sub.message).toContain("The parent thread's MCP profile cannot be applied");
        if (file === REFUSE) {
          expect(task.message).toContain(
            "delegated here with an MCP profile this version cannot read (",
          );
        } else {
          expect(task.message).toMatch(/^The MCP profile of the thread on another machine/);
        }
      }
    }
    // A carried name is still shown.
    const named = refused(resolveMcpProfile(fromFile(REFUSE), [child("task")], carried([])));
    expect(named.message).toContain('with its MCP profile "remote", and');
  });

  it("does not tell the owner to set apply for a carried profile apply cannot read (#661)", () => {
    for (const value of [{ ...carried([]), resources: [] }, "t3-only", carried("none")]) {
      const task = refused(resolveMcpProfile(fromFile(REFUSE), [child("task")], value));
      expect(task.problem).toBe("carried-refused");
      expect(task.message).not.toContain('set mcpCarriedProfiles to "apply"');
      // It says why it cannot be read, as apply would.
      const apply = refused(resolveMcpProfile(fromFile({}), [child("task")], value));
      expect(apply.problem).toBe("carried");
      const issue = /\(([^)]*)\)/.exec(apply.message)?.[1];
      expect(issue).toBeTruthy();
      expect(task.message).toContain(`(${issue})`);
    }
  });

  it("tells a sub-agent, and its sub-agent, the reason at the top of the chain (#661)", () => {
    const settings = fromFile(REFUSE);
    const value = carried([]);
    const task = refused(resolveMcpProfile(settings, [child("task")], value));
    for (const lineage of [
      [child("sub"), child("task")],
      [child("grand"), child("sub"), child("task")],
    ]) {
      const sub = refused(resolveMcpProfile(settings, lineage, value));
      expect(sub.problem).toBe("parent");
      expect(sub.message).toContain(`The reason up the chain: ${task.message}`);
      // Once, however deep: each generation passes the root reason on, not its own message.
      expect(sub.message.split("The reason up the chain").length).toBe(2);
      expect(sub.message.split('mcpCarriedProfiles is "refuse"').length).toBe(2);
    }
    // A local parent whose profile is gone: the child says which and why.
    const local = fromFile({ mcpThreadProfiles: { p1: "gone" } });
    const own = refused(resolveMcpProfile(local, [top("p1")]));
    const sub = refused(resolveMcpProfile(local, [child("c1"), top("p1")]));
    expect(sub.message).toContain(`The reason up the chain: ${own.message}`);
  });

  it("makes every caller say what this machine does with a carried profile (#653)", () => {
    const { mcpCarriedProfiles, ...rest } = fromFile(REFUSE);
    expect(mcpCarriedProfiles).toBe("refuse");
    // Typecheck: settings without the key are not resolver settings, so no caller gets apply by
    // leaving it out.
    // @ts-expect-error mcpCarriedProfiles is required
    const partial: McpProfileSettings = rest;
    expect(partial).not.toHaveProperty("mcpCarriedProfiles");
  });

  it("takes a task that carries all, which cuts nothing, as apply would", () => {
    const value = carried("all", "all", "all");
    const refuse = resolved(resolveMcpProfile(fromFile(REFUSE), [child("task")], value));
    const apply = resolved(resolveMcpProfile(fromFile({}), [child("task")], value));
    expect(refuse).toEqual(apply);
    expect(isEveryMcpTool(refuse.profile)).toBe(true);
  });

  it("leaves the owner's pick for the task, and tasks with nothing carried, as before", () => {
    const picked = fromFile({ ...REFUSE, mcpThreadProfiles: { task: "t3-only" } });
    expect(resolved(resolveMcpProfile(picked, [child("task")], carried(["docs"]))).name).toBe(
      "t3-only",
    );
    const settings = fromFile({ ...REFUSE, mcpSubagentProfile: "t3-only" });
    expect(resolved(resolveMcpProfile(settings, [child("task")])).name).toBe("t3-only");
    expect(resolved(resolveMcpProfile(fromFile(REFUSE), [top("local")])).name).toBe("all");
  });

  it("follows the setting on each run: refuse, then apply, then refuse again", () => {
    const value = carried(["docs"]);
    const runs = [REFUSE, {}, { mcpCarriedProfiles: "apply" }, REFUSE].map(
      (file) => resolveMcpProfile(fromFile(file), [child("task")], value)._tag,
    );
    expect(runs).toEqual(["CannotApply", "Resolved", "Resolved", "CannotApply"]);
  });
});

describe("isWithinMcpProfile (#681)", () => {
  const set = (
    servers: "all" | ReadonlyArray<string>,
    t3Tools: "all" | ReadonlyArray<"orchestration" | "threads"> = "all",
  ) => ({ servers, t3Tools }) as Parameters<typeof isWithinMcpProfile>[0];

  it("all covers everything, and only all covers all", () => {
    expect(isWithinMcpProfile(set("all"), set("all"))).toBe(true);
    expect(isWithinMcpProfile(set(["docs"]), set("all"))).toBe(true);
    expect(isWithinMcpProfile(set("all"), set(["docs"]))).toBe(false);
    expect(isWithinMcpProfile(set([], "all"), set([], ["threads"]))).toBe(false);
  });

  it("a keep-list is within another only when it keeps nothing the other does not", () => {
    expect(isWithinMcpProfile(set([]), set(["docs"]))).toBe(true);
    expect(isWithinMcpProfile(set(["docs"]), set(["docs", "search"]))).toBe(true);
    expect(isWithinMcpProfile(set(["docs", "search"]), set(["docs"]))).toBe(false);
    expect(isWithinMcpProfile(set(["docs"], ["threads"]), set(["docs"], ["threads"]))).toBe(true);
    expect(isWithinMcpProfile(set(["docs"], ["threads"]), set(["docs"], []))).toBe(false);
  });
});

describe('builtinTools "none" (assistant)', () => {
  const settings = fromFile({
    mcpProfiles: { bare: { servers: "all", t3Tools: "all", builtinTools: "none" }, ...PROFILES },
    mcpThreadProfiles: { assistant: "bare", "lean-parent": "lean" },
  });

  it("reads an old profile without the field as before: built-in tools kept", () => {
    const result = resolved(resolveMcpProfile(settings, [top("lean-parent")]));
    expect(result.profile).toEqual(PROFILES.lean);
    expect(result.profile).not.toHaveProperty("builtinTools");
    expect(isEveryMcpTool(resolved(resolveMcpProfile(settings, [top("other")])).profile)).toBe(
      true,
    );
  });

  it("is narrower than all, and a child keeps it: never wider than its parent", () => {
    const own = resolved(resolveMcpProfile(settings, [top("assistant")]));
    expect(own.profile).toEqual({ servers: "all", t3Tools: "all", builtinTools: "none" });
    expect(isEveryMcpTool(own.profile)).toBe(false);
    expect(isWithinMcpProfile(own.profile, { servers: "all", t3Tools: "all" })).toBe(true);
    expect(isWithinMcpProfile({ servers: "all", t3Tools: "all" }, own.profile)).toBe(false);
    const sub = resolved(resolveMcpProfile(settings, [child("c1"), top("assistant")]));
    expect(sub.profile.builtinTools).toBe("none");
    expect(sub.narrowedByParent).toBe(true);
  });

  it("travels with a delegated task and comes back the same on the other machine", () => {
    const own = resolved(resolveMcpProfile(settings, [top("assistant")]));
    const carried = carryMcpProfile(own);
    expect(carried).toEqual({ name: "bare", servers: "all", t3Tools: "all", builtinTools: "none" });
    const there = resolved(resolveMcpProfile(fromFile({}), [child("task")], carried));
    expect(there.profile.builtinTools).toBe("none");
    // A task without it carries no field, so an older receiver reads it as before.
    expect(carryMcpProfile(resolved(resolveMcpProfile(settings, [top("x")])))).not.toHaveProperty(
      "builtinTools",
    );
  });

  it("the built-in assistant: no servers or built-in tools, read, start and message; a child keeps it", () => {
    const picked = fromFile({ mcpThreadProfiles: { a1: "assistant" } });
    const own = resolved(resolveMcpProfile(picked, [top("a1")]));
    expect([own.name, own.source]).toEqual(["assistant", "thread-pick"]);
    expect(own.profile).toEqual({
      servers: [],
      t3Tools: ["read", "start", "message"],
      builtinTools: "none",
    });
    const sub = resolved(resolveMcpProfile(picked, [child("c1"), top("a1")]));
    expect(sub.profile).toEqual(own.profile);
  });

  it.each([["some"], [false], [["Bash"]]])("refuses, never widens, builtinTools %j", (value) => {
    const broken = fromFile({
      mcpProfiles: { odd: { servers: "all", t3Tools: "all", builtinTools: value } },
      mcpProfile: "odd",
    });
    expect(refused(resolveMcpProfile(broken, [top("t1")])).message).toContain("builtinTools");
    const carried = { name: "odd", servers: "all", t3Tools: "all", builtinTools: value };
    expect(refused(resolveMcpProfile(fromFile({}), [child("task")], carried)).message).toContain(
      "builtinTools",
    );
  });
});

describe("the assistant thread is never wider than the assistant profile (#953)", () => {
  const ASSISTANT_SET = {
    servers: [],
    t3Tools: ["read", "start", "message"],
    builtinTools: "none",
  };
  const roles = { assistant: "a1", orchestrators: {} };

  it("a managed policy that drops its pick: the defaults' all is cut down to assistant", () => {
    // The policy owns mcpThreadProfiles as a whole and leaves the assistant's pick out.
    const dropped = fromFile({
      mcpProfiles: PROFILES,
      threadRoles: roles,
      mcpThreadProfiles: { other: "docs" },
    });
    const own = resolved(resolveMcpProfile(dropped, [top("a1")]));
    expect([own.name, own.source]).toEqual(["assistant", "assistant-role"]);
    expect(own.profile).toEqual(ASSISTANT_SET);
    // A project default and a wider pick are cut too; a child gets no more than its parent.
    const project = resolved(resolveMcpProfile(dropped, [top("a1", { mcpProfile: "docs" })]));
    expect(project.profile).toEqual(ASSISTANT_SET);
    const wide = fromFile({ threadRoles: roles, mcpThreadProfiles: { a1: "all" } });
    expect(resolved(resolveMcpProfile(wide, [top("a1")])).profile).toEqual(ASSISTANT_SET);
    expect(resolved(resolveMcpProfile(dropped, [child("c1"), top("a1")])).profile).toEqual(
      ASSISTANT_SET,
    );
  });

  it("its own pick stands when it is within assistant; other threads are not touched", () => {
    const settings = fromFile({
      mcpProfiles: { tiny: { servers: [], t3Tools: ["read"], builtinTools: "none" } },
      threadRoles: roles,
      mcpThreadProfiles: { a1: "tiny" },
    });
    const own = resolved(resolveMcpProfile(settings, [top("a1")]));
    expect([own.name, own.source]).toEqual(["tiny", "thread-pick"]);
    const picked = fromFile({ threadRoles: roles, mcpThreadProfiles: { a1: "assistant" } });
    expect(resolved(resolveMcpProfile(picked, [top("a1")])).source).toBe("thread-pick");
    const other = resolved(resolveMcpProfile(settings, [top("t2")]));
    expect([other.name, isEveryMcpTool(other.profile)]).toEqual(["all", true]);
    // No role: the thread that held the pick gets what any thread gets.
    expect(isEveryMcpTool(resolved(resolveMcpProfile(fromFile({}), [top("a1")])).profile)).toBe(
      true,
    );
  });

  it("refuses, never widens, when the built-in assistant cannot be read or the pick is broken", () => {
    const shadowed = fromFile({
      mcpProfiles: { assistant: { servers: "all", t3Tools: "all" } },
      threadRoles: roles,
    });
    expect(refused(resolveMcpProfile(shadowed, [top("a1")])).profileName).toBe("assistant");
    const missing = fromFile({ threadRoles: roles, mcpThreadProfiles: { a1: "gone" } });
    expect(refused(resolveMcpProfile(missing, [top("a1")])).problem).toBe("missing");
  });
});

describe("the narrow groups read, start and message (#980)", () => {
  const WHOLE = ["orchestration", "threads", "projects"] as const;
  const set = (t3Tools: "all" | ReadonlyArray<string>) =>
    ({ servers: [], t3Tools }) as Parameters<typeof isWithinMcpProfile>[0];

  it("a narrow group is within the whole groups its tools are in, never the other way", () => {
    expect(isWithinMcpProfile(set(["read", "start", "message"]), set(WHOLE))).toBe(true);
    expect(isWithinMcpProfile(set(["message"]), set(["orchestration"]))).toBe(true);
    // `read` has tools in all three: two of them do not cover it.
    expect(isWithinMcpProfile(set(["read"]), set(["orchestration", "threads"]))).toBe(false);
    expect(isWithinMcpProfile(set(WHOLE), set(["read", "start", "message"]))).toBe(false);
    expect(isWithinMcpProfile(set(["threads"]), set(["read"]))).toBe(false);
  });

  it("an existing profile with the whole groups keeps them; its child keeps a narrow default", () => {
    const settings = fromFile({
      mcpProfiles: {
        whole: { servers: [], t3Tools: [...WHOLE] },
        reader: { servers: [], t3Tools: ["read"] },
      },
      mcpThreadProfiles: { w1: "whole" },
      mcpSubagentProfile: "reader",
    });
    const own = resolved(resolveMcpProfile(settings, [top("w1")]));
    expect(own.profile.t3Tools).toEqual([...WHOLE]);
    const sub = resolved(resolveMcpProfile(settings, [child("c1"), top("w1")]));
    expect([sub.name, sub.profile.t3Tools, sub.narrowedByParent]).toEqual([
      "reader",
      ["read"],
      false,
    ]);
  });

  it("the assistant thread with a whole-group pick is cut to read, start and message", () => {
    const settings = fromFile({
      mcpProfiles: { whole: { servers: [], t3Tools: [...WHOLE], builtinTools: "none" } },
      threadRoles: { assistant: "a1", orchestrators: {} },
      mcpThreadProfiles: { a1: "whole" },
    });
    const own = resolved(resolveMcpProfile(settings, [top("a1")]));
    expect([own.name, own.source, own.profile.t3Tools]).toEqual([
      "assistant",
      "assistant-role",
      ["read", "start", "message"],
    ]);
  });

  it("a carried assistant recorded before the split is re-narrowed as it is read", () => {
    const old = {
      name: "assistant",
      servers: [],
      t3Tools: [...WHOLE],
      builtinTools: "none",
    };
    expect(readCarried(old)).toEqual({
      ok: true,
      name: "assistant",
      profile: { servers: [], t3Tools: ["read", "start", "message"], builtinTools: "none" },
    });
    // A delegated task that carries it gets no more than today's assistant.
    const there = resolved(resolveMcpProfile(fromFile({}), [child("task")], old));
    expect(there.profile.t3Tools).toEqual(["read", "start", "message"]);
    // An older build's own profile named assistant is cut the same way, never widened.
    expect(readCarried({ name: "assistant", servers: "all", t3Tools: ["issues"] })).toEqual({
      ok: true,
      name: "assistant",
      profile: { servers: [], t3Tools: [], builtinTools: "none" },
    });
    // Another name is read as it was carried.
    const docs = { name: "docs", servers: [], t3Tools: [...WHOLE] };
    expect(readCarried(docs)).toEqual({
      ok: true,
      name: "docs",
      profile: { servers: [], t3Tools: [...WHOLE] },
    });
  });
});
