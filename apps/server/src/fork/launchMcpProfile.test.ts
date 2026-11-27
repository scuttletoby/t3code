/**
 * Fork: a thread with a narrower MCP profile than `all` cannot start a thread that gets more
 * than its own profile, through upstream's real `t3_thread_launch` handler and access rules.
 */
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { describe, expect, it } from "@effect/vitest";
import {
  EnvironmentId,
  OrchestratorMcpFailure,
  type OrchestrationV2ThreadShell,
  type Project as ProjectRecord,
  ProjectId,
  ProviderInstanceId,
  type ServerProvider,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Stream from "effect/Stream";

import * as ServerConfig from "../config.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as McpToolAccess from "../mcp/McpToolAccess.ts";
import * as OrchestratorMcpService from "../mcp/OrchestratorMcpService.ts";
import * as OrchestratorHandlers from "../mcp/toolkits/orchestrator/handlers.ts";
import { OrchestratorToolkit } from "../mcp/toolkits/orchestrator/tools.ts";
import * as ThreadMetadataMcpService from "../mcp/ThreadMetadataMcpService.ts";
import * as ProjectHandlers from "../mcp/toolkits/project/handlers.ts";
import { ProjectToolkit } from "../mcp/toolkits/project/tools.ts";
import * as ThreadLaunch from "../orchestration-v2/ThreadLaunchService.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinkRequests from "../peer/PeerLinkRequests.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as ManagedProjectFolders from "../project/ManagedProjectFolders.ts";
import * as Project from "../project/ProjectService.ts";
import * as DocumentText from "../provider/DocumentText.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as GitVcsDriver from "../vcs/GitVcsDriver.ts";
import * as VcsProcess from "../vcs/VcsProcess.ts";
import {
  OPTION_CASE_PROVIDERS,
  OPTION_CASE_ROWS,
  OPTION_CASES,
} from "./modelPolicyOptions.testkit.ts";
import { type SlotWorld, slotGateOver, slotWorld } from "./slotWorld.testkit.ts";

const PROJECT = ProjectId.make("project:wide");
const NARROW_PROJECT = ProjectId.make("project:narrow");
const CALLER = ThreadId.make("thread:launcher");
const INSTANCE = ProviderInstanceId.make("claudeAgent");
const HERE = EnvironmentId.make("environment-here");
const THERE = EnvironmentId.make("environment-there");

type Settings = Parameters<typeof ServerSettings.layerTest>[0];

/** A profile that keeps one outside server and every T3 tool. */
const DOCS_ONLY: Settings = {
  mcpProfiles: { docs: { servers: ["docs"], t3Tools: "all" } },
} as Settings;

const callerShell = (): OrchestrationV2ThreadShell =>
  ({
    id: CALLER,
    projectId: PROJECT,
    lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: CALLER },
    runtimeMode: "full-access",
    interactionMode: "default",
    deletedAt: null,
    archivedAt: null,
    activeRunId: "run:1",
    providerInstanceId: INSTANCE,
    // What a launch that names no model inherits (the model policy's no-model test).
    modelSelection: { instanceId: INSTANCE, model: "claude-opus" },
  }) as unknown as OrchestrationV2ThreadShell;

/**
 * Upstream's launch handler with a thread caller (or the owner's client with `caller: false`, a
 * linked machine's link with `"linked agent"` (its agent's call, naming the thread) or `"linked
 * owner"`), the settings given, and every launch and forwarded call recorded. With `slots`, the
 * server's slot gate runs over that world; without, the host runs none.
 */
const harness = (
  settings: Settings,
  options: {
    readonly caller?: boolean | "linked agent" | "linked owner";
    readonly slots?: SlotWorld;
    /** This machine's providers (none unless named). */
    readonly providers?: ReadonlyArray<ServerProvider>;
  } = {},
) => {
  const launched: Array<ThreadLaunch.ThreadLaunchInput> = [];
  const forwarded: Array<unknown> = [];
  const threadCaller = options.caller === undefined || options.caller === true;
  const linkSender = { environmentId: THERE, threadId: "thread:sender", title: "Sender" };
  const base = Layer.mergeAll(
    Layer.mock(ProviderRegistry.ProviderRegistry)({
      getProviders: Effect.succeed(options.providers ?? []),
      refreshWorkspaceSnapshot: () => Effect.succeed([]),
    }),
    ServerSettings.layerTest({
      fallbackModelSelection: { instanceId: INSTANCE, model: "claude-opus" },
      ...settings,
    } as Settings),
    Layer.succeed(McpInvocationContext.McpInvocationContext, {
      environmentId: HERE,
      requestNamespace: "thread:launcher:session",
      thread: threadCaller
        ? { threadId: CALLER, providerSessionId: "session", providerInstanceId: INSTANCE }
        : undefined,
      client: threadCaller
        ? undefined
        : options.caller === false
          ? { sessionId: "session-1", label: "Owner", access: "full-access" }
          : {
              sessionId: "link-1",
              label: "Laptop",
              access: "full-access",
              linked: true,
              ...(options.caller === "linked agent" ? { senderThread: linkSender as never } : {}),
            },
      issuedAt: 0,
      capabilities: new Set(["orchestration" as const]),
    }),
    Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadShell: (threadId) => {
        if (threadId === CALLER) return Effect.succeed(callerShell());
        const launch = launched.find((candidate) => candidate.threadId === threadId);
        return Effect.succeed(
          launch === undefined
            ? null
            : ({
                id: launch.threadId,
                projectId: launch.projectId,
                modelSelection: launch.modelSelection,
                deletedAt: null,
              } as unknown as OrchestrationV2ThreadShell),
        );
      },
    }),
    Layer.mock(ThreadLaunch.ThreadLaunchService)({
      checkWorktreeBase: () => Effect.void,
      launch: (launch) => {
        launched.push(launch);
        return Effect.succeed({
          threadId: launch.threadId,
          projection: {
            thread: {
              id: launch.threadId,
              projectId: launch.projectId,
              title: launch.title,
              modelSelection: launch.modelSelection,
            },
            runs: [],
          },
          resumed: false,
        } as unknown as ThreadLaunch.ThreadLaunchResult);
      },
    }),
    Layer.mock(Project.ProjectService)({
      getById: (id) =>
        Effect.succeed(
          id === PROJECT || id === NARROW_PROJECT
            ? Option.some({
                id,
                workspaceRoot: "/projects/target",
                defaultModelSelection: { instanceId: INSTANCE, model: "claude-opus" },
              } as unknown as ProjectRecord)
            : Option.none(),
        ),
      workspaceScanDirectory: Effect.succeed,
    }),
    Layer.mock(PeerForwarding.PeerForwarding)({
      call: (_scope, _tool, environmentId, params) => {
        forwarded.push({ environmentId, params });
        // What the machine there answers does not matter here, only that the call went.
        return Effect.fail(
          new OrchestratorMcpFailure({ code: "orchestration_error", message: "sent there" }),
        ) as never;
      },
    }),
    Layer.mock(ManagedProjectFolders.ManagedProjectFolders)({
      namedProjectsRoot: "/projects",
      isScratchProject: () => Effect.succeed(false),
    }),
    DocumentText.layer.pipe(Layer.provideMerge(NodeServices.layer)),
  ).pipe(
    Layer.provideMerge(GitVcsDriver.layer),
    Layer.provideMerge(VcsProcess.layer),
    Layer.provideMerge(
      ServerConfig.layerTest(process.cwd(), { prefix: "t3-launch-mcp-profile-" }).pipe(
        Layer.provide(NodeServices.layer),
      ),
    ),
    Layer.provideMerge(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer)),
  );
  const dependencies =
    options.slots === undefined
      ? base
      : slotGateOver(options.slots, INSTANCE).pipe(Layer.provideMerge(base));
  const launch = (input: {
    readonly projectId?: ProjectId;
    readonly environmentId?: EnvironmentId | undefined;
    readonly clientRequestId?: string | undefined;
    /** `null`: a launch without a message, which starts no run. */
    readonly message?: string | null;
    /** The launch's model; `null` names none (it inherits the caller's). */
    readonly modelSelection?: Record<string, unknown> | null;
  }) =>
    Effect.gen(function* () {
      const toolkit = yield* ProjectToolkit.pipe(
        Effect.provide(
          McpToolAccess.HandlersLayer.layer(ProjectHandlers.layer).pipe(
            Layer.provide(dependencies),
          ),
        ),
      );
      return yield* toolkit
        .handle("t3_thread_launch", {
          title: "Launched",
          projectId: input.projectId ?? PROJECT,
          ...(input.message === null ? {} : { message: input.message ?? "Do the work" }),
          ...(input.modelSelection === null
            ? {}
            : {
                modelSelection: input.modelSelection ?? {
                  instanceId: INSTANCE,
                  model: "claude-opus",
                },
              }),
          ...(input.environmentId === undefined ? {} : { environmentId: input.environmentId }),
          ...(input.clientRequestId === undefined
            ? {}
            : { clientRequestId: input.clientRequestId }),
        } as never)
        .pipe(Stream.unwrap, Stream.runCollect, Effect.provide(dependencies));
    });
  const scheduled: Array<unknown> = [];
  /** Upstream's real `schedule_task` handler, the other tool that starts threads. */
  const schedule = (input: {
    readonly environmentId?: EnvironmentId;
    readonly scheduledTaskId?: string;
  }) =>
    Effect.gen(function* () {
      const service = Layer.mergeAll(
        Layer.mock(OrchestratorMcpService.OrchestratorMcpService)({
          scheduleTask: (_scope, task) => {
            scheduled.push(task);
            return Effect.fail(
              new OrchestratorMcpFailure({
                code: "orchestration_error",
                message: "scheduled here",
              }),
            ) as never;
          },
          updateScheduledTask: (_scope, task) => {
            scheduled.push(task);
            return Effect.fail(
              new OrchestratorMcpFailure({ code: "orchestration_error", message: "updated here" }),
            ) as never;
          },
        }),
        Layer.mock(ThreadMetadataMcpService.ThreadMetadataMcpService)({}),
        Layer.mock(PeerLinks.PeerLinks)({}),
        Layer.mock(PeerLinkRequests.PeerLinkRequests)({}),
        Layer.mock(RemoteDelegation.RemoteDelegation)({}),
      ).pipe(Layer.provideMerge(dependencies));
      const toolkit = yield* OrchestratorToolkit.pipe(
        Effect.provide(
          McpToolAccess.HandlersLayer.layer(OrchestratorHandlers.layer).pipe(
            Layer.provide(service),
          ),
        ),
      );
      return yield* toolkit
        .handle("schedule_task", {
          prompt: "Do the work",
          schedule: { type: "interval", everyMs: 3_600_000 },
          ...input,
        } as never)
        .pipe(Stream.unwrap, Stream.runCollect, Effect.provide(service));
    });
  return { launched, forwarded, scheduled, launch, schedule };
};

/** `code: message` of a refused call, or `undefined` for one that went through. */
const refusal = (chunks: Iterable<unknown>): string | undefined => {
  const last = [...chunks].at(-1) as
    | { readonly isFailure: boolean; readonly result: { code?: string; message?: string } }
    | undefined;
  return last?.isFailure === true ? `${last.result.code}: ${last.result.message}` : undefined;
};

const pickFor = (profile: string) => ({ mcpThreadProfiles: { [CALLER]: profile } });

it.effect("a narrowed thread cannot launch a thread that gets every MCP server (#681)", () =>
  Effect.gen(function* () {
    const { launched, launch } = harness({ ...DOCS_ONLY, ...pickFor("docs") } as Settings);
    const message = refusal(yield* launch({}));
    expect(message).toContain('capability_denied: This thread\'s MCP profile is "docs"');
    expect(message).toContain("delegate_task");
    expect(launched).toHaveLength(0);
    // Run again (an agent's retry with the same key, or a new call): refused each time.
    for (const clientRequestId of ["retry-1", "retry-1", undefined]) {
      expect(refusal(yield* launch({ clientRequestId }))).toContain("delegate_task");
    }
    expect(launched).toHaveLength(0);
  }),
);

it.effect("t3-only on the launcher refuses a launch into a project left on all (#681)", () =>
  Effect.gen(function* () {
    const { launched, launch } = harness(pickFor("t3-only") as Settings);
    expect(refusal(yield* launch({}))).toContain("t3-only");
    expect(launched).toHaveLength(0);
  }),
);

it.effect("a refusal names what the new thread keeps: only built-in tools here", () =>
  Effect.gen(function* () {
    // Every MCP server and T3 tool, but no built-in tool: a launch into a project on all is
    // wider only in its built-in tools, and the refusal says so.
    const bare = { servers: "all", t3Tools: "all", builtinTools: "none" };
    const { launched, launch } = harness({
      mcpProfiles: { bare },
      ...pickFor("bare"),
    } as Settings);
    const message = refusal(yield* launch({}));
    expect(message).toContain('would get "all", which keeps built-in tools "bare" does not');
    expect(message).not.toContain("MCP servers");
    expect(launched).toHaveLength(0);
    // A launcher on docs: the thread on all keeps more MCP servers.
    const docs = harness({ ...DOCS_ONLY, ...pickFor("docs") } as Settings);
    expect(refusal(yield* docs.launch({}))).toContain('which keeps MCP servers "docs" does not');
  }),
);

it.effect("a launch the launcher's profile covers starts, also a narrower one", () =>
  Effect.gen(function* () {
    const settings = {
      ...DOCS_ONLY,
      ...pickFor("docs"),
      projectSettingsFolded: true,
      // `docs` is within `docs`; `t3-only` (no outside server) is within it too.
      projectSettingsOverrides: { [PROJECT]: { mcpProfile: "docs" } },
      mcpProfile: "t3-only",
    } as Settings;
    const { launched, launch } = harness(settings);
    expect(refusal(yield* launch({ projectId: PROJECT }))).toBeUndefined();
    expect(refusal(yield* launch({ projectId: NARROW_PROJECT }))).toBeUndefined();
    expect(launched.map((entry) => entry.projectId)).toEqual([PROJECT, NARROW_PROJECT]);
  }),
);

it.effect("a launcher on all, and the owner's own client, launch as before", () =>
  Effect.gen(function* () {
    const wide = harness({});
    expect(refusal(yield* wide.launch({}))).toBeUndefined();
    expect(wide.launched).toHaveLength(1);
    // The owner's client has no calling thread, so no profile to keep to.
    const owner = harness({ ...DOCS_ONLY, mcpProfile: "docs" } as Settings, { caller: false });
    expect(refusal(yield* owner.launch({}))).toBeUndefined();
    expect(owner.launched).toHaveLength(1);
  }),
);

it.effect("a launcher whose profile cannot be worked out is refused, never taken as all", () =>
  Effect.gen(function* () {
    // A pick of a profile no setting defines (an old or empty state): it cannot apply.
    const { launched, launch } = harness(pickFor("deleted-profile") as Settings);
    expect(refusal(yield* launch({}))).toContain("cannot be worked out");
    expect(launched).toHaveLength(0);
  }),
);

it.effect("a launch whose new thread's profile cannot be worked out is refused (#681)", () =>
  Effect.gen(function* () {
    // The project names a profile no setting defines (an old or deleted one): never taken as all.
    const { launched, launch } = harness({
      ...DOCS_ONLY,
      ...pickFor("docs"),
      projectSettingsFolded: true,
      projectSettingsOverrides: { [NARROW_PROJECT]: { mcpProfile: "deleted-profile" } },
    } as Settings);
    for (const clientRequestId of [undefined, "retry-1", "retry-1", "retry-2"]) {
      const message = refusal(yield* launch({ projectId: NARROW_PROJECT, clientRequestId }));
      expect(message).toContain(
        "invalid_request: The new thread's MCP profile cannot be worked out",
      );
    }
    expect(launched).toHaveLength(0);
    // A project on all is refused for being wider, a different refusal.
    expect(refusal(yield* launch({ projectId: PROJECT }))).toContain("capability_denied");
    expect(launched).toHaveLength(0);
  }),
);

it.effect("a narrowed thread cannot launch on another machine; an all thread can (#681)", () =>
  Effect.gen(function* () {
    const narrow = harness({ ...DOCS_ONLY, ...pickFor("docs") } as Settings);
    expect(refusal(yield* narrow.launch({ environmentId: THERE }))).toContain(
      "another machine gets that machine's profile",
    );
    // Refused here, before anything was sent.
    expect(narrow.forwarded).toHaveLength(0);
    const wide = harness({});
    expect(refusal(yield* wide.launch({ environmentId: THERE }))).toContain("sent there");
    expect(wide.forwarded).toHaveLength(1);
  }),
);

it.effect("a narrowed thread cannot schedule a task on another machine either (#681)", () =>
  Effect.gen(function* () {
    const narrow = harness({ ...DOCS_ONLY, ...pickFor("docs") } as Settings);
    expect(refusal(yield* narrow.schedule({ environmentId: THERE }))).toContain(
      "another machine gets that machine's profile",
    );
    expect(narrow.forwarded).toHaveLength(0);
    // Here it goes on to the service, which the launch check of its runs covers.
    expect(refusal(yield* narrow.schedule({}))).toContain("scheduled here");
    // An update there may make its runs start threads there, which get that machine's profile.
    const update = { environmentId: THERE, scheduledTaskId: "scheduled-task:1" };
    expect(refusal(yield* narrow.schedule(update))).toContain("the task was not changed");
    expect(narrow.forwarded).toHaveLength(0);
    const wide = harness({});
    expect(refusal(yield* wide.schedule({ environmentId: THERE }))).toContain("sent there");
    expect(refusal(yield* wide.schedule(update))).toContain("sent there");
    expect(wide.forwarded).toHaveLength(2);
  }),
);

it.effect(
  "a narrowed thread's launch here, then there, then here again: each call is its own (#681)",
  () =>
    Effect.gen(function* () {
      // Runs again: a launch here marks only its own caller, so the next call there is refused.
      const narrow = harness({
        ...DOCS_ONLY,
        ...pickFor("docs"),
        projectSettingsFolded: true,
        mcpProfile: "t3-only",
      } as Settings);
      expect(refusal(yield* narrow.launch({ projectId: NARROW_PROJECT }))).toBeUndefined();
      expect(refusal(yield* narrow.launch({ environmentId: THERE }))).toContain(
        "another machine gets that machine's profile",
      );
      expect(
        refusal(yield* narrow.launch({ projectId: NARROW_PROJECT, clientRequestId: "again" })),
      ).toBeUndefined();
      expect(narrow.forwarded).toHaveLength(0);
      expect(narrow.launched).toHaveLength(2);
    }),
);

describe("t3_thread_launch through the agent slot gate (#706)", () => {
  const ONE_SLOT = { agentSlots: { enabled: true, maxPerMachine: 1 } } as unknown as Settings;
  const world = slotWorld;
  const BUSY = ThreadId.make("thread:busy");

  it.effect("full: refused slots_full, nothing launched; free: launched", () =>
    Effect.gen(function* () {
      const full = harness(ONE_SLOT, { slots: world([BUSY]) });
      const message = refusal(yield* full.launch({}));
      expect(message).toMatch(/^slots_full: /);
      expect(full.launched).toHaveLength(0);
      const free = harness(ONE_SLOT, { slots: world() });
      expect(refusal(yield* free.launch({}))).toBeUndefined();
      expect(free.launched).toHaveLength(1);
    }),
  );

  it.effect("the calling thread is not counted against its own launch", () =>
    Effect.gen(function* () {
      // The launcher's own run is the only one: it waits on the child, so its slot is not one.
      const slots = world([CALLER]);
      const { launched, launch } = harness(ONE_SLOT, { slots });
      expect(refusal(yield* launch({}))).toBeUndefined();
      expect(launched).toHaveLength(1);
    }),
  );

  it.effect("the owner's client (and a launch a linked machine sent here) has no caller", () =>
    Effect.gen(function* () {
      // No thread asks, so nothing is left out: the one running agent fills the slot.
      const full = harness(ONE_SLOT, { caller: false, slots: world([CALLER]) });
      expect(refusal(yield* full.launch({}))).toMatch(/^slots_full: /);
      expect(full.launched).toHaveLength(0);
      const free = harness(ONE_SLOT, { caller: false, slots: world() });
      expect(refusal(yield* free.launch({}))).toBeUndefined();
      expect(free.launched).toHaveLength(1);
    }),
  );

  it.effect("a launch without a message starts no run, so it is not gated", () =>
    Effect.gen(function* () {
      const slots = world([BUSY]);
      const { launched, launch } = harness(ONE_SLOT, { slots });
      expect(refusal(yield* launch({ message: null }))).toBeUndefined();
      expect(launched).toHaveLength(1);
      expect(slots.reads).toBe(0);
    }),
  );

  it.effect("runs again with the same key: refused while full, replayed once accepted", () =>
    Effect.gen(function* () {
      const slots = world([BUSY]);
      const { launched, launch } = harness(ONE_SLOT, { slots });
      expect(refusal(yield* launch({ clientRequestId: "same" }))).toMatch(/^slots_full: /);
      expect(refusal(yield* launch({ clientRequestId: "same" }))).toMatch(/^slots_full: /);
      expect(launched).toHaveLength(0);
      slots.running = [];
      expect(refusal(yield* launch({ clientRequestId: "same" }))).toBeUndefined();
      expect(launched).toHaveLength(1);
      // Full again: the retry of the accepted launch starts nothing new, so it is not refused.
      slots.running = [BUSY];
      const reads = slots.reads;
      expect(refusal(yield* launch({ clientRequestId: "same" }))).toBeUndefined();
      expect(slots.reads).toBe(reads);
      // A new key is a new start, and is refused.
      expect(refusal(yield* launch({ clientRequestId: "other" }))).toMatch(/^slots_full: /);
      // Two launch calls, one thread: the replay asked for the same one.
      expect(launched).toHaveLength(2);
      expect(launched[1]!.threadId).toBe(launched[0]!.threadId);
    }),
  );

  it.effect("a count that cannot be read refuses (fails closed), nothing launched", () =>
    Effect.gen(function* () {
      const slots = { ...world(), fails: true };
      const { launched, launch } = harness(ONE_SLOT, { slots });
      expect(refusal(yield* launch({}))).toMatch(/^slots_full: /);
      expect(launched).toHaveLength(0);
    }),
  );

  it.effect("off, or a host that runs no gate: launched as before, nothing read", () =>
    Effect.gen(function* () {
      const slots = world([BUSY]);
      const off = harness(
        { agentSlots: { mode: "fixed", maxPerMachine: 1 } } as unknown as Settings,
        { slots },
      );
      expect(refusal(yield* off.launch({}))).toBeUndefined();
      expect(off.launched).toHaveLength(1);
      expect(slots.reads).toBe(0);
      const none = harness(ONE_SLOT);
      expect(refusal(yield* none.launch({}))).toBeUndefined();
      expect(none.launched).toHaveLength(1);
    }),
  );

  it.effect("a launch on another machine is that machine's to gate, not this one's", () =>
    Effect.gen(function* () {
      // Full here: the call still goes there (an old machine without the gate starts it as before).
      const slots = world([BUSY]);
      const { forwarded, launched, launch } = harness(ONE_SLOT, { slots });
      expect(refusal(yield* launch({ environmentId: THERE }))).toContain("sent there");
      expect(forwarded).toHaveLength(1);
      expect(launched).toHaveLength(0);
      expect(slots.reads).toBe(0);
    }),
  );
});

describe("the model policy on t3_thread_launch (#731)", () => {
  const ROW = {
    driver: "claudeAgent",
    model: "claude-opus",
    efforts: ["high"],
    defaultEffort: "high",
  };
  const policy = (mode: string, allow: ReadonlyArray<unknown> = [ROW]) =>
    ({ modelPolicy: { mode, allow } }) as Settings;
  const opus = (effort?: string) => ({
    instanceId: INSTANCE,
    model: "claude-opus",
    ...(effort === undefined ? {} : { options: [{ id: "effort", value: effort }] }),
  });

  it.effect("enforce: an agent's launch off the rows, or naming no model, starts nothing", () =>
    Effect.gen(function* () {
      const { launched, launch } = harness(policy("enforce"));
      const cases = [
        [null, "the call names no model"],
        [{ instanceId: INSTANCE, model: "claude-other" }, "claude-other is in no row"],
        [opus("max"), "claude-opus does not allow effort max"],
        [opus(), "claude-opus needs an effort from its row"],
      ] as const;
      for (const [modelSelection, why] of cases) {
        const message = refusal(yield* launch({ modelSelection }));
        expect(message).toContain(`model_unavailable: Model policy refused this pick: ${why}`);
        expect(message).toContain("modelSelection.model");
        // Runs again: refused again.
        expect(refusal(yield* launch({ modelSelection, clientRequestId: "again" }))).toContain(why);
      }
      expect(launched).toHaveLength(0);
      expect(refusal(yield* launch({ modelSelection: opus("high") }))).toBeUndefined();
      expect(launched).toHaveLength(1);
    }),
  );

  it.effect("empty state: enforce with no rows refuses every agent launch", () =>
    Effect.gen(function* () {
      const { launched, launch } = harness(policy("enforce", []));
      expect(refusal(yield* launch({ modelSelection: opus("high") }))).toContain(
        "the policy has no rows",
      );
      expect(launched).toHaveLength(0);
    }),
  );

  it.effect("agents only: the owner's client launches any model (#125)", () =>
    Effect.gen(function* () {
      const { launched, launch } = harness(policy("enforce", []), { caller: false });
      expect(refusal(yield* launch({ modelSelection: opus("max") }))).toBeUndefined();
      expect(launched).toHaveLength(1);
    }),
  );

  it.effect("advise and off: the launch starts as asked", () =>
    Effect.gen(function* () {
      for (const mode of ["advise", "off"]) {
        const { launched, launch } = harness(policy(mode));
        expect(refusal(yield* launch({ modelSelection: null }))).toBeUndefined();
        expect(launched).toHaveLength(1);
      }
    }),
  );
  it.effect("a decoy effort option is read too: [reasoning: high, effort: max] is refused", () =>
    Effect.gen(function* () {
      const { launched, launch } = harness(policy("enforce"));
      const decoy = (first: string, second: string) => ({
        instanceId: INSTANCE,
        model: "claude-opus",
        options: [
          { id: first, value: "high" },
          { id: second, value: "max" },
        ],
      });
      for (const [first, second] of [
        ["reasoning", "effort"],
        ["effort", "reasoningEffort"],
        ["reasoningEffort", "effort"],
      ] as const) {
        expect(refusal(yield* launch({ modelSelection: decoy(first, second) }))).toContain(
          "claude-opus does not allow effort high + max",
        );
      }
      expect(launched).toHaveLength(0);
    }),
  );

  it.effect("a retry with the same key that names a new model is checked again", () =>
    Effect.gen(function* () {
      const { launched, launch } = harness(policy("enforce"));
      // An idle launch (no message, so no run) with an allowed model.
      expect(
        refusal(
          yield* launch({ clientRequestId: "same", message: null, modelSelection: opus("high") }),
        ),
      ).toBeUndefined();
      expect(launched).toHaveLength(1);
      // The retry adds a message (it would start the run) and names a model off the rows.
      for (const modelSelection of [{ instanceId: INSTANCE, model: "claude-other" }, opus("max")]) {
        expect(refusal(yield* launch({ clientRequestId: "same", modelSelection }))).toContain(
          "Model policy refused this pick",
        );
      }
      expect(launched).toHaveLength(1);
      // Runs again with the allowed model, or naming none (it keeps the checked first one): starts.
      expect(
        refusal(yield* launch({ clientRequestId: "same", modelSelection: opus("high") })),
      ).toBeUndefined();
      expect(
        refusal(yield* launch({ clientRequestId: "same", modelSelection: null })),
      ).toBeUndefined();
      expect(launched).toHaveLength(3);
      expect(launched.every((launch) => launch.threadId === launched[0]!.threadId)).toBe(true);
    }),
  );
});

describe("the model policy on a launch sent to a linked machine (#731)", () => {
  const ROW = { driver: "claudeAgent", model: "claude-opus", efforts: ["high"] };
  const policy = (mode: string, allow: ReadonlyArray<unknown> = [ROW]) =>
    ({ modelPolicy: { mode, allow } }) as Settings;
  const opus = (effort: string) => ({
    instanceId: INSTANCE,
    model: "claude-opus",
    options: [{ id: "effort", value: effort }],
  });
  const ULTRACODE_OFF = { id: "ultracode", value: false };

  it.effect("enforce: an agent's launch there off the rows, or naming no model, is not sent", () =>
    Effect.gen(function* () {
      const { forwarded, launched, launch } = harness(policy("enforce"));
      for (const [modelSelection, why] of [
        [null, "the call names no model"],
        [{ instanceId: INSTANCE, model: "claude-other" }, "claude-other is in no row"],
        [opus("max"), "does not allow effort max"],
        // A decoy effort id there: the driver there is not known, so every effort id is read.
        [
          {
            instanceId: INSTANCE,
            model: "claude-opus",
            options: [
              { id: "effort", value: "high" },
              { id: "variant", value: "max" },
            ],
          },
          "does not allow effort high + max",
        ],
      ] as const) {
        const message = refusal(yield* launch({ environmentId: THERE, modelSelection }));
        expect(message).toContain("Model policy refused this pick: ");
        expect(message).toContain(why);
        // Runs again (same key): refused again, still nothing sent.
        expect(
          refusal(yield* launch({ environmentId: THERE, modelSelection, clientRequestId: "k" })),
        ).toContain(why);
      }
      expect(forwarded).toHaveLength(0);
      expect(launched).toHaveLength(0);
      // Inside the rows, naming ultracode (a Claude pick sent there must): sent.
      const inside = { ...opus("high"), options: [...opus("high").options, ULTRACODE_OFF] };
      expect(refusal(yield* launch({ environmentId: THERE, modelSelection: inside }))).toContain(
        "sent there",
      );
      expect(forwarded).toHaveLength(1);
    }),
  );

  it.effect("sender off, this machine enforce: an agent's launch sent here is refused here", () =>
    Effect.gen(function* () {
      // The agent's machine has the policy off: it sends the launch.
      const sender = harness(policy("off"));
      expect(
        refusal(yield* sender.launch({ environmentId: THERE, modelSelection: opus("max") })),
      ).toContain("sent there");
      // This machine (enforce) gets it from the link, naming the agent's thread: checked, refused.
      const { launched, launch } = harness(policy("enforce"), { caller: "linked agent" });
      for (const clientRequestId of ["k", "k"]) {
        expect(refusal(yield* launch({ modelSelection: opus("max"), clientRequestId }))).toContain(
          "Model policy refused this pick: claude-opus does not allow effort max",
        );
      }
      expect(launched).toHaveLength(0);
      expect(refusal(yield* launch({ modelSelection: opus("high") }))).toBeUndefined();
      expect(launched).toHaveLength(1);
      // A call the link sends for no thread (the owner's client there) stays unchecked.
      const owner = harness(policy("enforce"), { caller: "linked owner" });
      expect(refusal(yield* owner.launch({ modelSelection: opus("max") }))).toBeUndefined();
      expect(owner.launched).toHaveLength(1);
    }),
  );

  it.effect("the owner's client, advise and off: sent as asked", () =>
    Effect.gen(function* () {
      const owner = harness(policy("enforce", []), { caller: false });
      expect(
        refusal(yield* owner.launch({ environmentId: THERE, modelSelection: null })),
      ).toContain("sent there");
      expect(owner.forwarded).toHaveLength(1);
      for (const mode of ["advise", "off"]) {
        const { forwarded, launch } = harness(policy(mode));
        expect(refusal(yield* launch({ environmentId: THERE, modelSelection: null }))).toContain(
          "sent there",
        );
        expect(forwarded).toHaveLength(1);
      }
    }),
  );
});

describe("the model policy on t3_thread_launch: an option the policy cannot read (#793 review)", () => {
  const policy = (mode: string) =>
    ({ modelPolicy: { mode, allow: OPTION_CASE_ROWS } }) as unknown as Settings;
  const routes = [
    { route: "an agent's launch here", caller: true, environmentId: undefined },
    {
      route: "a linked agent's launch received here",
      caller: "linked agent",
      environmentId: undefined,
    },
    { route: "an agent's launch sent to a linked machine", caller: true, environmentId: THERE },
  ] as const;

  it.effect("enforce: refused on each route, nothing started or sent; without it, it goes", () =>
    Effect.gen(function* () {
      for (const { route, caller, environmentId } of routes) {
        for (const testCase of OPTION_CASES) {
          const name = `${route}: ${testCase.name}`;
          const { forwarded, launched, launch } = harness(policy("enforce"), {
            caller,
            providers: OPTION_CASE_PROVIDERS,
          });
          const modelSelection = (withExtra: boolean) => ({
            instanceId: testCase.instanceId,
            model: testCase.model,
            options: withExtra ? [testCase.allowed, testCase.extra] : [testCase.allowed],
          });
          // Runs again (same key): refused each time.
          for (const clientRequestId of ["k", "k"]) {
            const message = refusal(
              yield* launch({
                environmentId,
                modelSelection: modelSelection(true),
                clientRequestId,
              }),
            );
            if (environmentId === undefined) {
              // Upstream refuses it first here (its option check), as with the policy off.
              expect(message, name).toContain(testCase.launchHere);
            } else {
              expect(message, name).toContain(`Model policy refused this pick: ${testCase.why}`);
              expect(message, name).toContain(testCase.fix);
            }
          }
          expect([launched.length, forwarded.length], name).toEqual([0, 0]);
          const allowed = refusal(
            yield* launch({ environmentId, modelSelection: modelSelection(false) }),
          );
          if (environmentId === undefined) {
            expect(allowed, name).toBeUndefined();
            expect(launched, name).toHaveLength(1);
          } else {
            expect(allowed, name).toContain("sent there");
            expect(forwarded, name).toHaveLength(1);
          }
        }
      }
    }),
  );

  it.effect("advise: the launch goes as with the policy off", () =>
    Effect.gen(function* () {
      for (const { route, caller, environmentId } of routes) {
        for (const mode of ["advise", "off"]) {
          const name = `${route} (${mode})`;
          const { forwarded, launched, launch } = harness(policy(mode), {
            caller,
            providers: OPTION_CASE_PROVIDERS,
          });
          for (const testCase of OPTION_CASES) {
            const message = refusal(
              yield* launch({
                environmentId,
                modelSelection: {
                  instanceId: testCase.instanceId,
                  model: testCase.model,
                  options: [testCase.allowed, testCase.extra],
                },
                clientRequestId: testCase.name,
              }),
            );
            // Here upstream refuses an option the model list does not describe, in every
            // mode; a launch sent to a linked machine is checked there.
            if (environmentId === undefined) expect(message, name).toContain(testCase.launchHere);
            else expect(message, name).toContain("sent there");
          }
          expect([launched.length, forwarded.length], name).toEqual(
            environmentId === undefined ? [0, 0] : [0, OPTION_CASES.length],
          );
        }
      }
    }),
  );
});
