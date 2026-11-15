// @effect-diagnostics nodeBuiltinImport:off - reads the upstream source to count its fork lines.
/**
 * Fork: the delegation checks' seam. The upstream `OrchestratorMcpService.ts` keeps only the seat
 * calls; this file fails if a fork check is inlined there again. What the checks do is tested in
 * `OrchestratorMcpService.usageGate.test.ts` and `OrchestratorMcpService.slotGate.test.ts`.
 */
import * as NodeFS from "node:fs";

import { assert, describe, it } from "@effect/vitest";
import {
  ProjectId,
  ProviderInstanceId,
  type OrchestrationV2ThreadProjection,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
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
  Layer.mock(SlotGate.SlotGate)({
    acquire: () => Effect.die("slot acquired"),
    started: () => Effect.die("slot started"),
    release: () => Effect.die("slot released"),
  }),
);
const host: DelegateChecks.DelegateChecksHost = {
  resolveTarget: () => Effect.die("target resolved"),
};

describe("fork delegation checks: runs again", () => {
  it.effect("a retry of an accepted delegation reads no setting and takes no slot", () =>
    Effect.gen(function* () {
      const checks = yield* DelegateChecks.make(host);
      const before = yield* checks.beforeWorkspace({
        isRetry: true,
        parent,
        requested: undefined,
        runtimeMode: undefined,
        providers: [],
        target,
      });
      assert.deepStrictEqual(before, { target });
      const hold = yield* checks.afterWorkspace({ isRetry: true, callerThreadId, target });
      yield* hold.started(ThreadId.make("thread:seam-child"));
      yield* hold.release;
    }).pipe(Effect.scoped, Effect.provide(untouched)),
  );

  it.effect("create_threads' seat checks nothing yet and keeps the target, retry or not", () =>
    Effect.gen(function* () {
      const checks = yield* DelegateChecks.make(host);
      for (const isRetry of [false, true]) {
        const result = yield* checks.beforeCreateThread({
          isRetry,
          callerThreadId,
          parent,
          target,
        });
        assert.deepStrictEqual(result, { target });
      }
    }).pipe(Effect.provide(untouched)),
  );
});
