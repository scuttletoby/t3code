// @effect-diagnostics nodeBuiltinImport:off - reads the upstream source to count its fork lines.
/**
 * Fork: the delegation checks' seam. The upstream `OrchestratorMcpService.ts` keeps only the seat
 * calls; this file fails if a fork check is inlined there again. What the checks do is tested in
 * `OrchestratorMcpService.usageGate.test.ts` and `OrchestratorMcpService.slotGate.test.ts`; the
 * inherited agent option here and in `OrchestratorMcpService.test.ts`.
 */
import * as NodeFS from "node:fs";

import { assert, describe, it } from "@effect/vitest";
import {
  ProjectId,
  ProviderInstanceId,
  type OrchestrationV2ThreadProjection,
  type ServerProvider,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as OrchestratorMcpService from "../mcp/OrchestratorMcpService.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as DelegateChecks from "./delegateChecks.ts";

const UPSTREAM_FILE = new URL("../mcp/OrchestratorMcpService.ts", import.meta.url);

/** The body of one service method: from its `Effect.fn` name to the next method's. */
const methodBody = (source: string, method: string) => {
  const start = source.indexOf(`${method}: Effect.fn("OrchestratorMcpService.${method}")`);
  assert.isAtLeast(start, 0, `method ${method} not found in OrchestratorMcpService.ts`);
  const next = source.indexOf(': Effect.fn("OrchestratorMcpService.', start + method.length + 20);
  return source.slice(start, next === -1 ? undefined : next);
};

const count = (text: string, needle: string) => text.split(needle).length - 1;

describe("fork delegation checks: upstream seat calls", () => {
  const source = NodeFS.readFileSync(UPSTREAM_FILE, "utf8");

  it("the upstream file builds the checks once, from the fork module", () => {
    assert.strictEqual(count(source, 'from "../fork/delegateChecks.ts"'), 1);
    assert.strictEqual(count(source, "DelegateChecks.make("), 1);
  });

  it("delegate_task calls seat 1 and seat 2 once each, and create_threads its seat once", () => {
    const delegateTask = methodBody(source, "delegateTask");
    const createThreads = methodBody(source, "createThreads");
    const seats = {
      beforeWorkspace: [delegateTask, 1],
      afterWorkspace: [delegateTask, 1],
      beforeCreateThread: [createThreads, 1],
    } as const;
    for (const [seat, [body, expected]] of Object.entries(seats)) {
      assert.strictEqual(count(body, `delegateChecks.${seat}(`), expected, `seat ${seat}`);
      assert.strictEqual(count(source, `delegateChecks.${seat}(`), expected, `seat ${seat} (file)`);
    }
    // Every use of the built checks is one of the seats.
    const uses = source.match(/\bdelegateChecks\.(?!ts\b)\w+/g) ?? [];
    assert.deepStrictEqual(
      uses.toSorted(),
      Object.keys(seats)
        .map((seat) => `delegateChecks.${seat}`)
        .toSorted(),
    );
  });

  it("no fork check is inlined: the gates are reached only through the fork module", () => {
    for (const inlined of [
      "../slots/",
      "../usage/UsagePacer",
      "../usage/UsagePacingGate",
      "SlotGate",
      "slotGate",
      "UsagePacer",
      "usagePacer",
      "UsagePacingGate",
      "usagePacingGate",
      "usagePacingLowerEffort",
      "agentSlots",
    ]) {
      assert.strictEqual(count(source, inlined), 0, `"${inlined}" in OrchestratorMcpService.ts`);
    }
  });

  it("the fork lines in the upstream file stay pinned", () => {
    // A new fork line here goes through fork/delegateChecks.ts. If one truly must sit in the
    // upstream file, raise this number and say why in the PR.
    assert.strictEqual(count(source, "// Fork:"), 6);
  });
});

const callerThreadId = ThreadId.make("thread:seam-parent");
const parent = {
  thread: {
    projectId: ProjectId.make("project:seam"),
    runtimeMode: "full-access",
  },
} as unknown as Pick<OrchestrationV2ThreadProjection, "thread">;
const target: DelegateChecks.DelegateTarget = {
  modelSelection: { instanceId: ProviderInstanceId.make("codex"), model: "gpt-5.4" },
  supportedRuntimeModes: undefined,
};

/** Every service the checks may read fails the test if it is read. */
const untouched = Layer.mergeAll(
  Layer.mock(ServerSettings.ServerSettingsService)({
    getSettings: Effect.die("settings read"),
  }),
  Layer.mock(ThreadManagementService.ThreadManagementService)({}),
  Layer.mock(ProviderRegistry.ProviderRegistry)({ getProviders: Effect.die("providers read") }),
  Layer.mock(SlotGate.SlotGate)({
    acquire: () => Effect.die("slot acquired"),
    started: () => Effect.die("slot started"),
    release: () => Effect.die("slot released"),
  }),
);
const host: DelegateChecks.DelegateChecksHost = {
  resolveTarget: () => Effect.die("target resolved"),
  scanWorkspace: () => Effect.die("folder scanned"),
};

describe("fork delegation checks: runs again", () => {
  it.effect("a retry of an accepted delegation reads, scans and holds nothing", () =>
    Effect.gen(function* () {
      const checks = yield* DelegateChecks.make(host);
      const before = yield* checks.beforeWorkspace({
        isRetry: true,
        parent,
        requested: undefined,
        runtimeMode: undefined,
        cwd: "/work/project",
        target,
      });
      assert.deepStrictEqual(before, { target });
      const hold = yield* checks.afterWorkspace({ isRetry: true, callerThreadId, target });
      yield* hold.started(ThreadId.make("thread:seam-child"));
      yield* hold.release;
    }).pipe(Effect.scoped, Effect.provide(untouched)),
  );

  it.effect("create_threads' seat on a retry reads nothing and keeps the target", () =>
    Effect.gen(function* () {
      const checks = yield* DelegateChecks.make(host);
      for (const isRetry of [false, true]) {
        // A target without an agent option reads nothing, retry or not.
        const result = yield* checks.beforeCreateThread({
          isRetry,
          callerThreadId,
          parent,
          requested: { model: "gpt-5.4" },
          target,
        });
        assert.deepStrictEqual(result, { target });
      }
      // A retry keeps even an inherited agent option: it starts no work (the receipt holds the
      // target it was first dispatched with).
      const withAgent = agentTarget([{ id: "agent", value: "build" }]);
      const retry = yield* checks.beforeCreateThread({
        isRetry: true,
        callerThreadId,
        parent,
        requested: { model: "big-pickle" },
        target: withAgent,
      });
      assert.deepStrictEqual(retry, { target: withAgent });
      const before = yield* checks.beforeWorkspace({
        isRetry: true,
        parent,
        requested: { model: "big-pickle" },
        runtimeMode: undefined,
        cwd: undefined,
        target: withAgent,
      });
      assert.deepStrictEqual(before, { target: withAgent });
    }).pipe(Effect.provide(untouched)),
  );
});

const opencodeId = ProviderInstanceId.make("opencode-work");
const kiroId = ProviderInstanceId.make("kiro-work");
const agentTarget = (
  options: ReadonlyArray<{ readonly id: string; readonly value: string | boolean }>,
  instanceId = opencodeId,
): DelegateChecks.DelegateTarget => ({
  modelSelection: { instanceId, model: "big-pickle", options },
  supportedRuntimeModes: undefined,
});
/** Pacing off, and a provider list with each driver: the drop runs with the gate off as well. */
const providersWith = Layer.mergeAll(
  Layer.mock(ServerSettings.ServerSettingsService)({
    getSettings: Effect.succeed({ usagePacingGate: "off" } as never),
  }),
  Layer.mock(ThreadManagementService.ThreadManagementService)({}),
  Layer.mock(ProviderRegistry.ProviderRegistry)({
    getProviders: Effect.succeed([
      { instanceId: opencodeId, driver: "opencode", models: [] },
      { instanceId: kiroId, driver: "kiro", models: [] },
      { instanceId: ProviderInstanceId.make("codex"), driver: "codex", models: [] },
    ] as unknown as ReadonlyArray<ServerProvider>),
  }),
);

describe("fork delegation checks: an inherited agent option (#643)", () => {
  const effort = { id: "variant", value: "high" } as const;
  const agent = { id: "agent", value: "build" } as const;

  it("a call that names its model drops the agent option it did not name", () => {
    const cases = [
      {
        name: "opencode, model named, agent inherited",
        driver: "opencode",
        requested: { model: "big-pickle" },
        options: [effort, agent],
        kept: [effort],
      },
      {
        name: "opencode, model and options named, agent inherited",
        driver: "opencode",
        requested: { model: "big-pickle", options: [effort] },
        options: [effort, agent],
        kept: [effort],
      },
      {
        name: "kiro, model named, mode inherited",
        driver: "kiro",
        requested: { model: "big-pickle" },
        options: [{ id: "mode", value: "planner" }],
        kept: [],
      },
      {
        name: "an instance not in the list drops every agent id",
        driver: undefined,
        requested: { model: "big-pickle" },
        options: [agent, { id: "mode", value: "planner" }, effort],
        kept: [effort],
      },
      {
        name: "the call names the agent itself",
        driver: "opencode",
        requested: { model: "big-pickle", options: [agent] },
        options: [effort, agent],
        kept: [effort, agent],
      },
      {
        name: "no model named: the parent's options stay, as upstream",
        driver: "opencode",
        requested: undefined,
        options: [effort, agent],
        kept: [effort, agent],
      },
      {
        name: "no model named, options named: upstream merge stays",
        driver: "opencode",
        requested: { options: [effort] },
        options: [effort, agent],
        kept: [effort, agent],
      },
      {
        name: "another driver's id is no agent option",
        driver: "codex",
        requested: { model: "big-pickle" },
        options: [agent, { id: "mode", value: "x" }],
        kept: [agent, { id: "mode", value: "x" }],
      },
      {
        name: "opencode keeps an option named mode",
        driver: "opencode",
        requested: { model: "big-pickle" },
        options: [{ id: "mode", value: "x" }],
        kept: [{ id: "mode", value: "x" }],
      },
    ] as const;
    for (const testCase of cases) {
      const given = agentTarget(testCase.options);
      const result = DelegateChecks.withoutInheritedAgent(
        given,
        testCase.requested,
        testCase.driver,
      );
      assert.deepStrictEqual(result.modelSelection.options, testCase.kept, testCase.name);
      assert.deepStrictEqual(result.modelSelection.model, given.modelSelection.model);
      // Runs again: the result of a drop is a fixed point.
      assert.deepStrictEqual(
        DelegateChecks.withoutInheritedAgent(result, testCase.requested, testCase.driver),
        result,
        `${testCase.name} (again)`,
      );
    }
    // No options at all (a child on another model): nothing to drop, same object back.
    assert.strictEqual(
      DelegateChecks.withoutInheritedAgent(target, { model: "gpt-5.4" }, "opencode"),
      target,
    );
  });

  it.effect("both seats drop it on the final target, by the target instance's driver", () =>
    Effect.gen(function* () {
      const checks = yield* DelegateChecks.make(host);
      for (const [instanceId, option] of [
        [opencodeId, agent],
        [kiroId, { id: "mode", value: "planner" }],
      ] as const) {
        const given = agentTarget([effort, option], instanceId);
        const expected = agentTarget([effort], instanceId);
        const created = yield* checks.beforeCreateThread({
          isRetry: false,
          callerThreadId,
          parent,
          requested: { model: "big-pickle" },
          target: given,
        });
        assert.deepStrictEqual(created, { target: expected }, `create_threads ${instanceId}`);
        const before = yield* checks.beforeWorkspace({
          isRetry: false,
          parent,
          requested: { model: "big-pickle" },
          runtimeMode: undefined,
          cwd: undefined,
          target: given,
        });
        assert.deepStrictEqual(before, { target: expected }, `delegate_task ${instanceId}`);
      }
    }).pipe(Effect.provide(providersWith)),
  );
});

describe("fork delegation checks: the copied folder model merge", () => {
  it("gives the same lists as the target lookup's withWorkspaceModels (#596)", () => {
    const model = (slug: string) => ({ slug, name: slug, isCustom: false });
    const snapshot = (cwd: string, models?: ReadonlyArray<ReturnType<typeof model>>) => ({
      cwd,
      checkedAt: "2026-11-13T12:00:00.000Z",
      slashCommands: [],
      skills: [],
      ...(models === undefined ? {} : { models }),
    });
    const providers = [
      // No snapshots, a snapshot of another folder, one without models, one that adds a model,
      // and an empty model list.
      { instanceId: "none", models: [model("a")] },
      { instanceId: "other", models: [model("a")], workspaceSnapshots: [snapshot("/elsewhere")] },
      { instanceId: "plain", models: [model("a")], workspaceSnapshots: [snapshot("/work")] },
      {
        instanceId: "adds",
        models: [model("a")],
        workspaceSnapshots: [
          snapshot("/elsewhere", []),
          snapshot("/work", [model("a"), model("b")]),
        ],
      },
      { instanceId: "empty", models: [model("a")], workspaceSnapshots: [snapshot("/work", [])] },
      // Pending snapshots: a pending filter on one side only fails the check.
      ...(["modelsPending", "slashCommandsPending"] as const).map((flag) => ({
        instanceId: flag,
        models: [model("a")],
        workspaceSnapshots: [{ ...snapshot("/work", [model("a"), model("c")]), [flag]: true }],
      })),
    ] as unknown as ReadonlyArray<ServerProvider>;
    for (const cwd of [undefined, "/work", "/elsewhere", "/missing"]) {
      assert.deepStrictEqual(
        DelegateChecks.withFolderModels(providers, cwd),
        OrchestratorMcpService.withWorkspaceModels(providers, cwd),
        `cwd ${cwd}`,
      );
    }
    assert.deepStrictEqual(
      DelegateChecks.withFolderModels(providers, "/work")[3]?.models.map((entry) => entry.slug),
      ["a", "b"],
    );
  });
});
