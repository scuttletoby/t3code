// @effect-diagnostics nodeBuiltinImport:off - reads the upstream source to count its fork lines.
/**
 * Fork: the delegation checks' seam. The upstream `OrchestratorMcpService.ts` keeps only the seat
 * calls; this file fails if a fork check is inlined there again. The checks themselves are tested
 * here, in the usage and slot gate tests, and on every launch path in `delegateOwnOptions.test.ts`.
 */
import * as NodeFS from "node:fs";

import { assert, describe, it } from "@effect/vitest";
import {
  DEFAULT_SERVER_SETTINGS,
  type OrchestratorMcpTarget,
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

/** The remote task read, a `const` beside the methods: to the next one. */
const remoteReadBody = (source: string) => {
  const start = source.indexOf(
    'const readRemoteTask = Effect.fn("OrchestratorMcpService.readRemoteTask")',
  );
  assert.isAtLeast(start, 0, "readRemoteTask not found in OrchestratorMcpService.ts");
  return source.slice(start, source.indexOf("Effect.fn(", start + 40));
};

describe("fork delegation checks: upstream seat calls", () => {
  const source = NodeFS.readFileSync(UPSTREAM_FILE, "utf8");

  it("the upstream file builds the checks once, from the fork module", () => {
    assert.strictEqual(count(source, 'from "../fork/delegateChecks.ts"'), 1);
    assert.strictEqual(count(source, "DelegateChecks.make("), 1);
  });

  it("delegate_task calls seat 1 and seat 2 once each, create_threads its seat, the remote read its own", () => {
    const delegateTask = methodBody(source, "delegateTask");
    const createThreads = methodBody(source, "createThreads");
    const seats = {
      beforeWorkspace: [delegateTask, 1],
      afterWorkspace: [delegateTask, 1],
      beforeCreateThread: [createThreads, 1],
      // The remote path has no seat: it takes its target's own options through the same rule.
      remoteTarget: [delegateTask, 1],
      // A remote task's status reads its prompts there, over upstream's fixed false/null.
      remotePrompts: [remoteReadBody(source), 1],
    } as const;
    for (const [seat, [body, expected]] of Object.entries(seats)) {
      assert.strictEqual(count(body, `delegateChecks.${seat}(`), expected, `seat ${seat}`);
      assert.strictEqual(count(source, `delegateChecks.${seat}(`), expected, `seat ${seat} (file)`);
    }
    // Only through the checks, which read the model policy.
    assert.strictEqual(count(source, "DelegateChecks.remoteTarget("), 0, "remote target (bare)");
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
    // upstream file, raise this number and say why in the PR. 8: upstream's endsTaskWait is a
    // module function, so its seat (a secret ends a wait) is one call there on the existing import.
    assert.strictEqual(count(source, "// Fork:"), 8);
  });
});

const callerThreadId = ThreadId.make("thread:seam-parent");
/**
 * A new thread without a prompt, as create_threads passes it: it starts no run, so it takes no
 * agent slot (`OrchestratorMcpService.slotGate.test.ts` checks one that does).
 */
const child = { threadId: ThreadId.make("thread:seam-child"), index: 0, batch: [{}] };
const parent = {
  thread: {
    id: callerThreadId,
    projectId: ProjectId.make("project:seam"),
    runtimeMode: "full-access",
    modelSelection: { instanceId: ProviderInstanceId.make("codex"), model: "gpt-5.4" },
    lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: callerThreadId },
  },
} as unknown as Pick<OrchestrationV2ThreadProjection, "thread">;
const target: DelegateChecks.DelegateTarget = {
  modelSelection: { instanceId: ProviderInstanceId.make("codex"), model: "gpt-5.4" },
  supportedRuntimeModes: undefined,
};

/** Every service the checks may read but settings fails the test if it is read. */
const untouchedButSettings = Layer.mergeAll(
  Layer.mock(ThreadManagementService.ThreadManagementService)({}),
  Layer.mock(ProviderRegistry.ProviderRegistry)({ getProviders: Effect.die("providers read") }),
  Layer.mock(SlotGate.SlotGate)({
    acquire: () => Effect.die("slot acquired"),
    started: () => Effect.die("slot started"),
    release: () => Effect.die("slot released"),
  }),
);
/** Every service the checks may read fails the test if it is read. */
const untouched = Layer.merge(
  untouchedButSettings,
  Layer.mock(ServerSettings.ServerSettingsService)({ getSettings: Effect.die("settings read") }),
);
/**
 * As {@link untouched}, but settings read the defaults (every thread on MCP profile `all`), and
 * each read counts: a new `create_threads` thread reads them for its MCP profile check.
 */
const settingsCounted = (reads: { count: number }) =>
  Layer.merge(
    untouchedButSettings,
    Layer.mock(ServerSettings.ServerSettingsService)({
      getSettings: Effect.sync(() => {
        reads.count += 1;
        return DEFAULT_SERVER_SETTINGS;
      }),
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

  const reads = { count: 0 };
  it.effect("create_threads' seat on a retry reads nothing and keeps the target", () =>
    Effect.gen(function* () {
      const checks = yield* DelegateChecks.make(host);
      // A target without options reads nothing on a retry; a new thread reads the settings for
      // its MCP profile (launchMcpProfile.ts) and for the model policy (off: nothing more).
      const fresh = yield* checks.beforeCreateThread({
        isRetry: false,
        child,
        parent,
        requested: { model: "gpt-5.4" },
        target,
      });
      assert.deepStrictEqual(fresh, { target });
      assert.strictEqual(reads.count, 2);
      const result = yield* checks.beforeCreateThread({
        isRetry: true,
        child,
        parent,
        requested: { model: "gpt-5.4" },
        target,
      });
      assert.deepStrictEqual(result, { target });
      // A retry keeps even an option the call did not name: it starts no work (the receipt holds
      // the target it was first dispatched with).
      const withAgent = agentTarget([{ id: "agent", value: "build" }]);
      const retry = yield* checks.beforeCreateThread({
        isRetry: true,
        child,
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
      assert.strictEqual(reads.count, 2, "the retries read nothing");
    }).pipe(Effect.provide(settingsCounted(reads))),
  );
});

const agentTarget = (
  options: ReadonlyArray<{ readonly id: string; readonly value: string | boolean }>,
): DelegateChecks.DelegateTarget => ({
  modelSelection: { instanceId: ProviderInstanceId.make("opencode"), model: "big-pickle", options },
  supportedRuntimeModes: undefined,
});
/**
 * Pacing off, a model policy in `mode` (a failed read for `unreadable`) whose one row allows the
 * pick: `big-pickle` at `high` (no provider is listed, so the pick has no driver and every
 * driver's effort id counts, `variant` too).
 */
const pacingOff = (mode: "off" | "advise" | "enforce" | "unreadable") =>
  Layer.mergeAll(
    Layer.mock(ServerSettings.ServerSettingsService)({
      getSettings:
        mode === "unreadable"
          ? Effect.fail(new Error("settings unreadable") as never)
          : Effect.succeed({
              ...DEFAULT_SERVER_SETTINGS,
              usagePacingGate: "off",
              modelPolicy: {
                mode,
                allow: [{ driver: "opencode", model: "big-pickle", efforts: ["high"] }],
              },
            } as never),
    }),
    Layer.mock(ThreadManagementService.ThreadManagementService)({}),
    Layer.mock(ProviderRegistry.ProviderRegistry)({
      getProviders: Effect.succeed([] as ReadonlyArray<ServerProvider>),
    }),
  );

type Target = OrchestratorMcpTarget;

describe("fork delegation checks: a child's own options", () => {
  const effort = { id: "variant", value: "high" } as const;
  const agent = { id: "agent", value: "build" } as const;
  const mode = { id: "_t3/session-mode", value: "code" } as const;

  it("a call that names its model keeps only the options it names", () => {
    const cases = [
      {
        name: "model named: no option of the parent's, of any kind",
        requested: { model: "big-pickle" },
        options: [effort, agent, mode, { id: "serviceTier", value: "fast" }],
        kept: [],
      },
      {
        name: "model and an option named",
        requested: { model: "big-pickle", options: [agent] },
        options: [effort, agent, mode],
        kept: [agent],
      },
      {
        name: "an old effort ultracode keeps the switch it stands for",
        requested: { model: "big-pickle", options: [{ id: "effort", value: "ultracode" }] },
        options: [
          { id: "effort", value: "xhigh" },
          { id: "ultracode", value: true },
          { id: "fastMode", value: true },
        ],
        kept: [
          { id: "effort", value: "xhigh" },
          { id: "ultracode", value: true },
        ],
      },
      {
        name: "no model named: the parent's options stay, as upstream",
        requested: undefined,
        options: [effort, agent],
        kept: [effort, agent],
      },
      {
        name: "no model named, options named: upstream merge stays",
        requested: { options: [effort] },
        options: [effort, agent],
        kept: [effort, agent],
      },
    ] as const;
    for (const testCase of cases) {
      const given = agentTarget(testCase.options);
      const result = DelegateChecks.withOwnOptions(given, testCase.requested);
      assert.deepStrictEqual(result.modelSelection.options, testCase.kept, testCase.name);
      assert.deepStrictEqual(
        DelegateChecks.ownOptions(testCase.options, testCase.requested),
        testCase.kept,
        `${testCase.name} (options)`,
      );
      assert.deepStrictEqual(result.modelSelection.model, given.modelSelection.model);
      // Runs again: the result is a fixed point.
      assert.deepStrictEqual(
        DelegateChecks.withOwnOptions(result, testCase.requested),
        result,
        `${testCase.name} (again)`,
      );
    }
    // No options at all (a child on another model, an old thread): same object back.
    assert.strictEqual(DelegateChecks.withOwnOptions(target, { model: "gpt-5.4" }), target);
    assert.strictEqual(DelegateChecks.ownOptions(undefined, { model: "gpt-5.4" }), undefined);
  });

  it("a remote target that names its model sends exactly its own options", () => {
    const box = "environment:box" as never;
    assert.deepStrictEqual(
      DelegateChecks.remoteTarget<Target>({ environmentId: box, model: "m" }),
      {
        environmentId: box,
        model: "m",
        options: [],
      },
    );
    const named = { environmentId: box, model: "m", options: [agent] };
    assert.strictEqual(DelegateChecks.remoteTarget(named), named);
    // Runs again: the same target.
    const bare = DelegateChecks.remoteTarget<Target>({ environmentId: box, model: "m" });
    assert.strictEqual(DelegateChecks.remoteTarget(bare), bare);
    // No model named: as it came (the other side inherits, as upstream does).
    const unnamed = { environmentId: box, options: [effort] };
    assert.strictEqual(DelegateChecks.remoteTarget(unnamed), unnamed);
  });

  const requested = { model: "big-pickle", options: [effort] };
  const given = agentTarget([effort, agent]);
  /** Both local seats and the remote target, under one settings layer. */
  const seats = Effect.gen(function* () {
    const checks = yield* DelegateChecks.make(host);
    // A refusal shows as its code.
    const created = yield* checks
      .beforeCreateThread({ isRetry: false, child, parent, requested, target: given })
      .pipe(Effect.match({ onFailure: (refusal) => refusal.code, onSuccess: (r) => r.target }));
    const before = yield* checks
      .beforeWorkspace({
        isRetry: false,
        parent,
        requested,
        runtimeMode: undefined,
        cwd: undefined,
        target: given,
      })
      .pipe(Effect.match({ onFailure: (refusal) => refusal.code, onSuccess: (r) => r.target }));
    const remote = yield* checks
      .remoteTarget<Target>({ model: "big-pickle", options: [effort] })
      .pipe(Effect.match({ onFailure: (refusal) => refusal.code, onSuccess: (r) => r }));
    return { created, before, remote };
  });

  it.effect.each(["advise", "enforce"] as const)(
    "with the model policy %s, every path applies it to the final target",
    (mode) =>
      Effect.gen(function* () {
        assert.deepStrictEqual(yield* seats, {
          created: agentTarget([effort]),
          // No role: the slot gate counts the task as a writer.
          before: { ...agentTarget([effort]), agentKind: "agent-writer" },
          remote: { model: "big-pickle", options: [effort] },
        });
      }).pipe(Effect.provide(pacingOff(mode))),
  );

  // Settings that cannot be read: the caller's MCP profile is unknown (refused, never a thread on
  // `all`, launchMcpProfile.ts), and the model policy cannot be checked (refused, fail closed,
  // modelPolicyCheck.ts). Nothing starts on any path.
  it.effect("with settings that cannot be read, every path refuses", () =>
    Effect.gen(function* () {
      assert.deepStrictEqual(yield* seats, {
        created: "invalid_request",
        before: "model_unavailable",
        remote: "model_unavailable",
      });
    }).pipe(Effect.provide(pacingOff("unreadable"))),
  );

  it.effect(
    "the task's role reaches the slot gate as its kind; no role or target is a writer",
    () => {
      const asked: Array<string | undefined> = [];
      const gate = Layer.mock(SlotGate.SlotGate)({
        acquire: (input) =>
          Effect.sync(() => {
            asked.push(input.kind);
            return { type: "allow" as const, token: null };
          }),
        release: () => Effect.void,
      });
      return Effect.gen(function* () {
        const checks = yield* DelegateChecks.make(host);
        // The role is the caller's word (it may lie): the gate checks what the agent then uses.
        for (const requested of [
          { model: "big-pickle", options: [effort], role: "review" as const },
          { role: "research" as const },
          { model: "big-pickle", options: [effort], role: "implementation" as const },
          { role: undefined },
          undefined,
        ]) {
          const before = yield* checks.beforeWorkspace({
            isRetry: false,
            parent,
            requested,
            runtimeMode: undefined,
            cwd: undefined,
            target: given,
          });
          const hold = yield* checks.afterWorkspace({
            isRetry: false,
            callerThreadId,
            target: before.target,
          });
          yield* hold.release;
        }
        // Literal names: the capacity rule's kinds (capacity-rule.json).
        const [light, writer] = ["agent-light", "agent-writer"];
        assert.deepStrictEqual(asked, [light, light, writer, writer, writer]);
      }).pipe(Effect.scoped, Effect.provide(Layer.merge(pacingOff("off"), gate)));
    },
  );

  it.effect("with the model policy off, every path keeps upstream's inheritance", () =>
    Effect.gen(function* () {
      assert.deepStrictEqual(yield* seats, {
        created: given,
        before: { ...given, agentKind: "agent-writer" },
        remote: { model: "big-pickle", options: [effort] },
      });
    }).pipe(Effect.provide(pacingOff("off"))),
  );

  const reads = { count: 0 };
  it.effect("a call with nothing to drop reads settings only for its checks, on any path", () =>
    Effect.gen(function* () {
      const checks = yield* DelegateChecks.make(host);
      const own = agentTarget([effort]);
      // create_threads reads settings twice per thread: its MCP profile check and the model policy
      // (off here, so no provider is read).
      const cases = [requested, undefined, { options: [effort] }];
      for (const req of cases) {
        const created = yield* checks.beforeCreateThread({
          isRetry: false,
          child,
          parent,
          requested: req,
          target: own,
        });
        assert.strictEqual(created.target, own);
      }
      assert.strictEqual(reads.count, 2 * cases.length);
      const named = { model: "m", options: [agent] };
      assert.strictEqual(yield* checks.remoteTarget(named), named);
      const unnamed = { options: [agent] };
      assert.strictEqual(yield* checks.remoteTarget(unnamed), unnamed);
      assert.strictEqual(
        reads.count,
        2 * cases.length + 2,
        "a remote target reads settings once, for the model policy alone",
      );
    }).pipe(Effect.provide(settingsCounted(reads))),
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
