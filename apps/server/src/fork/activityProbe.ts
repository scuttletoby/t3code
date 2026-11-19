/**
 * Fork: a read-only "is T3 busy?" answer for programs on this machine (a go-live quiet check), counted
 * as the automatic updater counts. No sign-in, so only a loopback caller with no proxy or browser
 * headers is answered; every other caller gets a plain 404 that does not say the route exists.
 * Off (`server.activityProbe`, the default): a local caller gets 404 with a reason, others the plain one.
 */
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import type * as Option from "effect/Option";
import * as Ref from "effect/Ref";
import type * as Scope from "effect/Scope";
import { HttpRouter, HttpServerRequest, HttpServerResponse } from "effect/http";

import packageJson from "../../package.json" with { type: "json" };
import {
  type AutoUpdateActivity,
  activityReason,
  makeActivityCounter,
} from "../cloud/autoUpdate.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as TerminalManager from "../terminal/Manager.ts";

export const ACTIVITY_PROBE_PATH = "/api/fork/activity";
/** Bumped when a field changes meaning; a reader that does not know the version says "unknown". */
export const ACTIVITY_PROBE_VERSION = 1;

/**
 * There is no "starting": upstream holds every HTTP request until the server accepts commands
 * (server.ts, `layerCommandReadiness`), so a server that is starting does not answer at all.
 */
export type ActivityProbeServerState = "running" | "stopping";

export interface ActivityProbeAnswer {
  readonly probeVersion: typeof ACTIVITY_PROBE_VERSION;
  /** True only while running with nothing working. */
  readonly quiet: boolean;
  /** Why it is not quiet, or null when it is. */
  readonly reason: string | null;
  readonly state: ActivityProbeServerState;
  /** This server's process: a reader checks it against the home's `server-runtime.json`. */
  readonly pid: number;
  readonly serverVersion: string;
  /** The counts, or null when they were not read (stopping, or thread state unreadable). */
  readonly activity: AutoUpdateActivity | null;
}

export function activityProbeAnswer(input: {
  readonly pid: number;
  readonly serverVersion: string;
  readonly state: ActivityProbeServerState;
  readonly activity: AutoUpdateActivity | null;
}): ActivityProbeAnswer {
  const activity = input.state === "running" ? input.activity : null;
  const reason =
    input.state === "stopping"
      ? "T3 is stopping"
      : activity === null
        ? "thread state could not be read"
        : activityReason(activity);
  return {
    probeVersion: ACTIVITY_PROBE_VERSION,
    quiet: reason === null,
    reason,
    state: input.state,
    pid: input.pid,
    serverVersion: input.serverVersion,
    activity,
  };
}

const isLoopbackAddress = (address: string): boolean => {
  const bare = address.startsWith("::ffff:") ? address.slice("::ffff:".length) : address;
  return /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(bare) || bare === "::1";
};

/** A Host header naming this machine's loopback (any port); a missing one is a non-browser client. */
const isLoopbackHost = (host: string | undefined): boolean => {
  if (host === undefined) return true;
  const name = host.startsWith("[") ? host.slice(1, host.indexOf("]")) : host.replace(/:\d+$/, "");
  return name.toLowerCase() === "localhost" || isLoopbackAddress(name);
};

/**
 * Headers a local proxy adds for a caller it forwards: Tailscale Serve, cloudflared and SSH or
 * reverse proxies connect from loopback on behalf of other machines.
 */
const PROXY_HEADERS = [
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-proto",
  "x-forwarded-port",
  "forwarded",
  "x-real-ip",
  "true-client-ip",
  "cf-connecting-ip",
  "tailscale-user-login",
  "via",
] as const;

/**
 * A browser page's request: an Origin, or fetch metadata. Browsers send `sec-fetch-site` and
 * `sec-fetch-dest` on every request to loopback, including no-cors ones such as `<img src>` that
 * carry no Origin. Node's own `fetch` (undici, which the reader uses) sends `sec-fetch-mode: cors`
 * and neither of the others, so that one value alone is a program.
 */
const isBrowserRequest = (headers: Readonly<Record<string, string | undefined>>): boolean =>
  headers.origin !== undefined ||
  headers["sec-fetch-site"] !== undefined ||
  headers["sec-fetch-dest"] !== undefined ||
  (headers["sec-fetch-mode"] !== undefined && headers["sec-fetch-mode"] !== "cors");

/**
 * Only a program on this machine: a loopback peer, no proxy's headers, no browser page's.
 * A TCP-level forward to loopback (`ssh -L`/`-R`, `tailscale serve --tcp`, socat, editor port
 * forwarding) adds no headers and lets its client choose the Host, so it looks local; whoever can
 * open such a forward can already reach this machine's loopback.
 */
export function isLocalProbeRequest(
  peerAddress: Option.Option<string>,
  headers: Readonly<Record<string, string | undefined>>,
): boolean {
  if (peerAddress._tag === "None" || !isLoopbackAddress(peerAddress.value)) return false;
  if (PROXY_HEADERS.some((header) => headers[header] !== undefined)) return false;
  if (isBrowserRequest(headers)) return false;
  return isLoopbackHost(headers.host);
}

/** What any caller that is not local gets, on or off: upstream's plain "no such route". */
const notFound = () => HttpServerResponse.text("Not Found", { status: 404 });

/** "running" while the routes are up, "stopping" once their scope closes (the server shuts down). */
export const trackServerState: Effect.Effect<
  Effect.Effect<ActivityProbeServerState>,
  never,
  Scope.Scope
> = Effect.gen(function* () {
  const state = yield* Ref.make<ActivityProbeServerState>("running");
  yield* Effect.addFinalizer(() => Ref.set(state, "stopping"));
  return Ref.get(state);
});

const json = (body: unknown, status: number) =>
  HttpServerResponse.jsonUnsafe(body, { status, headers: { "cache-control": "no-store" } });

/**
 * The route's work, with the setting, state and counter passed in. Reads nothing it refuses.
 * The caller is checked first, so a remote caller cannot learn that this is the fork or how the
 * setting is set: it always gets the plain 404.
 */
export const handleActivityProbe = Effect.fn("fork.activityProbe.handle")(function* <E>(deps: {
  readonly pid: number;
  readonly serverVersion: string;
  readonly enabled: Effect.Effect<boolean>;
  readonly state: Effect.Effect<ActivityProbeServerState>;
  readonly activity: Effect.Effect<AutoUpdateActivity, E>;
}) {
  const request = yield* HttpServerRequest.HttpServerRequest;
  if (!isLocalProbeRequest(request.peerAddress, request.headers)) return notFound();
  if (!(yield* deps.enabled)) {
    return json({ error: "activity probe is off (server.activityProbe)" }, 404);
  }
  const state = yield* deps.state;
  const activity =
    state === "running"
      ? yield* deps.activity.pipe(
          Effect.catch((error) =>
            Effect.logWarning("activity probe could not read thread state", { error }).pipe(
              Effect.as(null),
            ),
          ),
        )
      : null;
  return json(
    activityProbeAnswer({ pid: deps.pid, serverVersion: deps.serverVersion, state, activity }),
    200,
  );
});

/**
 * `GET /api/fork/activity`; the setting is read on each request, so switching it needs no restart.
 * Takes the projection store from outside; `layer` below gives it the server's.
 */
export const layerRoute = Layer.unwrap(
  Effect.gen(function* () {
    const settings = yield* ServerSettings.ServerSettingsService;
    const projections = yield* ProjectionStore.ProjectionStoreV2;
    const terminals = yield* TerminalManager.TerminalManager;
    const state = yield* trackServerState;
    const activity = yield* makeActivityCounter({ projections, terminals });
    const enabled = settings.getSettings.pipe(
      Effect.map((current) => current.activityProbe),
      Effect.orElseSucceed(() => false),
    );
    return HttpRouter.add(
      "GET",
      ACTIVITY_PROBE_PATH,
      handleActivityProbe({
        pid: process.pid,
        serverVersion: packageJson.version,
        enabled,
        state,
        activity,
      }),
    );
  }),
);

export const layer = layerRoute.pipe(Layer.provide(ProjectionStore.layer));
