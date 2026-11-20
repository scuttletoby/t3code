/**
 * Fork: a thread with a narrower MCP profile than `all` cannot start a thread that gets more
 * than its own profile, through upstream's real `t3_thread_launch` handler and access rules.
 */
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { expect, it } from "@effect/vitest";
import {
  EnvironmentId,
  OrchestratorMcpFailure,
  type OrchestrationV2ThreadShell,
  type Project as ProjectRecord,
  ProjectId,
  ProviderInstanceId,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Stream from "effect/Stream";

import * as ServerConfig from "../config.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as McpToolAccess from "../mcp/McpToolAccess.ts";
import * as ProjectHandlers from "../mcp/toolkits/project/handlers.ts";
import { ProjectToolkit } from "../mcp/toolkits/project/tools.ts";
import * as ThreadLaunch from "../orchestration-v2/ThreadLaunchService.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as ManagedProjectFolders from "../project/ManagedProjectFolders.ts";
import * as Project from "../project/ProjectService.ts";
import * as DocumentText from "../provider/DocumentText.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as GitVcsDriver from "../vcs/GitVcsDriver.ts";
import * as VcsProcess from "../vcs/VcsProcess.ts";

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
  }) as unknown as OrchestrationV2ThreadShell;

/**
 * Upstream's launch handler with a thread caller (or the owner's client with `caller: false`),
 * the settings given, and every launch and forwarded call recorded.
 */
const harness = (settings: Settings, options: { readonly caller?: boolean } = {}) => {
  const launched: Array<ThreadLaunch.ThreadLaunchInput> = [];
  const forwarded: Array<unknown> = [];
  const threadCaller = options.caller !== false;
  const dependencies = Layer.mergeAll(
    Layer.mock(ProviderRegistry.ProviderRegistry)({
      getProviders: Effect.succeed([]),
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
        : { sessionId: "session-1", label: "Owner", access: "full-access" },
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
  const launch = (input: {
    readonly projectId?: ProjectId;
    readonly environmentId?: EnvironmentId;
    readonly clientRequestId?: string | undefined;
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
          message: "Do the work",
          modelSelection: { instanceId: INSTANCE, model: "claude-opus" },
          ...(input.environmentId === undefined ? {} : { environmentId: input.environmentId }),
          ...(input.clientRequestId === undefined
            ? {}
            : { clientRequestId: input.clientRequestId }),
        } as never)
        .pipe(Stream.unwrap, Stream.runCollect, Effect.provide(dependencies));
    });
  return { launched, forwarded, launch };
};

/** `code: message` of a refused launch, or `undefined` for one that went through. */
const refusal = (
  chunks: Effect.Success<ReturnType<ReturnType<typeof harness>["launch"]>>,
): string | undefined => {
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
