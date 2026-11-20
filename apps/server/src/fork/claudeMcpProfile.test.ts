import * as NodeServices from "@effect/platform-node/NodeServices";
import {
  ManagedPolicyDocument,
  ProjectId,
  ServerSettings,
  ServerSettingsError,
  ThreadId,
} from "@t3tools/contracts";
import { assert, describe, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Schema from "effect/Schema";
import * as TestClock from "effect/testing/TestClock";

import * as ServerConfig from "../config.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import {
  CHECK_SESSION_TIMEOUT,
  ClaudeMcpProfileError,
  EVERY_MCP_SERVER,
  type ClaudeMcpProfilePlan,
  type FindThread,
  type LineageThread,
  checkClaudeSession,
  claudeMcpCallOutsideProfile,
  claudeMcpProfileForThread,
  claudeMcpProfilePlan,
  claudeMcpServersOutsidePlan,
  makeClaudeMcpProfiles,
  planKeyFor,
} from "./claudeMcpProfile.ts";
import { type McpProfileCarried } from "./mcpProfile.ts";

const fromFile = Schema.decodeSync(ServerSettings);
const policyDocument = Schema.decodeUnknownSync(ManagedPolicyDocument);
const isRefusal = Schema.is(ClaudeMcpProfileError);
const T3 = ["t3-code"];

const thread = (id: string, parent: string | null = null): LineageThread => ({
  id: ThreadId.make(id),
  projectId: ProjectId.make("project"),
  lineage: {
    parentThreadId: parent === null ? null : ThreadId.make(parent),
    relationshipToParent: parent === null ? null : "subagent",
    rootThreadId: ThreadId.make("root"),
  },
});
const store = (threads: ReadonlyArray<LineageThread>) => (threadId: ThreadId) =>
  Effect.succeed(threads.find((entry) => entry.id === threadId) ?? null);
const noReads = () => Effect.die("read a parent thread");

const outcome = (result: ClaudeMcpProfilePlan | ClaudeMcpProfileError) =>
  isRefusal(result) ? result.message : result.options;

describe("claudeMcpProfilePlan", () => {
  it("refuses a profile that limits T3's own tools, which nothing applies yet", () => {
    const result = claudeMcpProfilePlan("lean", { servers: [], t3Tools: ["threads"] }, T3);
    assert.include(String(outcome(result)), 'MCP profile "lean" limits T3\'s own tools');
  });
});

describe("claudeMcpServersOutsidePlan", () => {
  const statuses = [
    { name: "t3-code", status: "connected" as const },
    { name: "docs", status: "pending" as const },
    { name: "claude.ai Calendar", status: "needs-auth" as const },
    { name: "off", status: "disabled" as const },
  ];
  it.each([
    { servers: "all" as const, outside: [] },
    { servers: [], outside: ["docs", "claude.ai Calendar"] },
    { servers: ["docs"], outside: ["claude.ai Calendar"] },
  ])("names what $servers leaves out, at any status but disabled", ({ servers, outside }) => {
    const plan = { name: "p", options: {}, servers };
    assert.deepStrictEqual(claudeMcpServersOutsidePlan(plan, T3, statuses), outside);
  });
});

describe("claudeMcpCallOutsideProfile", () => {
  const call = (tool_name: string, extra: object = {}) => ({ tool_name, tool_input: {}, ...extra });
  it.each([
    { name: "a tool of a named server", call: call("mcp__docs__search"), outside: undefined },
    { name: "a tool of T3's own server", call: call("mcp__t3-code__x"), outside: undefined },
    { name: "a tool of another server", call: call("mcp__github__x"), outside: "github" },
    {
      name: "a server Claude names, by its configured name",
      call: call("mcp__claude_ai_Calendar__x", { mcp_server: { name: "claude.ai Calendar" } }),
      outside: "claude.ai Calendar",
    },
    {
      name: "a named server whose name Claude rewrites in tool names",
      call: call("mcp__claude_ai_Drive__x"),
      outside: undefined,
    },
    {
      name: "a resource read from another server",
      call: { tool_name: "ReadMcpResourceTool", tool_input: { server: "github", uri: "x" } },
      outside: "github",
    },
    {
      name: "a resource listing of every server",
      call: { tool_name: "ListMcpResourcesTool", tool_input: {} },
      outside: EVERY_MCP_SERVER,
    },
    {
      name: "a resource listing of a named server",
      call: { tool_name: "ListMcpResourcesTool", tool_input: { server: "docs" } },
      outside: undefined,
    },
    {
      name: "a skill another server serves, run through Skill",
      call: { tool_name: "Skill", tool_input: { skill: "/mcp__github__triage" } },
      outside: "github",
    },
    {
      name: "a skill a named server serves, run through Skill",
      call: { tool_name: "Skill", tool_input: { skill: "mcp__docs__triage", args: "x" } },
      outside: undefined,
    },
    {
      name: "a skill no MCP server serves",
      call: { tool_name: "Skill", tool_input: { skill: "plugin:mcp__github__x" } },
      outside: undefined,
    },
    {
      name: "another server's prompt, run through the older SlashCommand",
      call: { tool_name: "SlashCommand", tool_input: { command: "/mcp__github__triage 12" } },
      outside: "github",
    },
    { name: "a tool that is not MCP", call: call("Bash"), outside: undefined },
  ])("$name", ({ call: input, outside }) => {
    assert.strictEqual(
      claudeMcpCallOutsideProfile(["docs", "claude.ai Drive"], T3, input),
      outside,
    );
  });
});

describe("planKeyFor", () => {
  it("gives overrides with no worked-out plan a key no other turn has, never all's", () => {
    const first = planKeyFor({});
    assert.isDefined(first);
    assert.notStrictEqual(planKeyFor({}), first);
  });
});

describe("makeClaudeMcpProfiles", () => {
  const readFailure = new ServerSettingsError({
    settingsPath: "settings.json",
    operation: "read-secret",
    cause: new Error("secret store unreadable"),
  });
  /** A task another machine delegated here, carrying t3-only. */
  const carriedTask: LineageThread = {
    ...thread("task"),
    delegatedFrom: {
      environmentId: "environment-there" as never,
      threadId: ThreadId.make("thread-there"),
      title: "Parent there",
      mcpProfile: { name: "t3-only", servers: [], t3Tools: "all" },
    },
  };
  /**
   * A fresh start (a server restart or a Claude adapter rebuild) that plans a thread once per
   * entry: the settings, or a failed read (it reads provider secrets too). `file` is what
   * settings.json holds: its text, `undefined` for no file, or "unreadable".
   */
  const plansFor = (
    reads: ReadonlyArray<Record<string, unknown> | "fails">,
    file: string | undefined | "unreadable" = undefined,
    policy?: unknown,
    self: LineageThread = thread("t"),
  ) =>
    Effect.gen(function* () {
      const { settingsPath } = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      if (file === "unreadable") yield* fs.makeDirectory(settingsPath);
      else if (file !== undefined) yield* fs.writeFileString(settingsPath, file);
      const service = yield* ServerSettingsService.ServerSettingsService;
      let next = 0;
      const getSettings = Effect.suspend(() => {
        const read = reads[next++];
        return read === "fails" ? Effect.fail(readFailure) : Effect.succeed(fromFile(read ?? {}));
      });
      const plan = yield* makeClaudeMcpProfiles.pipe(
        Effect.provideService(ServerSettingsService.ServerSettingsService, {
          ...service,
          getSettings,
        }),
      );
      const outcomes: Array<string> = [];
      for (const _ of reads) {
        outcomes.push(
          yield* plan(self, {}).pipe(
            Effect.match({
              onFailure: (refusal) => refusal.message,
              // A carried profile cuts an `all` thread without renaming it.
              onSuccess: (result) =>
                result.name === "all" && result.servers !== "all"
                  ? `all cut to ${JSON.stringify(result.servers)}`
                  : result.name,
            }),
          ),
        );
      }
      return outcomes;
    }).pipe(
      Effect.provide(
        Layer.mergeAll(
          ServerSettingsService.ServerSettingsService.layerTest(),
          ManagedPolicy.layerTest(policy === undefined ? undefined : policyDocument(policy)),
          Layer.fresh(ServerConfig.layerTest(process.cwd(), { prefix: "t3code-mcp-profile-" })),
        ).pipe(Layer.provideMerge(NodeServices.layer)),
      ),
    );

  it.effect("with no profile anywhere, a failed settings read gives all instead of refusing", () =>
    Effect.gen(function* () {
      assert.deepStrictEqual(yield* plansFor(["fails", {}, "fails"]), ["all", "all", "all"]);
      assert.deepStrictEqual(yield* plansFor(["fails"], "{}"), ["all"]);
    }),
  );

  it.effect("after a restart, a failed secret read keeps the profile settings.json sets", () =>
    Effect.gen(function* () {
      const file = JSON.stringify({ mcpProfile: "t3-only" });
      assert.deepStrictEqual(yield* plansFor(["fails", "fails"], file), ["t3-only", "t3-only"]);
      const named = JSON.stringify({
        mcpProfile: "docs",
        mcpProfiles: { docs: { servers: ["docs"], t3Tools: "all" } },
      });
      assert.deepStrictEqual(yield* plansFor(["fails"], named), ["docs"]);
    }),
  );

  it.effect("a failed secret read follows a profile the managed policy locks", () =>
    Effect.gen(function* () {
      const policy = {
        schemaVersion: 1,
        organization: "Example Corp",
        settings: { mcpProfile: { value: "t3-only", locked: true } },
      };
      assert.deepStrictEqual(yield* plansFor(["fails"], "{}", policy), ["t3-only"]);
    }),
  );

  it.effect(
    "a failed read with a settings.json that does not decode refuses a narrower carried task (#653)",
    () =>
      Effect.gen(function* () {
        // The file may say mcpCarriedProfiles "refuse", so it is not read as the default apply.
        const task: LineageThread = {
          ...thread("task"),
          delegatedFrom: {
            environmentId: "environment-there" as never,
            threadId: ThreadId.make("thread-there"),
            title: "Parent there",
            mcpProfile: { name: "t3-only", servers: [], t3Tools: "all" },
          },
        };
        const broken = "{ not json";
        const runs = yield* plansFor(["fails", "fails"], broken, undefined, task);
        assert.strictEqual(runs.length, 2);
        for (const run of runs) assert.include(run, "set to refuse such tasks");
        // A file that decodes, or no file: the default apply cuts it.
        const cut = ["all cut to []"];
        assert.deepStrictEqual(yield* plansFor(["fails"], "{}", undefined, task), cut);
        assert.deepStrictEqual(yield* plansFor(["fails"], undefined, undefined, task), cut);
        // A task carrying all cuts nothing, so it is taken; a thread of this machine's own gets
        // the defaults, as before.
        const all: LineageThread = {
          ...task,
          delegatedFrom: {
            ...task.delegatedFrom!,
            mcpProfile: { name: "all", servers: "all", t3Tools: "all" },
          },
        };
        assert.deepStrictEqual(yield* plansFor(["fails"], broken, undefined, all), ["all"]);
        assert.deepStrictEqual(yield* plansFor(["fails"], broken), ["all"]);
      }),
  );

  it.effect(
    "a settings.json broken at load refuses a carried task though the read gave the defaults (#661)",
    () =>
      Effect.gen(function* () {
        // Upstream's load gives every setting its default (apply) for a file that does not decode;
        // the file is checked on each read, so it reads as refuse here too.
        for (const broken of ["{ not json", "", JSON.stringify({ providers: 5 })]) {
          const runs = yield* plansFor([{}, {}], broken, undefined, carriedTask);
          assert.strictEqual(runs.length, 2);
          for (const run of runs) assert.include(run, "set to refuse such tasks");
          // This machine's own threads still start, with what the read gave.
          assert.deepStrictEqual(yield* plansFor([{ mcpProfile: "t3-only" }], broken), ["t3-only"]);
        }
        // Fixed (or removed) again, the next read takes the task as before.
        assert.deepStrictEqual(yield* plansFor([{}], "{}", undefined, carriedTask), [
          "all cut to []",
        ]);
        assert.deepStrictEqual(yield* plansFor([{}], undefined, undefined, carriedTask), [
          "all cut to []",
        ]);
      }),
  );

  it.effect("legacy keys the load would refuse count as a file that does not decode (#661)", () =>
    Effect.gen(function* () {
      // The settings decode alone takes this file; the load refuses it for its legacy providers.
      const file = JSON.stringify({ providers: 5, mcpProfile: "t3-only" });
      const [run] = yield* plansFor(["fails"], file, undefined, carriedTask);
      assert.include(run, "set to refuse such tasks");
      // Its MCP profile keys still count for this machine's own threads: never the defaults' all
      //, with the read failing or giving the defaults as upstream's load does.
      assert.deepStrictEqual(yield* plansFor(["fails"], file), ["t3-only"]);
      assert.deepStrictEqual(yield* plansFor([{}, {}], file), ["t3-only", "t3-only"]);
    }),
  );

  it.effect("a policy lock stays over the fallback for a broken settings.json (#661)", () =>
    Effect.gen(function* () {
      const lockProfile = {
        schemaVersion: 1,
        organization: "Example Corp",
        settings: { mcpProfile: { value: "t3-only", locked: true } },
      };
      // A failed read and a broken file: still the locked t3-only, never the defaults' all.
      assert.deepStrictEqual(yield* plansFor(["fails", "fails"], "{ not json", lockProfile), [
        "t3-only",
        "t3-only",
      ]);
      // A lock of mcpCarriedProfiles wins over the refuse a broken file reads as: the owner
      // cannot set refuse there, so the carried task is cut, as the policy says.
      const lockApply = {
        schemaVersion: 1,
        organization: "Example Corp",
        settings: { mcpCarriedProfiles: { value: "apply", locked: true } },
      };
      for (const reads of [["fails"], [{}]] as const) {
        assert.deepStrictEqual(yield* plansFor(reads, "{ not json", lockApply, carriedTask), [
          "all cut to []",
        ]);
      }
      const lockRefuse = {
        schemaVersion: 1,
        organization: "Example Corp",
        settings: { mcpCarriedProfiles: { value: "refuse", locked: true } },
      };
      const [refusal] = yield* plansFor(["fails"], "{}", lockRefuse, carriedTask);
      assert.include(refusal, "set to refuse such tasks");
    }),
  );

  it.effect("refuses while settings.json cannot be read, whatever the settings read gave", () =>
    Effect.gen(function* () {
      // The read may hold a broken file's defaults from an earlier load, so it does not count
      // while the file cannot be read: never all on a guess.
      const runs = yield* plansFor([{}, "fails"], "unreadable");
      assert.strictEqual(runs.length, 2);
      for (const run of runs)
        assert.include(run, "could not read this thread's MCP profile settings");
    }),
  );

  it.effect(
    "a settings.json that does not decode keeps the MCP profile its text sets, never all (#681)",
    () =>
      Effect.gen(function* () {
        // Upstream's load refuses each of these whole and gives every setting its default.
        const files = [
          // A setting the decode refuses.
          { mcpProfile: "t3-only", defaultRuntimeMode: 5 },
          // Legacy keys the load refuses.
          { mcpProfile: "t3-only", providers: 5 },
        ];
        for (const settings of files) {
          const file = JSON.stringify(settings);
          assert.deepStrictEqual(yield* plansFor([{}, "fails", {}], file), [
            "t3-only",
            "t3-only",
            "t3-only",
          ]);
        }
        const decoded = ServerSettingsService.decodeSettingsFileText(
          JSON.stringify(files[0]),
          (persisted) => persisted,
        );
        assert.strictEqual(decoded._tag, "Failure");
        // A project's own profile and a thread's pick are read as well.
        const project = JSON.stringify({
          defaultRuntimeMode: 5,
          mcpProfiles: { docs: { servers: ["docs"], t3Tools: "all" } },
          projectSettingsOverrides: { project: { mcpProfile: "docs" } },
        });
        assert.deepStrictEqual(yield* plansFor([{}], project), ["docs"]);
        const picked = JSON.stringify({ providers: 5, mcpThreadProfiles: { t: "t3-only" } });
        assert.deepStrictEqual(yield* plansFor([{}], picked), ["t3-only"]);
        // A project entry it cannot read refuses the thread, never all.
        const [entry] = yield* plansFor(
          [{}],
          JSON.stringify({
            providers: 5,
            projectSettingsOverrides: { project: 5 },
            mcpProfile: "all",
          }),
        );
        assert.include(entry, "unreadable");
      }),
  );

  it.effect(
    "a settings.json that names a profile key but cannot be read refuses until it is fixed (#681)",
    () =>
      Effect.gen(function* () {
        const { settingsPath } = yield* ServerConfig.ServerConfig;
        const fs = yield* FileSystem.FileSystem;
        const service = yield* ServerSettingsService.ServerSettingsService;
        // As upstream's load reads the file: its settings, the defaults for one it cannot parse.
        const getSettings = fs.readFileString(settingsPath).pipe(
          Effect.map((raw) => {
            try {
              return fromFile(JSON.parse(raw));
            } catch {
              return fromFile({});
            }
          }),
          Effect.orElseSucceed(() => fromFile({})),
        );
        const plan = yield* makeClaudeMcpProfiles.pipe(
          Effect.provideService(ServerSettingsService.ServerSettingsService, {
            ...service,
            getSettings,
          }),
        );
        const run = () =>
          plan(thread("t"), {}).pipe(
            Effect.match({ onFailure: (refusal) => refusal.message, onSuccess: (r) => r.name }),
          );
        // Stopped half-way through a write, or a list: refused on each turn, never all.
        for (const broken of ['{ "mcpProfile": "t3-only", "theme": ', '["mcpProfile"]']) {
          yield* fs.writeFileString(settingsPath, broken);
          for (const _ of [1, 2]) {
            const refused = yield* run();
            assert.include(refused, "settings.json sets MCP profiles but cannot be read");
            assert.include(refused, "fix settings.json");
          }
        }
        // Fixed, the next turn reads it again; removed, the defaults apply as before.
        yield* fs.writeFileString(settingsPath, JSON.stringify({ mcpProfile: "t3-only" }));
        assert.strictEqual(yield* run(), "t3-only");
        yield* fs.remove(settingsPath);
        assert.strictEqual(yield* run(), "all");
      }).pipe(
        Effect.provide(
          Layer.mergeAll(
            ServerSettingsService.ServerSettingsService.layerTest(),
            ManagedPolicy.layerTest(),
            Layer.fresh(ServerConfig.layerTest(process.cwd(), { prefix: "t3code-mcp-profile-" })),
          ).pipe(Layer.provideMerge(NodeServices.layer)),
        ),
      ),
  );
});

describe("makeClaudeMcpProfiles without a thread store (#640)", () => {
  const plannedChild = (plan: Effect.Success<typeof makeClaudeMcpProfiles>, self: LineageThread) =>
    plan(self, {}).pipe(
      Effect.match({
        onFailure: (refusal) => refusal.message,
        onSuccess: (result) => result.servers,
      }),
    );

  it.effect("with settings but no store, refuses a thread with a parent, never giving it all", () =>
    Effect.gen(function* () {
      const plan = yield* makeClaudeMcpProfiles.pipe(
        Effect.provide(ServerSettingsService.ServerSettingsService.layerTest()),
      );
      const child = yield* plannedChild(plan, thread("child", "parent"));
      assert.include(String(child), "parent thread cannot be read");
      // Run again: the same refusal; a thread with no parent is not affected.
      assert.strictEqual(yield* plannedChild(plan, thread("child", "parent")), child);
      assert.strictEqual(yield* plannedChild(plan, thread("top")), "all");
    }),
  );

  it.effect("built alone (no store, no settings: the default settings), a child gets all", () =>
    Effect.gen(function* () {
      // Nothing here can narrow a thread: the defaults give all and no store holds a carried
      // profile. A profile carried on the thread itself still cuts it.
      const plan = yield* makeClaudeMcpProfiles;
      assert.strictEqual(yield* plannedChild(plan, thread("child", "parent")), "all");
    }),
  );
});

describe("checkClaudeSession", () => {
  it.effect("refuses and closes a session that does not list its servers in time", () =>
    Effect.gen(function* () {
      let closed = 0;
      const plan = { name: "t3-only", options: {}, servers: [] };
      const check = yield* checkClaudeSession(
        plan,
        {},
      )({
        mcpServerStatus: Effect.never,
        close: Effect.sync(() => void closed++),
      }).pipe(Effect.flip, Effect.forkChild);
      yield* TestClock.adjust(CHECK_SESSION_TIMEOUT);
      const refusal = yield* Fiber.join(check);
      assert.include(refusal.message, "could not check which MCP servers Claude loaded");
      assert.strictEqual(closed, 1);
    }),
  );
});

describe("claudeMcpProfileForThread", () => {
  const forThread = (file: Record<string, unknown>, self: LineageThread, findThread: FindThread) =>
    claudeMcpProfileForThread(fromFile(file), self, T3, findThread).pipe(
      Effect.match({ onFailure: (refusal) => refusal.message, onSuccess: (plan) => plan.servers }),
    );

  it.effect(
    "with no profile set, a child whose parent cannot be read is refused, not given all",
    () =>
      Effect.gen(function* () {
        // Any parent may be a task another machine delegated with a narrower profile, so a parent
        // that cannot be read refuses the child even with nothing set here.
        const gone = yield* forThread({}, thread("child", "gone"), store([]));
        assert.include(String(gone), "parent thread cannot be read");
        assert.strictEqual(yield* forThread({}, thread("top"), noReads), "all");
        // The owner's pick for that very thread is final without its parents.
        const picked = yield* forThread(
          { mcpThreadProfiles: { child: "all" } },
          thread("child", "gone"),
          store([]),
        );
        assert.strictEqual(picked, "all");
      }),
  );

  it.effect("an inheriting child is cut to its parent's pick, read from the store", () =>
    Effect.gen(function* () {
      const file = { mcpThreadProfiles: { parent: "t3-only" } };
      const child = yield* forThread(file, thread("child", "parent"), store([thread("parent")]));
      assert.deepStrictEqual(child, []);
    }),
  );

  it.effect("a parent that cannot be read refuses the child, unless the child has a pick", () =>
    Effect.gen(function* () {
      const orphan = yield* forThread(
        { mcpThreadProfiles: { other: "t3-only" } },
        thread("c", "x"),
        store([]),
      );
      assert.include(String(orphan), "parent thread cannot be read");
      const picked = yield* forThread(
        { mcpThreadProfiles: { c: "t3-only" } },
        thread("c", "x"),
        store([]),
      );
      assert.deepStrictEqual(picked, []);
    }),
  );

  it.effect("a looping lineage is refused, never widened to all", () =>
    Effect.gen(function* () {
      const file = { mcpThreadProfiles: { other: "all" } };
      const looping = yield* forThread(file, thread("a", "b"), store([thread("b", "a")]));
      assert.include(String(looping), "repeat a thread");
    }),
  );
});

describe("a task another machine delegated here (#532)", () => {
  const T3_ONLY: McpProfileCarried = { name: "t3-only", servers: [], t3Tools: "all" };
  /** A task launched here by a linked machine: no parent here, `delegatedFrom` names its thread there. */
  const remoteTask = (id: string, mcpProfile?: Record<string, unknown>): LineageThread => ({
    ...thread(id),
    delegatedFrom: {
      environmentId: "environment-there" as never,
      threadId: ThreadId.make("thread-there"),
      title: "Parent there",
      ...(mcpProfile === undefined ? {} : { mcpProfile }),
    },
  });
  const planned = (
    file: Record<string, unknown>,
    self: LineageThread,
    threads: ReadonlyArray<LineageThread> = [],
  ) =>
    Effect.gen(function* () {
      const warnings: Array<string> = [];
      const servers = yield* claudeMcpProfileForThread(
        fromFile(file),
        self,
        T3,
        store(threads),
      ).pipe(
        Effect.match({
          onFailure: (refusal) => refusal.message,
          onSuccess: (plan) => plan.servers,
        }),
        Effect.provide(
          Logger.layer(
            [
              Logger.make(({ message, logLevel }) => {
                if (logLevel === "Warn") warnings.push(String([message].flat()[0]));
              }),
            ],
            { mergeWithExisting: false },
          ),
        ),
      );
      return { servers, warnings };
    });

  it.effect("gets its remote parent's t3-only with nothing set here", () =>
    Effect.gen(function* () {
      const { servers, warnings } = yield* planned({}, remoteTask("task", T3_ONLY));
      assert.deepStrictEqual(servers, []);
      assert.deepStrictEqual(warnings, []);
    }),
  );

  it.effect("passes the carried profile on to its own sub-agents here", () =>
    Effect.gen(function* () {
      const task = remoteTask("task", T3_ONLY);
      const { servers } = yield* planned({}, thread("sub", "task"), [task]);
      assert.deepStrictEqual(servers, []);
    }),
  );

  it.effect("refuses a sub-agent whose task cannot be read here, never giving it all", () =>
    Effect.gen(function* () {
      // The task's row fails to read or decode (the store's read turns both into null), so its
      // carried t3-only is unknown: refused with nothing set here, with a sub-agent rule of all
      // and at any depth; only the owner's pick for the sub-agent itself stands.
      const sub = thread("sub", "task");
      assert.include(String((yield* planned({}, sub)).servers), "parent thread cannot be read");
      const rule = yield* planned({ mcpSubagentProfile: "all" }, sub);
      assert.include(String(rule.servers), "parent thread cannot be read");
      const deep = yield* planned({}, thread("deep", "sub"), [sub]);
      assert.include(String(deep.servers), "parent thread cannot be read");
      const picked = yield* planned({ mcpThreadProfiles: { sub: "t3-only" } }, sub);
      assert.deepStrictEqual(picked.servers, []);
    }),
  );

  it.effect("refuses a carried profile this version cannot read", () =>
    Effect.gen(function* () {
      const { servers } = yield* planned({}, remoteTask("task", { ...T3_ONLY, resources: [] }));
      assert.include(String(servers), "cannot be read by this version");
    }),
  );

  it.effect(
    "with nothing carried (an older sender, or a task stored before #532), gets this machine's profile and says so",
    () =>
      Effect.gen(function* () {
        const { servers, warnings } = yield* planned(
          { mcpSubagentProfile: "t3-only" },
          remoteTask("task"),
        );
        assert.deepStrictEqual(servers, []);
        assert.strictEqual(warnings.length, 1);
        assert.include(warnings[0], "sent no MCP profile");
        const all = yield* planned({}, remoteTask("task"));
        assert.strictEqual(all.servers, "all");
      }),
  );

  it.effect("plans a carried profile without the settings service too", () =>
    Effect.gen(function* () {
      const plan = yield* makeClaudeMcpProfiles;
      const result = yield* plan(remoteTask("task", T3_ONLY), {});
      assert.deepStrictEqual(result.servers, []);
    }),
  );

  it.effect(
    "on a machine set to refuse carried profiles, refuses the task and its sub-agents (#640)",
    () =>
      Effect.gen(function* () {
        const REFUSE = { mcpCarriedProfiles: "refuse" };
        const task = remoteTask("task", T3_ONLY);
        assert.include(String((yield* planned(REFUSE, task)).servers), "set to refuse such tasks");
        const sub = yield* planned(REFUSE, thread("sub", "task"), [task]);
        assert.include(String(sub.servers), "cannot be applied");
        // The owner's pick for the task stands; a task carrying all is taken.
        const picked = yield* planned({ ...REFUSE, mcpThreadProfiles: { task: "t3-only" } }, task);
        assert.deepStrictEqual(picked.servers, []);
        const all = remoteTask("task", { name: "all", servers: "all", t3Tools: "all" });
        assert.strictEqual((yield* planned(REFUSE, all)).servers, "all");
      }),
  );
});
