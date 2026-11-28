/**
 * Fork (placement design rows P3 and P4, facts out): `t3_environment_read` on this machine returns
 * `hostResources` (an upstream pull request's field) and the fork's `placement`, through upstream's real
 * environment handlers. A linked machine's read is in `environmentFacts.linked.test.ts`.
 */
import { expect, it } from "@effect/vitest";
import {
  ENVIRONMENT_FILE_LINK_AGENT_HINT,
  EnvironmentId,
  ProviderInstanceId,
  ServerSettings,
  ThreadId,
} from "@t3tools/contracts";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as Deferred from "effect/Deferred";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";
import * as Stream from "effect/Stream";
import * as TestClock from "effect/testing/TestClock";

import * as Environment from "../environment/ServerEnvironment.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as McpToolAccess from "../mcp/McpToolAccess.ts";
import { liveThreadShell } from "../mcp/McpToolAccess.testkit.ts";
import * as EnvironmentHandlers from "../mcp/toolkits/environment/handlers.ts";
import { EnvironmentToolkit } from "../mcp/toolkits/environment/tools.ts";
import * as ThreadCommandExecutor from "../orchestration-v2/ThreadCommandExecutor.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as Settings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as EnvironmentFacts from "./environmentFacts.ts";
import { type FactsCommandResult, FactsCommands } from "./factsCommand.ts";

const environmentId = EnvironmentId.make("environment:facts");
const threadId = ThreadId.make("thread:facts");
const fromFile = Schema.decodeUnknownSync(ServerSettings);

const SLOTS_ON = fromFile({
  agentSlots: { enabled: true, maxPerMachine: 3, minFreeMemoryMb: 512 },
});

const host = {
  sampledAt: 1_700_000_000_000,
  cpuUtilization: 0.25,
  cpuCount: 8,
  availableMemoryBytes: 4096 * 1024 * 1024,
  totalMemoryBytes: 16 * 1024 ** 3,
};

const usageOf = (used: number, freeMemoryMb = 4096): SlotGate.SlotUsage => ({
  machine: { used, max: 3 },
  accounts: [],
  freeMemoryMb,
});

interface World {
  /** The slot gate's status read; `"missing"`: no gate on this server. */
  readonly usage?: Effect.Effect<SlotGate.SlotUsage | null> | "missing";
  /** `"missing"`: no host sampler on this server; `"hangs"`: its read never answers. */
  readonly host?: "missing" | "hangs";
  /**
   * `"unreadable"`: the settings file can no longer be read once upstream's part of the read has
   * read it (upstream's read fails the whole call when its own settings read fails).
   */
  readonly settings?: ServerSettings | "unreadable";
  /** This server's `factsCommand` runs; `"missing"`: no runner here. Default: never asked. */
  readonly facts?: ((factsCommand: string) => Effect.Effect<FactsCommandResult>) | "missing";
}

const settingsGoneAfterFirstRead = () => {
  let reads = 0;
  return Effect.suspend(() =>
    ++reads === 1
      ? Effect.succeed(SLOTS_ON)
      : Effect.fail({ _tag: "ServerSettingsError" } as never),
  );
};

const layerWorld = (world: World) =>
  Layer.mergeAll(
    ThreadCommandExecutor.layer,
    Layer.succeed(McpInvocationContext.McpInvocationContext, {
      environmentId,
      requestNamespace: "provider:facts",
      thread: {
        threadId,
        providerSessionId: "provider:facts",
        providerInstanceId: ProviderInstanceId.make("codex"),
      },
      client: undefined,
      capabilities: new Set(["orchestration" as const]),
      issuedAt: 0,
    }),
    Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadShell: () => Effect.succeed(liveThreadShell(threadId)),
    }),
    Layer.mock(Environment.ServerEnvironment)({
      getDescriptor: Effect.succeed({
        environmentId,
        label: "Facts",
        platform: { os: "linux", arch: "x64" },
        serverVersion: "0.0.0",
        capabilities: { repositoryIdentity: false },
      }),
    }),
    Layer.mock(Settings.ServerSettingsService)({
      getSettings:
        world.settings === "unreadable"
          ? settingsGoneAfterFirstRead()
          : Effect.succeed(world.settings ?? SLOTS_ON),
    }),
    world.host === "missing"
      ? Layer.empty
      : Layer.mock(HostResources.HostResources)({
          read: world.host === "hangs" ? Effect.never : Effect.succeed(host),
        }),
    world.usage === "missing"
      ? Layer.empty
      : Layer.mock(SlotGate.SlotGate)({ usage: world.usage ?? Effect.succeed(usageOf(1)) }),
    world.facts === "missing"
      ? Layer.empty
      : Layer.mock(FactsCommands)({
          run: world.facts ?? (() => Effect.die("factsCommand runs only when one is set")),
        }),
  );

/** Calls the tool as the MCP server does: the handler's result, then its JSON encoding. */
const readFacts = (world: World) =>
  Effect.gen(function* () {
    const toolkit = yield* EnvironmentToolkit;
    const results = yield* toolkit
      .handle("t3_environment_read", {})
      .pipe(Stream.unwrap, Stream.runCollect);
    const last = results.at(-1)!;
    expect(last.isFailure).toBe(false);
    return last.encodedResult as {
      readonly hostResources?: unknown;
      readonly placement?: Record<string, unknown>;
      readonly fileLinks?: unknown;
    };
  }).pipe(
    Effect.provide(
      McpToolAccess.HandlersLayer.layer(EnvironmentHandlers.layer).pipe(
        Layer.provideMerge(layerWorld(world)),
      ),
    ),
  );

it.effect("this machine's read carries hostResources and placement, with the facts time", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(1_800_000_000_000);
    const result = yield* readFacts({});
    expect(result.hostResources).toEqual(host);
    expect(result.placement).toEqual({
      labels: [],
      available: true,
      runningAgents: 1,
      slotsFree: 2,
      memoryFloorMb: 512,
      freeMemoryMb: 4096,
      factsAt: 1_800_000_000_000,
    });
  }),
);

it.effect("this machine's placement labels are its facts' labels, as the settings hold them", () =>
  Effect.gen(function* () {
    const labelled = fromFile({
      agentSlots: { enabled: true, maxPerMachine: 3, minFreeMemoryMb: 512 },
      placement: { mode: "advise", labels: ["GPU", "gpu", "ci-runner"] },
    });
    const { placement } = yield* readFacts({ settings: labelled });
    expect(placement?.labels).toEqual(["gpu", "ci-runner"]);
    // A placement value the file holds as unreadable reads as off: no labels are claimed.
    const broken = fromFile({ placement: { mode: "advise", labels: "gpu" } });
    expect((yield* readFacts({ settings: broken })).placement?.labels).toEqual([]);
  }),
);

it.effect("a slot gate that cannot count says unavailable with a reason, never slots free", () =>
  Effect.gen(function* () {
    for (const usage of [
      Effect.succeed(null),
      Effect.die("the thread list read crashed"),
      // Longer than the gate's own read may take: the read gives up and says so.
      Effect.never,
    ]) {
      const fiber = yield* readFacts({ usage }).pipe(Effect.forkChild);
      yield* TestClock.adjust(Duration.seconds(7));
      const { placement } = yield* Fiber.join(fiber);
      expect(placement).toMatchObject({
        available: false,
        reason: EnvironmentFacts.REASON_GATE_UNREAD,
      });
      expect(placement).not.toHaveProperty("slotsFree");
      expect(placement).not.toHaveProperty("runningAgents");
      expect(placement?.factsAt).toBeTypeOf("number");
    }
  }),
);

it.effect("no slot gate, or settings that cannot be read, give no count either", () =>
  Effect.gen(function* () {
    const missing = yield* readFacts({ usage: "missing" });
    expect(missing.placement).toEqual({
      labels: [],
      available: false,
      reason: EnvironmentFacts.REASON_GATE_MISSING,
      factsAt: 0,
    });
    const unreadable = yield* readFacts({ settings: "unreadable" });
    expect(unreadable.placement).toEqual({
      labels: [],
      available: false,
      reason: EnvironmentFacts.REASON_SETTINGS_UNREAD,
      factsAt: 0,
    });
  }),
);

it.effect("slots off (an empty or missing settings file) count nothing and limit nothing", () =>
  Effect.gen(function* () {
    for (const settings of [fromFile({}), fromFile({ agentSlots: { enabled: false } })]) {
      const { placement } = yield* readFacts({ settings, usage: Effect.die("not asked") });
      expect(placement).toEqual({
        labels: [],
        available: true,
        reason: EnvironmentFacts.REASON_SLOTS_OFF,
        factsAt: 0,
      });
    }
  }),
);

it.effect("a full machine, or one below its memory floor, is not available", () =>
  Effect.gen(function* () {
    const full = yield* readFacts({ usage: Effect.succeed(usageOf(4)) });
    expect(full.placement).toMatchObject({
      available: false,
      reason: EnvironmentFacts.REASON_FULL,
      runningAgents: 4,
      slotsFree: 0,
    });
    const low = yield* readFacts({ usage: Effect.succeed(usageOf(1, 100)) });
    expect(low.placement).toMatchObject({
      available: false,
      reason: EnvironmentFacts.REASON_LOW_MEMORY,
      slotsFree: 2,
      freeMemoryMb: 100,
      memoryFloorMb: 512,
    });
    const noLimit = yield* readFacts({
      settings: fromFile({ agentSlots: { enabled: true, maxPerAccount: { codex: 2 } } }),
      usage: Effect.succeed({ ...usageOf(1), machine: { used: 1, max: null } }),
    });
    expect(noLimit.placement).toMatchObject({
      available: true,
      slotsFree: null,
      memoryFloorMb: null,
    });
  }),
);

it.effect("no host sampler leaves hostResources out; placement still comes", () =>
  Effect.gen(function* () {
    const result = yield* readFacts({ host: "missing" });
    expect(result).not.toHaveProperty("hostResources");
    expect(result.placement).toMatchObject({ available: true, slotsFree: 2 });
  }),
);

it.effect("a host read that hangs is given up at its own limit; placement still comes", () =>
  Effect.gen(function* () {
    const fiber = yield* readFacts({ host: "hangs" }).pipe(Effect.forkChild);
    yield* TestClock.adjust(EnvironmentFacts.HOST_READ_TIMEOUT);
    const result = yield* Fiber.join(fiber);
    expect(result).not.toHaveProperty("hostResources");
    expect(result.placement).toMatchObject({ available: true, slotsFree: 2 });
  }),
);

it.effect("runs again: the same shape, measured again each time", () =>
  Effect.gen(function* () {
    let asked = 0;
    const usage = Effect.sync(() => usageOf(++asked));
    const first = yield* readFacts({ usage });
    yield* TestClock.adjust(Duration.seconds(30));
    const second = yield* readFacts({ usage });
    expect(Object.keys(second).toSorted()).toEqual(Object.keys(first).toSorted());
    expect(Object.keys(second.placement!).toSorted()).toEqual(
      Object.keys(first.placement!).toSorted(),
    );
    expect(first.placement).toMatchObject({ runningAgents: 1, slotsFree: 2, factsAt: 0 });
    expect(second.placement).toMatchObject({ runningAgents: 2, slotsFree: 1, factsAt: 30_000 });
  }),
);

it.effect("a read stopped half-way leaves nothing behind: the next one reads afresh", () =>
  Effect.gen(function* () {
    const asked = yield* Deferred.make<void>();
    let calls = 0;
    const usage = Effect.suspend(() =>
      ++calls === 1
        ? Deferred.succeed(asked, undefined).pipe(Effect.andThen(Effect.never))
        : Effect.succeed(usageOf(2)),
    );
    const stopped = yield* readFacts({ usage }).pipe(Effect.forkChild);
    yield* Deferred.await(asked);
    yield* Fiber.interrupt(stopped);
    const next = yield* readFacts({ usage });
    expect(calls).toBe(2);
    expect(next.placement).toMatchObject({ available: true, runningAgents: 2, slotsFree: 1 });
  }),
);

it.effect("this machine's read says how to link a file on it from another environment", () =>
  Effect.gen(function* () {
    const result = yield* readFacts({});
    expect(result.fileLinks).toBe(ENVIRONMENT_FILE_LINK_AGENT_HINT);
    expect(result.fileLinks).toContain("/t3-environment/<environmentId>/<absolute path>");
  }),
);

const withPlacement = (placement: Record<string, unknown>) =>
  fromFile({ agentSlots: { enabled: true, maxPerMachine: 3, minFreeMemoryMb: 512 }, placement });

it.effect("labels are this machine's setting and its factsCommand's lines, canonical", () =>
  Effect.gen(function* () {
    const asked: Array<string> = [];
    const { placement } = yield* readFacts({
      settings: withPlacement({ labels: ["owner-logins", "GPU"], factsCommand: "/opt/facts" }),
      facts: (command) =>
        Effect.sync(() => {
          asked.push(command);
          return { available: true, labels: ["gpu", "ci-runner"] };
        }),
    });
    expect(asked).toEqual(["/opt/facts"]);
    expect(placement).toEqual({
      labels: ["owner-logins", "gpu", "ci-runner"],
      available: true,
      runningAgents: 1,
      slotsFree: 2,
      memoryFloorMb: 512,
      freeMemoryMb: 4096,
      factsAt: 0,
    });
  }),
);

it.effect("no factsCommand set: the labels alone, nothing runs, even with no runner here", () =>
  Effect.gen(function* () {
    const settings = withPlacement({ labels: ["gpu"] });
    for (const facts of [undefined, "missing"] as const) {
      const { placement } = yield* readFacts({ settings, ...(facts ? { facts } : {}) });
      expect(placement).toMatchObject({ labels: ["gpu"], available: true, slotsFree: 2 });
      expect(placement).not.toHaveProperty("reason");
    }
  }),
);

it.effect("a factsCommand that says busy makes the machine not available, with its reason", () =>
  Effect.gen(function* () {
    const { placement } = yield* readFacts({
      settings: withPlacement({ labels: ["gpu"], factsCommand: "/opt/facts" }),
      facts: () =>
        Effect.succeed({ available: false, reason: "factsCommand exited with code 1: in a game" }),
    });
    // The slot counts stay: they are still true; only the answer changes.
    expect(placement).toEqual({
      labels: ["gpu"],
      available: false,
      reason: "factsCommand exited with code 1: in a game",
      runningAgents: 1,
      slotsFree: 2,
      memoryFloorMb: 512,
      freeMemoryMb: 4096,
      factsAt: 0,
    });
  }),
);

it.effect("a full machine whose factsCommand also says no keeps both reasons, slots first", () =>
  Effect.gen(function* () {
    const { placement } = yield* readFacts({
      settings: withPlacement({ factsCommand: "/opt/facts" }),
      usage: Effect.succeed(usageOf(3)),
      facts: () => Effect.succeed({ available: false, reason: "factsCommand did not answer" }),
    });
    expect(placement).toMatchObject({
      available: false,
      reason: `${EnvironmentFacts.REASON_FULL} factsCommand did not answer`,
      slotsFree: 0,
    });
  }),
);

it.effect("slots off, factsCommand says no: not available, the command's reason", () =>
  Effect.gen(function* () {
    const { placement } = yield* readFacts({
      settings: fromFile({ placement: { factsCommand: "/opt/facts" } }),
      usage: Effect.die("not asked"),
      facts: () => Effect.succeed({ available: false, reason: "factsCommand exited with code 2" }),
    });
    expect(placement).toEqual({
      labels: [],
      available: false,
      reason: "factsCommand exited with code 2",
      factsAt: 0,
    });
  }),
);

it.effect("a factsCommand with no runner here, or a run that dies, is never free", () =>
  Effect.gen(function* () {
    const settings = withPlacement({ labels: ["gpu"], factsCommand: "/opt/facts" });
    const missing = yield* readFacts({ settings, facts: "missing" });
    expect(missing.placement).toMatchObject({
      labels: ["gpu"],
      available: false,
      reason: EnvironmentFacts.REASON_FACTS_RUNNER_MISSING,
    });
    const died = yield* readFacts({ settings, facts: () => Effect.die("spawn crashed") });
    expect(died.placement).toMatchObject({
      labels: [],
      available: false,
      reason: EnvironmentFacts.REASON_FACTS_UNREAD,
      slotsFree: 2,
    });
  }),
);

it.effect(
  "a factsCommand this OS cannot run is not available with the problem; it never runs",
  () =>
    Effect.gen(function* () {
      const { placement } = yield* readFacts({
        settings: withPlacement({ labels: ["gpu"], factsCommand: "C:\\tools\\facts.exe" }),
      }).pipe(Effect.provideService(HostProcess.Platform, "linux"));
      expect(placement).toMatchObject({ labels: [], available: false, slotsFree: 2 });
      expect(placement?.reason).toMatch(
        /^This machine's placement setting is not valid on it, so it reads as not available: .*factsCommand/u,
      );
    }),
);

it.effect("a placement setting the file could not read is not available, never free", () =>
  Effect.gen(function* () {
    const { placement } = yield* readFacts({
      settings: withPlacement({ labels: ["gpu"], mode: "sometimes" }),
    });
    expect(placement).toMatchObject({ labels: [], available: false });
    expect(placement?.reason).toContain("placement setting is not valid");
  }),
);
