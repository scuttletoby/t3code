/**
 * Fork (placement row P6, `advise`): the advice text for a `delegate_task` child. Each test answers
 * one question: is anything read while placement is off, which machines are asked, what a machine
 * that never answers does, and that the advice never fails the task. The seat that adds it to the
 * reply is tested in `delegateChecks.test.ts`.
 */
import { assert, describe, it } from "@effect/vitest";
import {
  DEFAULT_PLACEMENT_SETTINGS,
  DEFAULT_SERVER_SETTINGS,
  EnvironmentId,
  ProjectId,
  type PlacementMode,
  type ServerSettings as Settings,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as TestClock from "effect/testing/TestClock";

import type { PlacementResult } from "../../../../packages/shared/src/forkPlacement.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as RepositoryIdentityResolver from "../project/RepositoryIdentityResolver.ts";
import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as PlacementAdvice from "./placementAdvice.ts";

const NOW = 1_800_000_000_000;
const GB = 1024 ** 3;
const hereId = EnvironmentId.make("environment:here");
const boxId = EnvironmentId.make("environment:box");
const KEY = "github.com/example/widgets";

const settingsIn = (mode: PlacementMode): Settings => ({
  ...DEFAULT_SERVER_SETTINGS,
  placement: { ...DEFAULT_PLACEMENT_SETTINGS, mode },
});

const input = (mode: PlacementMode): PlacementAdvice.AdviceInput => ({
  settings: settingsIn(mode),
  projectId: ProjectId.make("project:widgets"),
  runtimeMode: "full-access",
  driverKind: "codex" as PlacementAdvice.AdviceInput["driverKind"],
  model: "gpt-5.4",
});

const scope: McpInvocationContext.McpInvocationScope = {
  environmentId: hereId,
  requestNamespace: "placement-advice-test",
  thread: undefined,
  client: undefined,
  capabilities: new Set(),
  issuedAt: 1,
  linkedEnvironments: true,
};

const hostFacts = (cpuUtilization: number) => ({
  sampledAt: NOW,
  cpuUtilization,
  cpuCount: 8,
  availableMemoryBytes: 8 * GB,
  totalMemoryBytes: 16 * GB,
});

/** What the linked machine answers to each forwarded tool; `never`: it does not answer. */
interface There {
  readonly facts?: unknown;
  readonly projects?: ReadonlyArray<string>;
  readonly driverKind?: string;
}

const forwardingTo = (there: There | "never", calls: Array<string>) =>
  Layer.mock(PeerForwarding.PeerForwarding)({
    call: ((_scope: unknown, tool: { readonly name: string }) => {
      calls.push(tool.name);
      if (there === "never") return Effect.never;
      switch (tool.name) {
        case "t3_environment_read":
          return Effect.succeed(there.facts);
        case "t3_project_list":
          return Effect.succeed({
            projects: (there.projects ?? [KEY]).map((canonicalKey) => ({
              repositoryIdentity: { canonicalKey },
            })),
            nextCursor: null,
          });
        case "orchestrator_capabilities":
          return Effect.succeed({
            providers: [
              {
                driverKind: there.driverKind ?? "codex",
                canRunChildTask: true,
                models: [{ id: "GPT-5.4" }],
              },
            ],
          });
        default:
          return Effect.die(`unexpected tool ${tool.name}`);
      }
    }) as never,
  });

const linksTo = (enabled = true) =>
  Layer.mock(PeerLinks.PeerLinks)({
    enabled: Effect.succeed(enabled),
    names: Effect.succeed([{ environmentId: boxId, label: "Build box" }]),
    get: () =>
      Effect.succeedSome({
        environmentId: boxId,
        label: "Build box",
        urls: ["https://box.example.test"],
        access: "full-access",
        linkedAt: DateTime.makeUnsafe(NOW - 60_000),
        expiresAt: DateTime.makeUnsafe(NOW + 3_600_000),
        lastReachedAt: null,
        lastError: null,
      } as never),
  });

/**
 * This machine: a slot gate with slots off (available), a CPU sample unless `noSample`, and this
 * project's repository key (`resolve` replaces that read).
 */
const hereServices = (
  options: {
    readonly noSample?: boolean;
    readonly resolve?: Effect.Effect<never>;
  } = {},
) =>
  Layer.mergeAll(
    Layer.mock(SlotGate.SlotGate)({}),
    Layer.mock(ServerSettings.ServerSettingsService)({
      getSettings: Effect.succeed({
        ...DEFAULT_SERVER_SETTINGS,
        agentSlots: { ...DEFAULT_SERVER_SETTINGS.agentSlots, enabled: false },
      }),
    }),
    Layer.mock(ProjectService.ProjectService)({
      getById: () => Effect.succeedSome({ workspaceRoot: "/work/widgets" } as never),
    }),
    Layer.mock(RepositoryIdentityResolver.RepositoryIdentityResolver)({
      resolve: () => options.resolve ?? Effect.succeed({ canonicalKey: KEY } as never),
    }),
    options.noSample === true
      ? Layer.empty
      : Layer.mock(HostResources.HostResources)({ read: Effect.succeed(hostFacts(0.5)) }),
  );

const goodFacts = {
  hostResources: hostFacts(0.1),
  placement: { labels: [], available: true, factsAt: NOW },
};

/** The advice for one call, with `make` and `advise` under the same services. The test sets the clock. */
const adviseWith = (
  mode: PlacementMode,
  layer: Layer.Layer<never>,
  withScope: McpInvocationContext.McpInvocationScope | null = scope,
) =>
  Effect.gen(function* () {
    const { advise } = yield* PlacementAdvice.make();
    const advice = advise(input(mode));
    return yield* withScope === null
      ? advice
      : advice.pipe(Effect.provideService(McpInvocationContext.McpInvocationContext, withScope));
  }).pipe(Effect.provide(layer));

describe("fork placement advice (P6)", () => {
  it.effect("placement off: no advice, and no machine is asked", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const calls: Array<string> = [];
      const layer = Layer.mergeAll(
        Layer.mock(PeerLinks.PeerLinks)({ enabled: Effect.die("links read") }),
        forwardingTo("never", calls),
        Layer.mock(SlotGate.SlotGate)({ usage: Effect.die("slots read") }),
      );
      assert.strictEqual(yield* adviseWith("off", layer), undefined);
      assert.deepStrictEqual(calls, []);
    }),
  );

  it.effect("a linked machine that fits is picked, by its name, with its reasons", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const calls: Array<string> = [];
      const layer = Layer.mergeAll(
        hereServices({ noSample: true }),
        linksTo(),
        forwardingTo({ facts: goodFacts }, calls),
      );
      const advice = yield* adviseWith("advise", layer);
      assert.isDefined(advice);
      assert.isTrue(
        advice.startsWith(
          `${PlacementAdvice.NOTE_PREFIX} a worktree child would run on Build box (${boxId}).`,
        ),
        advice,
      );
      // Here has no CPU sample, so it is the fallback after the linked machine.
      assert.include(advice, `Order: Build box (${boxId}), this machine (${hereId}).`);
      assert.isTrue(advice.endsWith(PlacementAdvice.NOTE_SHARED_STAYS), advice);
      assert.deepStrictEqual(calls.toSorted(), [
        "orchestrator_capabilities",
        "t3_environment_read",
        "t3_project_list",
      ]);
    }),
  );

  it.effect("a linked machine on another provider is left out with H7, here is picked", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const layer = Layer.mergeAll(
        hereServices(),
        linksTo(),
        forwardingTo({ facts: goodFacts, driverKind: "claudeAgent" }, []),
      );
      const advice = yield* adviseWith("advise", layer);
      assert.include(advice, "a worktree child would run here.");
      assert.include(advice, `Left out: Build box (${boxId}): H7 The provider is not there.`);
    }),
  );

  it.effect("a linked machine that never answers is left out after the window (H1)", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const layer = Layer.mergeAll(hereServices(), linksTo(), forwardingTo("never", []));
      const fiber = yield* adviseWith("advise", layer).pipe(Effect.forkChild);
      yield* TestClock.adjust(PlacementAdvice.LINKED_READ_TIMEOUT);
      const advice = yield* Fiber.join(fiber);
      assert.include(advice, "a worktree child would run here.");
      assert.include(advice, `Build box (${boxId}): H1 The machine did not answer now.`);
      assert.notInclude(advice, PlacementAdvice.NOTE_TIMED_OUT);
    }),
  );

  it.effect("which machines were asked: links off here, no MCP caller", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const calls: Array<string> = [];
      const off = Layer.mergeAll(hereServices(), linksTo(false), forwardingTo("never", calls));
      const linksOff = yield* adviseWith("advise", off);
      assert.include(linksOff, "would run here.");
      assert.include(linksOff, PlacementAdvice.NOTE_LINKS_OFF);
      const betaOffInScope = yield* adviseWith(
        "advise",
        Layer.mergeAll(hereServices(), linksTo(), forwardingTo("never", calls)),
        { ...scope, linkedEnvironments: false },
      );
      assert.include(betaOffInScope, PlacementAdvice.NOTE_LINKS_OFF);
      const noCaller = yield* adviseWith(
        "advise",
        Layer.mergeAll(hereServices(), linksTo(), forwardingTo("never", calls)),
        null,
      );
      assert.include(noCaller, PlacementAdvice.NOTE_NO_CALLER);
      const noLinks = yield* adviseWith("advise", hereServices());
      assert.include(noLinks, PlacementAdvice.NOTE_LINKS_MISSING);
      assert.deepStrictEqual(calls, [], "no linked machine was asked");
    }),
  );

  it.effect("a defect reads as no advice; the task goes on", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const layer = Layer.mergeAll(
        // A defect outside every per-machine window.
        hereServices({ resolve: Effect.die("identity read crashed") }),
        linksTo(),
        forwardingTo("never", []),
      );
      assert.strictEqual(yield* adviseWith("advise", layer), PlacementAdvice.NOTE_FAILED);
    }),
  );

  it.effect("advice that takes too long is cut off with a note", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const layer = Layer.mergeAll(
        // This project's identity read hangs: no per-machine window bounds it.
        hereServices({ resolve: Effect.never }),
        linksTo(),
        forwardingTo("never", []),
      );
      const fiber = yield* adviseWith("advise", layer).pipe(Effect.forkChild);
      yield* TestClock.adjust(PlacementAdvice.ADVICE_TIMEOUT);
      assert.strictEqual(yield* Fiber.join(fiber), PlacementAdvice.NOTE_TIMED_OUT);
    }),
  );
});

describe("fork placement advice text", () => {
  const names = new Map([
    ["a", "this machine (a)"],
    ["b", "Build box (b)"],
  ]);
  const verdict = (environmentId: string, here: boolean, rank: number | null) => ({
    environmentId,
    here,
    failures: rank === null ? [{ rule: "H4" as const, reason: "Busy." }] : [],
    rank,
  });

  it("off gives none; none says placing would refuse; enforce says it is not built", () => {
    assert.strictEqual(
      PlacementAdvice.adviceText({ kind: "off", reason: "off" }, names),
      undefined,
    );
    const none: PlacementResult = {
      kind: "none",
      mode: "enforce",
      reason: "No machine fits.",
      verdicts: [verdict("a", true, null)],
    };
    assert.strictEqual(
      PlacementAdvice.adviceText(none, names, "Asked: none."),
      `${PlacementAdvice.NOTE_PREFIX} ${PlacementAdvice.NOTE_ENFORCE_NOT_BUILT} no machine fits a worktree child, so placing would refuse it. No machine fits. Asked: none. ${PlacementAdvice.NOTE_SHARED_STAYS}`,
    );
  });

  it("a pick names the machine, the order and what was left out", () => {
    const picked: PlacementResult = {
      kind: "picked",
      mode: "advise",
      environmentId: "b",
      order: ["b"],
      why: "b passes every hard rule.",
      verdicts: [verdict("a", true, null), verdict("b", false, 1)],
    };
    assert.strictEqual(
      PlacementAdvice.adviceText(picked, names),
      `${PlacementAdvice.NOTE_PREFIX} a worktree child would run on Build box (b). b passes every hard rule. Left out: this machine (a): H4 Busy. ${PlacementAdvice.NOTE_SHARED_STAYS}`,
    );
  });
});
