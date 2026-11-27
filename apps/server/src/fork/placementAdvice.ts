/**
 * Fork (placement P6, `advise`): where a `delegate_task` worktree child would run, as text for the
 * reply; it places nothing. The seat is not given `isolation` (P7 passes it). `packages/shared` has
 * no `./forkPlacement` export, so the engine is imported by relative path.
 */
import {
  type EnvironmentId,
  OrchestratorMcpCapabilitiesInput,
  OrchestratorMcpCapabilitiesResult,
  OrchestratorMcpFailure,
  type ProviderDriverKind,
  type RuntimeMode,
  type ServerSettings,
} from "@t3tools/contracts";
import { resolveProjectSettings } from "@t3tools/shared/projectSettings";
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import * as Tool from "effect/ai/Tool";

import {
  choosePlacement,
  type MachineVerdict,
  type PlacementMachine,
  type PlacementResult,
  type PlacementWorkspace,
} from "../../../../packages/shared/src/forkPlacement.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import { ProjectToolkit } from "../mcp/toolkits/project/tools.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as RepositoryIdentityResolver from "../project/RepositoryIdentityResolver.ts";
import * as EnvironmentFacts from "./environmentFacts.ts";
import { readPlacementForThisOs } from "./placementRead.ts";

/** How long a linked machine's facts read and its workspace lookups may take (design 4.5). */
export const LINKED_READ_TIMEOUT = Duration.seconds(3);
/**
 * The whole advice, this machine's own read included: past it the reply says so and the task goes
 * on. The linked reads end at {@link LINKED_READ_TIMEOUT}; this bounds the rest.
 */
export const ADVICE_TIMEOUT = Duration.seconds(5);
/** At most this many pages of a linked machine's project list (100 each) are read. */
const PROJECT_PAGES_MAX = 10;

export const NOTE_PREFIX = "Placement (advise; nothing was moved):";
export const NOTE_FAILED = `${NOTE_PREFIX} the advice could not be worked out.`;
export const NOTE_TIMED_OUT = `${NOTE_PREFIX} the advice took longer than ${Duration.format(ADVICE_TIMEOUT)}, so none is given.`;
export const NOTE_NO_CALLER =
  "this call has no MCP caller, so no linked machine was asked; only this machine was checked.";
export const NOTE_LINKS_OFF =
  "Linked environments is off here, so no linked machine was asked; only this machine was checked.";
export const NOTE_LINKS_MISSING =
  "This server has no link services, so no linked machine was asked; only this machine was checked.";
export const NOTE_SHARED_STAYS =
  "A shared child (no isolation) stays here: it works in this checkout.";
export const NOTE_ENFORCE_NOT_BUILT = "placement.mode is enforce, which is not built yet:";

/** `t3_environment_read` there; its answer is decoded by `factsFromAnswer`, not by this schema. */
const FactsReadTool = Tool.make("t3_environment_read", {
  parameters: Schema.Struct({}),
  success: Schema.Unknown,
  failure: OrchestratorMcpFailure,
  failureMode: "return",
});

/** `orchestrator_capabilities` there, as `peer/RemoteDelegation.ts` describes it. */
const CapabilitiesTool = Tool.make("orchestrator_capabilities", {
  parameters: OrchestratorMcpCapabilitiesInput,
  success: OrchestratorMcpCapabilitiesResult,
  failure: OrchestratorMcpFailure,
  failureMode: "return",
});

export interface AdviceInput {
  readonly settings: ServerSettings;
  readonly projectId: Parameters<typeof resolveProjectSettings>[1];
  /** The child's runtime mode, `inherit` resolved. */
  readonly runtimeMode: RuntimeMode;
  /** The driver and model the task runs on here (H7 there: the same driver and model). */
  readonly driverKind: ProviderDriverKind | undefined;
  readonly model: string;
}

/** A machine's name in the text: a link's label, or "this machine". */
type Names = ReadonlyMap<string, string>;

const nameOf = (names: Names, environmentId: string) => names.get(environmentId) ?? environmentId;

const verdictText = (names: Names, verdict: MachineVerdict) =>
  `${nameOf(names, verdict.environmentId)}: ${
    verdict.failures.length > 0
      ? verdict.failures.map((failure) => `${failure.rule} ${failure.reason}`).join(" ")
      : (verdict.unranked ?? "fits")
  }`;

/**
 * The reply's text for an engine result, or none for `off`. Pure, so the wording is tested alone.
 * `asked` says which machines were asked when not all could be.
 */
export const adviceText = (
  result: PlacementResult,
  names: Names,
  asked?: string,
): string | undefined => {
  if (result.kind === "off") return undefined;
  const mode = result.mode === "enforce" ? `${NOTE_ENFORCE_NOT_BUILT} ` : "";
  const scope = asked === undefined ? "" : ` ${asked}`;
  if (result.kind === "none") {
    return `${NOTE_PREFIX} ${mode}no machine fits a worktree child, so placing would refuse it. ${result.reason}${scope} ${NOTE_SHARED_STAYS}`;
  }
  const best = result.verdicts.find((verdict) => verdict.environmentId === result.environmentId);
  const where = best?.here === true ? "here" : `on ${nameOf(names, result.environmentId)}`;
  const others = result.verdicts.filter(
    (verdict) => verdict.environmentId !== result.environmentId,
  );
  const order =
    result.order.length > 1
      ? ` Order: ${result.order.map((id) => nameOf(names, id)).join(", ")}.`
      : "";
  const left = others.filter((verdict) => verdict.rank === null);
  // The engine's reasons end in a full stop; the list ends in one.
  const leftOut =
    left.length > 0
      ? ` Left out: ${left
          .map((v) => verdictText(names, v))
          .join("; ")
          .replace(/\.$/u, "")}.`
      : "";
  return `${NOTE_PREFIX} ${mode}a worktree child would run ${where}. ${result.why}${order}${leftOut}${scope} ${NOTE_SHARED_STAYS}`;
};

/**
 * Builds the advice inside the delegation checks. The link and project services are read if the
 * server has them (the MCP server's layer does, `server.ts`); without them only this machine is
 * checked, and the text says so.
 */
export const make = Effect.fnUntraced(function* () {
  const links = yield* Effect.serviceOption(PeerLinks.PeerLinks);
  const forwarding = yield* Effect.serviceOption(PeerForwarding.PeerForwarding);
  const projects = yield* Effect.serviceOption(ProjectService.ProjectService);
  const identities = yield* Effect.serviceOption(
    RepositoryIdentityResolver.RepositoryIdentityResolver,
  );

  /** This project's repository key, as `RemoteDelegation.resolveRemoteProject` matches it. */
  const repositoryKey = Effect.fn("PlacementAdvice.repositoryKey")(function* (
    projectId: AdviceInput["projectId"],
  ) {
    if (projectId === null || Option.isNone(projects) || Option.isNone(identities)) {
      return undefined;
    }
    const project = yield* projects.value.getById(projectId).pipe(
      Effect.map(Option.getOrUndefined),
      Effect.orElseSucceed(() => undefined),
    );
    if (project === undefined) return undefined;
    return (yield* identities.value.resolve(project.workspaceRoot))?.canonicalKey;
  });

  /** H7 there: checkouts of this repository, and the driver and model, or `null` (not checked). */
  const workspaceThere = Effect.fn("PlacementAdvice.workspaceThere")(function* (
    forward: PeerForwarding.PeerForwarding["Service"],
    scope: McpInvocationContext.McpInvocationScope,
    environmentId: EnvironmentId,
    key: string | undefined,
    input: AdviceInput,
  ): Effect.fn.Return<PlacementWorkspace | null, OrchestratorMcpFailure> {
    if (key === undefined || input.driverKind === undefined) return null;
    let matchingCheckouts = 0;
    let cursor: number | null = 0;
    for (let page = 0; cursor !== null; page++) {
      if (page === PROJECT_PAGES_MAX) return null;
      const listed: Tool.Success<typeof ProjectToolkit.tools.t3_project_list> = yield* forward.call(
        scope,
        ProjectToolkit.tools.t3_project_list,
        environmentId,
        {
          cursor,
          limit: 100,
        },
      );
      matchingCheckouts += listed.projects.filter(
        (project) => project.repositoryIdentity?.canonicalKey === key,
      ).length;
      cursor = listed.nextCursor;
    }
    const { providers } = yield* forward.call(scope, CapabilitiesTool, environmentId, {
      search: input.model,
    });
    const able = providers.filter(
      (provider) => provider.driverKind === input.driverKind && provider.canRunChildTask,
    );
    const model = input.model.toLowerCase();
    return {
      matchingCheckouts,
      providerPresent: able.length > 0,
      modelPresent: able.some((provider) =>
        provider.models.some((candidate) => candidate.id.toLowerCase() === model),
      ),
    };
  });

  /** One linked machine: its stored link, then its facts and workspace, each in its own window. */
  const linkedMachine = Effect.fn("PlacementAdvice.linkedMachine")(function* (
    linkService: PeerLinks.PeerLinks["Service"],
    forward: PeerForwarding.PeerForwarding["Service"],
    scope: McpInvocationContext.McpInvocationScope,
    environmentId: EnvironmentId,
    key: string | undefined,
    input: AdviceInput,
  ) {
    const weights = (yield* readPlacementForThisOs(input.settings)).weights;
    const weight = weights[environmentId];
    const stored = yield* linkService.get(environmentId).pipe(Effect.orElseSucceed(Option.none));
    const base = {
      environmentId,
      here: false,
      ...(weight === undefined ? {} : { weight }),
    } as const;
    if (Option.isNone(stored)) {
      return { ...base, link: null, facts: null, workspace: null } satisfies PlacementMachine;
    }
    const window = <A, E>(effect: Effect.Effect<A, E>) =>
      effect.pipe(
        Effect.timeoutOption(LINKED_READ_TIMEOUT),
        Effect.map(Option.getOrUndefined),
        Effect.catchCause(() => Effect.undefined),
      );
    const [answer, workspace] = yield* Effect.all(
      [
        window(
          forward
            .call(scope, FactsReadTool, environmentId, {})
            .pipe(
              Effect.flatMap((value) =>
                Effect.map(Clock.currentTimeMillis, (receivedAt) => ({ value, receivedAt })),
              ),
            ),
        ),
        window(workspaceThere(forward, scope, environmentId, key, input)),
      ],
      { concurrency: 2 },
    );
    const answered = answer !== undefined;
    return {
      ...base,
      link: {
        reachable: answered,
        expiresAt: DateTime.toEpochMillis(stored.value.expiresAt),
        access: stored.value.access,
        // A forwarded call needs the beta on here (checked before) and there (refused otherwise).
        betaHere: true,
        betaThere: answered,
      },
      facts: answered ? EnvironmentFacts.factsFromAnswer(answer.value, answer.receivedAt) : null,
      workspace: workspace ?? null,
    } satisfies PlacementMachine;
  });

  /**
   * The text for the reply, or none while placement is off. Never fails: a failure or a defect
   * reads as {@link NOTE_FAILED}, a slow read as {@link NOTE_TIMED_OUT}, both logged.
   */
  const advise = Effect.fn("PlacementAdvice.advise")(
    function* (input: AdviceInput): Effect.fn.Return<string | undefined> {
      const placement = yield* readPlacementForThisOs(input.settings);
      if (placement.mode === "off") return undefined;
      const scope = yield* Effect.serviceOption(McpInvocationContext.McpInvocationContext);
      const hereId = Option.isSome(scope) ? scope.value.environmentId : "this-machine";
      const names = new Map<string, string>([[hereId, `this machine (${hereId})`]]);
      const local = yield* EnvironmentFacts.read;
      const hereWeight = Option.isSome(scope)
        ? placement.weights[scope.value.environmentId]
        : undefined;
      const here: PlacementMachine = {
        environmentId: hereId,
        here: true,
        link: null,
        facts: { ...local, receivedAt: yield* Clock.currentTimeMillis },
        // The task's own checkout, and the target the lookup resolved here.
        workspace: { matchingCheckouts: 1, providerPresent: true, modelPresent: true },
        ...(hereWeight === undefined ? {} : { weight: hereWeight }),
      };
      let asked: string | undefined;
      let linked: ReadonlyArray<PlacementMachine> = [];
      if (Option.isNone(scope)) asked = NOTE_NO_CALLER;
      else if (Option.isNone(links) || Option.isNone(forwarding)) asked = NOTE_LINKS_MISSING;
      else if (scope.value.linkedEnvironments === false || !(yield* links.value.enabled)) {
        asked = NOTE_LINKS_OFF;
      } else {
        const linkService = links.value;
        const forward = forwarding.value;
        const callScope = scope.value;
        const stored = yield* linkService.names.pipe(Effect.orElseSucceed(() => []));
        for (const link of stored)
          names.set(link.environmentId, `${link.label} (${link.environmentId})`);
        const key = yield* repositoryKey(input.projectId);
        linked = yield* Effect.forEach(
          stored,
          (link) => linkedMachine(linkService, forward, callScope, link.environmentId, key, input),
          { concurrency: "unbounded" },
        );
      }
      const result = choosePlacement({
        mode: placement.mode,
        task: {
          runtimeMode: input.runtimeMode,
          needs: resolveProjectSettings(input.settings, input.projectId).settings.placementNeeds,
          never: placement.never,
          // Nothing is placed before `enforce` (P7), so no child was placed.
          fromPlacedChild: false,
        },
        machines: [here, ...linked],
        now: yield* Clock.currentTimeMillis,
      });
      yield* Effect.logInfo("placement advice").pipe(
        Effect.annotateLogs({
          result: result.kind,
          ...(result.kind === "picked" ? { pick: result.environmentId, order: result.order } : {}),
          ...(result.kind === "none" ? { reason: result.reason } : {}),
          ...(result.kind === "off" ? {} : { verdicts: result.verdicts }),
          ...(asked === undefined ? {} : { asked }),
        }),
      );
      return adviceText(result, names, asked);
    },
    Effect.timeoutOption(ADVICE_TIMEOUT),
    Effect.flatMap(
      Option.match({
        onNone: () =>
          Effect.logWarning("placement advice timed out").pipe(Effect.as(NOTE_TIMED_OUT)),
        onSome: Effect.succeed,
      }),
    ),
    Effect.catchCause((cause) =>
      Effect.logWarning("placement advice failed", cause).pipe(Effect.as(NOTE_FAILED)),
    ),
  );

  return { advise };
});
