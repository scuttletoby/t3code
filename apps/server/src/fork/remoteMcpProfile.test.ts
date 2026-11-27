/**
 * Fork: a remote `delegate_task` carries its parent's MCP profile in `delegatedFrom`, and
 * sends a profile narrower than `all` only to a linked machine that says it applies one. Everything
 * around the delegation is faked, the linked machine too: no second machine is needed.
 */
import { assert, it } from "@effect/vitest";
import {
  type EnvironmentId,
  NodeId,
  OrchestrationV2DelegatedFrom,
  type OrchestratorMcpDelegateTaskInput,
  OrchestratorMcpFailure,
  type OrchestratorMcpProviderCapability,
  PeerLinkError,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  RunId,
  ServerSettings,
  ThreadId,
} from "@t3tools/contracts";
import * as Context from "effect/Context";
import * as Deferred from "effect/Deferred";
import * as SecureRandom from "effect/crypto/SecureRandom";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";
import * as TestClock from "effect/testing/TestClock";
import { HttpClient, HttpClientError, HttpClientRequest, HttpClientResponse } from "effect/http";

import * as ServerEnvironment from "../environment/ServerEnvironment.ts";
import { advertisedJsonSchema } from "../mcp/advertisedJsonSchema.ts";
import type * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import { ProjectToolkit } from "../mcp/toolkits/project/tools.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as RepositoryIdentityResolver from "../project/RepositoryIdentityResolver.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import { resolveMcpProfile } from "./mcpProfile.ts";

const box = "environment-box" as EnvironmentId;
const parentThreadId = ThreadId.make("thread:parent");
const taskId = NodeId.make("node:task");
const instanceId = ProviderInstanceId.make("claude");
const modelSelection = { instanceId, model: "claude-opus" };
const fromFile = Schema.decodeSync(ServerSettings);
const launch = Schema.decodeUnknownSync(ProjectToolkit.tools.t3_thread_launch.parametersSchema);
const launchAsSent = Schema.decodeUnknownSync(
  Schema.toCodecJson(ProjectToolkit.tools.t3_thread_launch.parametersSchema),
);
const decodeDelegatedFrom = Schema.decodeSync(OrchestrationV2DelegatedFrom);

const scope: McpInvocationContext.McpThreadInvocationScope = {
  environmentId: "environment-here" as EnvironmentId,
  requestNamespace: "provider-session:parent",
  thread: {
    threadId: parentThreadId,
    providerSessionId: "provider-session:parent",
    providerInstanceId: instanceId,
  },
  client: undefined,
  capabilities: new Set(["orchestration"]),
  issuedAt: 1,
};

const input = {
  task: "Review the change on the box.",
  target: {
    environmentId: box,
    providerInstanceId: instanceId,
    model: "claude-opus",
    projectId: ProjectId.make("project:box"),
  },
  mode: "async",
  clientRequestId: "review-on-box",
} as OrchestratorMcpDelegateTaskInput as Parameters<
  RemoteDelegation.RemoteDelegation["Service"]["delegate"]
>[1];

/**
 * The linked machine: this version ("carries", or "set to refuse"), older (its app page answers),
 * 404 ("refuses"), not linked ("unlinked"), unreachable ("down"), silent ("never"), headers then
 * no body ("stalls"), or answering once `hold` is done.
 */
type Box =
  | "carries"
  | "set to refuse"
  | "older"
  | "refuses"
  | "unlinked"
  | "down"
  | "never"
  | "stalls"
  | { readonly hold: Deferred.Deferred<void> };

/** How `box` answers `request`, as its server would. */
const answer = (peer: Box, request: HttpClientRequest.HttpClientRequest) => {
  const respond = (response: Response) =>
    Effect.succeed(HttpClientResponse.fromWeb(request, response));
  if (typeof peer === "object")
    return Deferred.await(peer.hold).pipe(
      Effect.andThen(respond(Response.json({ mcpProfileCarry: true }))),
    );
  switch (peer) {
    case "carries":
      return respond(Response.json({ mcpProfileCarry: true }));
    case "set to refuse":
      return respond(Response.json({ mcpProfileCarry: false }));
    case "older":
      return respond(
        new Response("<!doctype html><p>app", { headers: { "content-type": "text/html" } }),
      );
    case "refuses":
      return respond(new Response("Not Found", { status: 404 }));
    case "stalls":
      return respond(
        new Response(new ReadableStream({ start() {} }), {
          headers: { "content-type": "application/json" },
        }),
      );
    case "never":
      return Effect.never;
    default:
      return Effect.fail(
        new HttpClientError.HttpClientError({
          reason: new HttpClientError.TransportError({
            request,
            cause: new TypeError("fetch failed"),
          }),
        }),
      );
  }
};

/** A provider instance the box lists in its `orchestrator_capabilities`. */
const providerThere = (id: string, driverKind: string): OrchestratorMcpProviderCapability => ({
  providerInstanceId: ProviderInstanceId.make(id),
  driverKind: ProviderDriverKind.make(driverKind),
  displayName: null,
  models: [],
  canRunChildTask: true,
  canRunCrossProviderChildTask: true,
  constraints: [],
});

const CLAUDE_THERE = [providerThere("claude", "claudeAgent"), providerThere("codex", "codex")];

/**
 * What the box answers to `orchestrator_capabilities`: its providers, a failure ("fails"), or its
 * providers once `hold` is done.
 */
type Capabilities =
  | ReadonlyArray<OrchestratorMcpProviderCapability>
  | "fails"
  | {
      readonly hold: Deferred.Deferred<void>;
      readonly providers: ReadonlyArray<OrchestratorMcpProviderCapability>;
    };

/**
 * The delegation service with the box faked. `settings` is this machine's settings file; the
 * delegating thread has no parent here. The task goes to provider instance `instance` there.
 */
const makeHarness = (
  settings: Record<string, unknown>,
  peer: Box,
  capabilities: Capabilities = CLAUDE_THERE,
  /** `null`: the call names no instance there. */
  instance: string | null = "claude",
  driverKind?: string,
) =>
  Effect.gen(function* () {
    const launched: Array<Record<string, unknown>> = [];
    /** Each `orchestrator_capabilities` call to the box. */
    let capabilityAsks = 0;
    const capabilitiesAsked = yield* Deferred.make<void>();
    /** Each ask of the box: where it went and with which token. */
    const asks: Array<string> = [];
    const resolve = () =>
      peer === "unlinked"
        ? Effect.fail(new PeerLinkError({ reason: "unreachable", message: "box did not answer." }))
        : Effect.succeed({
            link: { label: "box" },
            url: "https://box.test",
            token: "link-token",
          } as never);
    const asked = yield* Deferred.make<void>();
    const http = HttpClient.make((request) => {
      asks.push(`${request.url} ${request.headers.authorization}`);
      return Deferred.succeed(asked, undefined).pipe(Effect.andThen(answer(peer, request)));
    });

    const forwarding = Layer.mock(PeerForwarding.PeerForwarding)({
      call: ((_scope: unknown, tool: { readonly name: string }, _env: unknown, params: never) =>
        tool.name === "t3_thread_launch"
          ? Effect.sync(() => {
              launched.push(params);
              return { threadId: ThreadId.make("thread:box-child"), runId: null, modelSelection };
            })
          : tool.name === "orchestrator_capabilities"
            ? Effect.sync(() => capabilityAsks++).pipe(
                Effect.andThen(Deferred.succeed(capabilitiesAsked, undefined)),
                Effect.andThen(
                  capabilities === "fails"
                    ? Effect.fail(
                        new OrchestratorMcpFailure({
                          code: "orchestration_error",
                          message: "box did not answer orchestrator_capabilities.",
                        }),
                      )
                    : "hold" in capabilities
                      ? Deferred.await(capabilities.hold).pipe(
                          Effect.as({ providers: capabilities.providers }),
                        )
                      : Effect.succeed({ providers: capabilities }),
                ),
              )
            : Effect.die(`unexpected call ${tool.name}`)) as never,
    });
    const threads = Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadEventSequence: () => Effect.succeed(0) as never,
      getThreadRecords: ((
        threadId: ThreadId,
        _parts: unknown,
        only?: { readonly subagentIds?: ReadonlyArray<NodeId> },
      ) =>
        // The follower's read of its task waits for good; this test is about the launch.
        only?.subagentIds !== undefined
          ? Effect.never
          : Effect.succeed({
              thread: {
                id: threadId,
                title: "Parent",
                projectId: ProjectId.make("project:here"),
                modelSelection,
                runtimeMode: "full-access",
                interactionMode: "default",
                lineage: {
                  parentThreadId: null,
                  relationshipToParent: null,
                  rootThreadId: threadId,
                },
              },
              runs: [
                {
                  id: RunId.make("run:parent"),
                  status: "running",
                  ordinal: 1,
                  rootNodeId: NodeId.make("node:parent-root"),
                  providerInstanceId: instanceId,
                },
              ],
              subagents: [{ id: taskId, remoteChild: { threadId: "thread:box-child" } }],
            })) as never,
      dispatch: () =>
        Effect.succeed({
          storedEvents: [{ event: { type: "subagent.updated", payload: { id: taskId } } }],
        } as never),
    });

    const remote = yield* Layer.build(
      RemoteDelegation.layer.pipe(
        Layer.provide(
          Layer.mergeAll(
            forwarding,
            threads,
            Layer.mock(PeerLinks.PeerLinks)({ get: () => Effect.succeedNone, resolve }),
            Layer.mock(ProjectionStore.ProjectionStoreV2)({}),
            Layer.mock(ProjectService.ProjectService)({}),
            // Upstream's launch reads the providers to check an agent's mode pick.
            Layer.mock(ProviderRegistry.ProviderRegistry)({ getProviders: Effect.succeed([]) }),
            Layer.mock(RepositoryIdentityResolver.RepositoryIdentityResolver)({}),
            Layer.mock(ServerEnvironment.ServerEnvironment)({
              getEnvironmentId: Effect.succeed("environment-here" as EnvironmentId),
            }),
            Layer.mock(ServerSettingsService.ServerSettingsService)({
              getSettings: Effect.succeed(fromFile(settings)),
            }),
            Layer.succeed(SecureRandom.SecureRandom, {
              uuidV4: Effect.succeed("00000000-0000-4000-8000-000000000000"),
            } as never),
            Layer.succeed(HttpClient.HttpClient, http),
          ),
        ),
      ),
    ).pipe(Effect.map(Context.get(RemoteDelegation.RemoteDelegation)));

    const { providerInstanceId: _named, ...unnamed } = input.target;
    const target = {
      ...unnamed,
      ...(instance === null ? {} : { providerInstanceId: ProviderInstanceId.make(instance) }),
      ...(driverKind === undefined ? {} : { driverKind: ProviderDriverKind.make(driverKind) }),
    };
    return {
      delegate: remote.delegate(scope, { ...input, target }),
      launched,
      asks,
      capabilityAsks: () => capabilityAsks,
      /** Done once the box's providers have been asked for. */
      capabilitiesAsked: Deferred.await(capabilitiesAsked),
      /** Done once the box has been asked. */
      asked: Deferred.await(asked),
      /** What the launch there carried as the parent's profile. */
      carried: () =>
        launched.map(
          (params) => (params.delegatedFrom as { readonly mcpProfile?: unknown }).mcpProfile,
        ),
    };
  });

const T3_ONLY = { mcpThreadProfiles: { [parentThreadId]: "t3-only" } };

const ASKED = ["https://box.test/api/fork/features Bearer link-token"];

it.effect("carries a t3-only parent's profile to a machine that applies it", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness(T3_ONLY, "carries");
      yield* harness.delegate;
      assert.deepStrictEqual(harness.carried(), [{ name: "t3-only", servers: [], t3Tools: "all" }]);
      // Asked once, at the address that answered as the box, with its link token.
      assert.deepStrictEqual(harness.asks, ASKED);
      // And once which driver the instance there runs.
      assert.strictEqual(harness.capabilityAsks(), 1);
    }),
  ),
);

it.effect.each<readonly [string, string, string]>([
  [
    "a Codex instance",
    "codex",
    "Provider instance codex on box uses driver codex, which does not apply",
  ],
  [
    "an instance it does not list",
    "gone",
    "box lists no provider instance gone, so it cannot be told",
  ],
])(
  "refuses, before any launch, a narrower profile to %s on a machine that applies it (#661)",
  ([, instance, says]) =>
    Effect.scoped(
      Effect.gen(function* () {
        const harness = yield* makeHarness(T3_ONLY, "carries", CLAUDE_THERE, instance);
        const exit = yield* Effect.exit(harness.delegate);
        assert.isTrue(Exit.isFailure(exit));
        assert.include(JSON.stringify(exit), "capability_denied");
        assert.include(JSON.stringify(exit), says);
        assert.include(JSON.stringify(exit), 'this thread\'s MCP profile (\\"t3-only\\")');
        assert.deepStrictEqual(harness.launched, []);
        // Run again: asked again (an instance there can change), refused again.
        const again = yield* Effect.exit(harness.delegate);
        assert.include(JSON.stringify(again), says);
        assert.strictEqual(harness.capabilityAsks(), 2);
        assert.deepStrictEqual(harness.launched, []);
      }),
    ),
);

it.effect(
  "a call naming a Claude driver and no instance asks the machine's providers once, and carries (#672)",
  () =>
    Effect.scoped(
      Effect.gen(function* () {
        const harness = yield* makeHarness(T3_ONLY, "carries", CLAUDE_THERE, null, "claudeAgent");
        yield* harness.delegate;
        assert.deepStrictEqual(harness.carried(), [
          { name: "t3-only", servers: [], t3Tools: "all" },
        ]);
        // Upstream's instance pick asked; the carry reads the driver it picked by.
        assert.strictEqual(harness.capabilityAsks(), 1);
        // Run again: one more ask, not two.
        yield* harness.delegate;
        assert.strictEqual(harness.capabilityAsks(), 2);
      }),
    ),
);

it.effect(
  "a call naming the Codex driver and no instance is refused, before any launch, after one ask (#672)",
  () =>
    Effect.scoped(
      Effect.gen(function* () {
        const harness = yield* makeHarness(T3_ONLY, "carries", CLAUDE_THERE, null, "codex");
        const exit = yield* Effect.exit(harness.delegate);
        assert.include(JSON.stringify(exit), "capability_denied");
        assert.include(
          JSON.stringify(exit),
          "Provider instance codex on box uses driver codex, which does not apply",
        );
        assert.strictEqual(harness.capabilityAsks(), 1);
        assert.deepStrictEqual(harness.launched, []);
      }),
    ),
);

it.effect(
  "a call naming a Claude driver and a listed Claude instance looks the instance up, and carries",
  () =>
    Effect.scoped(
      Effect.gen(function* () {
        const harness = yield* makeHarness(
          T3_ONLY,
          "carries",
          CLAUDE_THERE,
          "claude",
          "claudeAgent",
        );
        yield* harness.delegate;
        assert.deepStrictEqual(harness.carried(), [
          { name: "t3-only", servers: [], t3Tools: "all" },
        ]);
        // Upstream's check and the carry's lookup: a named instance is not taken on trust.
        assert.strictEqual(harness.capabilityAsks(), 2);
      }),
    ),
);

it.effect(
  "refuses, before any launch, an instance the machine does not list, named with a Claude driver",
  () =>
    Effect.scoped(
      Effect.gen(function* () {
        // Upstream passes a named instance on unchecked when that machine does not list it.
        const harness = yield* makeHarness(T3_ONLY, "carries", CLAUDE_THERE, "gone", "claudeAgent");
        const exit = yield* Effect.exit(harness.delegate);
        assert.isTrue(Exit.isFailure(exit));
        assert.include(JSON.stringify(exit), "capability_denied");
        assert.include(
          JSON.stringify(exit),
          "box lists no provider instance gone, so it cannot be told",
        );
        assert.deepStrictEqual(harness.launched, []);
        // Run again (the instance may have been added there since): asked again, still refused.
        const again = yield* Effect.exit(harness.delegate);
        assert.include(JSON.stringify(again), "capability_denied");
        assert.strictEqual(harness.capabilityAsks(), 4);
        assert.deepStrictEqual(harness.launched, []);
      }),
    ),
);

it.effect("a call naming a Claude driver for a Codex instance there is refused upstream", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness(T3_ONLY, "carries", CLAUDE_THERE, "codex", "claudeAgent");
      const exit = yield* Effect.exit(harness.delegate);
      // The carry trusts the named driver only because this check comes first.
      assert.include(
        JSON.stringify(exit),
        "Provider instance codex there uses driver codex, not claudeAgent.",
      );
      assert.strictEqual(harness.capabilityAsks(), 1);
      assert.deepStrictEqual(harness.launched, []);
    }),
  ),
);

it.effect("refuses a narrower profile to a Codex instance on a machine set to refuse (#661)", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness(T3_ONLY, "set to refuse", CLAUDE_THERE, "codex");
      const exit = yield* Effect.exit(harness.delegate);
      assert.include(JSON.stringify(exit), "capability_denied");
      assert.deepStrictEqual(harness.launched, []);
    }),
  ),
);

it.effect("sends all, which cuts nothing, to a Codex instance without asking for drivers", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness({}, "carries", CLAUDE_THERE, "codex");
      yield* harness.delegate;
      assert.deepStrictEqual(harness.carried(), [{ name: "all", servers: "all", t3Tools: "all" }]);
      assert.strictEqual(harness.capabilityAsks(), 0);
    }),
  ),
);

it.effect("refuses a narrower profile when the machine does not list its providers (#661)", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness(T3_ONLY, "carries", "fails");
      const exit = yield* Effect.exit(harness.delegate);
      assert.isTrue(Exit.isFailure(exit));
      assert.include(JSON.stringify(exit), "box did not answer orchestrator_capabilities.");
      assert.deepStrictEqual(harness.launched, []);
    }),
  ),
);

it.effect(
  "stopped while asking for the machine's providers, it launches nothing; run again, it carries",
  () =>
    Effect.scoped(
      Effect.gen(function* () {
        const hold = yield* Deferred.make<void>();
        const harness = yield* makeHarness(T3_ONLY, "carries", { hold, providers: CLAUDE_THERE });
        const call = yield* harness.delegate.pipe(Effect.forkChild);
        yield* harness.capabilitiesAsked;
        yield* Fiber.interrupt(call);
        assert.deepStrictEqual(harness.launched, []);
        yield* Deferred.succeed(hold, undefined);
        yield* harness.delegate;
        assert.deepStrictEqual(harness.carried(), [
          { name: "t3-only", servers: [], t3Tools: "all" },
        ]);
        assert.strictEqual(harness.capabilityAsks(), 2);
      }),
    ),
);

it.effect.each<readonly [string, Box]>([
  ["on an older T3 (its app page answers)", "older"],
  ["that answers 404", "refuses"],
])("refuses, before any launch, a narrower profile to a machine %s", ([, peer]) =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness(
        { mcpProfiles: { docs: { servers: ["docs"], t3Tools: "all" } }, mcpProfile: "docs" },
        peer,
      );
      const exit = yield* Effect.exit(harness.delegate);
      assert.isTrue(Exit.isFailure(exit));
      assert.include(JSON.stringify(exit), "capability_denied");
      assert.include(
        JSON.stringify(exit),
        'does not apply this thread\'s MCP profile (\\"docs\\")',
      );
      assert.deepStrictEqual(harness.asks, ASKED);
      assert.deepStrictEqual(harness.launched, []);
    }),
  ),
);

it.effect(
  "refuses, before any launch, a narrower profile to a machine set to refuse it (#640)",
  () =>
    Effect.scoped(
      Effect.gen(function* () {
        const harness = yield* makeHarness(T3_ONLY, "set to refuse");
        const exit = yield* Effect.exit(harness.delegate);
        assert.include(JSON.stringify(exit), "capability_denied");
        assert.include(
          JSON.stringify(exit),
          "box does not take a task that carries a narrower MCP profile now",
        );
        assert.include(
          JSON.stringify(exit),
          'have the owner set mcpCarriedProfiles to \\"apply\\" on box',
        );
        assert.deepStrictEqual(harness.asks, ASKED);
        assert.deepStrictEqual(harness.launched, []);
        // Run again: asked again, refused again; nothing was launched in between.
        yield* Effect.exit(harness.delegate);
        assert.deepStrictEqual(harness.asks, [...ASKED, ...ASKED]);
        assert.deepStrictEqual(harness.launched, []);
      }),
    ),
);

it.effect("sends all, which cuts nothing, to a machine set to refuse without asking it", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness({}, "set to refuse");
      yield* harness.delegate;
      assert.deepStrictEqual(harness.carried(), [{ name: "all", servers: "all", t3Tools: "all" }]);
      assert.deepStrictEqual(harness.asks, []);
    }),
  ),
);

it.effect("refuses a narrower profile when the machine is not linked", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness(T3_ONLY, "unlinked");
      const exit = yield* Effect.exit(harness.delegate);
      assert.include(JSON.stringify(exit), "box did not answer.");
      assert.deepStrictEqual(harness.asks, []);
      assert.deepStrictEqual(harness.launched, []);
    }),
  ),
);

it.effect("refuses a narrower profile when the machine cannot be reached", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness(T3_ONLY, "down");
      const exit = yield* Effect.exit(harness.delegate);
      assert.include(JSON.stringify(exit), "orchestration_error");
      assert.include(JSON.stringify(exit), "box did not answer whether it applies");
      assert.deepStrictEqual(harness.launched, []);
    }),
  ),
);

it.effect.each<readonly [string, Box]>([
  ["does not answer in time", "never"],
  // One deadline covers the whole answer: headers in time do not leave the body unbounded.
  ["sends its headers and then stalls the body", "stalls"],
])("refuses a narrower profile when the machine %s", ([, peer]) =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness(T3_ONLY, peer);
      const call = yield* harness.delegate.pipe(Effect.exit, Effect.forkChild);
      // The box has been asked (and a stalling one has sent its headers) before time moves on.
      yield* harness.asked;
      yield* Effect.yieldNow;
      yield* TestClock.adjust(PeerLinks.probeTimeout("https://box.test"));
      const exit = yield* Fiber.join(call);
      assert.include(JSON.stringify(exit), "box did not answer whether it applies");
      assert.deepStrictEqual(harness.launched, []);
    }),
  ),
);

it.effect.each<readonly [string, Record<string, unknown>]>([
  ["no settings file (no keys)", {}],
  ["profiles off, the defaults written out", { mcpProfile: "all", mcpSubagentProfile: "inherit" }],
])("%s: carries all, and sends to an older machine without asking it", ([, settings]) =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness(settings, "older");
      yield* harness.delegate;
      assert.deepStrictEqual(harness.carried(), [{ name: "all", servers: "all", t3Tools: "all" }]);
      assert.deepStrictEqual(harness.asks, []);
    }),
  ),
);

it.effect("refuses when the parent's own profile cannot be worked out", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness({ mcpProfile: "gone" }, "carries");
      const exit = yield* Effect.exit(harness.delegate);
      assert.include(JSON.stringify(exit), "invalid_request");
      assert.include(JSON.stringify(exit), 'MCP profile \\"gone\\" is not set up');
      assert.deepStrictEqual(harness.launched, []);
    }),
  ),
);

it.effect(
  "stopped while asking the machine, it launches nothing; run again, it carries the same",
  () =>
    Effect.scoped(
      Effect.gen(function* () {
        const hold = yield* Deferred.make<void>();
        const harness = yield* makeHarness(T3_ONLY, { hold });
        const call = yield* harness.delegate.pipe(Effect.forkChild);
        // Stopped while the box is being asked.
        yield* harness.asked;
        yield* Fiber.interrupt(call);
        assert.deepStrictEqual(harness.launched, []);
        yield* Deferred.succeed(hold, undefined);
        yield* harness.delegate;
        yield* harness.delegate;
        const [first, second] = harness.carried();
        assert.deepStrictEqual(first, { name: "t3-only", servers: [], t3Tools: "all" });
        assert.deepStrictEqual(second, first);
        // Each run asks again: an answer is not kept, so an update or a downgrade there counts.
        assert.strictEqual(harness.asks.length, 3);
      }),
    ),
);

it("the receiving tool keeps the carried profile whole, and the receiver reads it back", () => {
  const carried = { name: "t3-only", servers: [], t3Tools: "all" };
  const from = { environmentId: "environment-here", threadId: "thread:parent", title: "P" };
  // From an older sender: nothing carried.
  assert.isUndefined(
    launch({ projectId: "project:box", title: "Task", delegatedFrom: from }).delegatedFrom
      ?.mcpProfile,
  );
  // A field a newer sender adds reaches the resolver, which refuses it rather than dropping it.
  const newer = launch({
    projectId: "project:box",
    title: "Task",
    delegatedFrom: { ...from, mcpProfile: { ...carried, newer: true } },
  }).delegatedFrom?.mcpProfile;
  assert.deepStrictEqual(newer, { ...carried, newer: true });
  assert.deepStrictEqual(decodeDelegatedFrom({ ...from, mcpProfile: carried }).mcpProfile, carried);
  const settings = fromFile({});
  const lineage = [{ threadId: "task", delegated: true }];
  assert.strictEqual(resolveMcpProfile(settings, lineage, newer)._tag, "CannotApply");
  const resolved = resolveMcpProfile(settings, lineage, carried);
  assert.deepStrictEqual(resolved._tag === "Resolved" && resolved.profile.servers, []);
});

it("agents are not shown the carried profile; a machine that does not know a field drops it", () => {
  const advertised = advertisedJsonSchema(ProjectToolkit.tools.t3_thread_launch) as {
    readonly properties: { readonly delegatedFrom: { readonly properties: object } };
  };
  assert.deepStrictEqual(Object.keys(advertised.properties.delegatedFrom.properties).toSorted(), [
    "environmentId",
    "threadId",
    "title",
  ]);
  // How the tool decodes what a linked machine sends: a field it does not know (as `mcpProfile` is
  // to a machine on an older T3) is dropped, not refused, so a launch with `all` still goes there.
  const sent = launchAsSent({
    projectId: "project:box",
    title: "Task",
    delegatedFrom: {
      environmentId: "environment-here",
      threadId: "thread:parent",
      title: "P",
      mcpProfile: { name: "all", servers: "all", t3Tools: "all" },
      newer: true,
    },
  });
  assert.deepStrictEqual(Object.keys(sent.delegatedFrom ?? {}).toSorted(), [
    "environmentId",
    "mcpProfile",
    "threadId",
    "title",
  ]);
});
