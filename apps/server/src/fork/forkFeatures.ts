/**
 * Fork: `GET /api/fork/features`, what this machine does for a linked machine (as its settings
 * allow), and the reader a linked machine asks it with. Only a linked machine's own token gets an
 * answer; any other caller gets a plain 404 (which, unlike upstream's app page, shows the fork).
 */
import { MCP_CARRIED_PROFILES_REFUSE } from "@t3tools/contracts";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import {
  FetchHttpClient,
  HttpClient,
  HttpClientRequest,
  HttpRouter,
  HttpServerRequest,
  HttpServerResponse,
} from "effect/http";

import * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import * as ServerConfig from "../config.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import { makeMcpProfileSettings } from "./claudeMcpProfile.ts";

export const FORK_FEATURES_PATH = "/api/fork/features";

/**
 * The answer. A reader counts a feature only when it is `true`. `mcpProfileCarry: false` says this
 * version could, but its owner set `mcpCarriedProfiles` to `refuse` (or its settings cannot be read
 * now); absent, the version cannot.
 */
export const ForkFeatures = Schema.Struct({ mcpProfileCarry: Schema.optionalKey(Schema.Boolean) });
export type ForkFeatures = typeof ForkFeatures.Type;
const decodeForkFeatures = Schema.decodeUnknownOption(ForkFeatures);

/**
 * What this machine answers, from the settings a Claude turn reads (fork/claudeMcpProfile.ts), so
 * the route and the turn agree: `false` while `mcpCarriedProfiles` reads `refuse` (also for a
 * settings.json that does not decode, which may say so) or the settings cannot be read at all.
 * Each task's turn there reads it again and refuses a task it would not take (fork/mcpProfile.ts).
 */
const thisMachine = (readSettings: Effect.Success<typeof makeMcpProfileSettings>) =>
  readSettings.pipe(
    Effect.map((read): ForkFeatures => ({
      mcpProfileCarry: read.mcpCarriedProfiles !== MCP_CARRIED_PROFILES_REFUSE,
    })),
    Effect.orElseSucceed((): ForkFeatures => ({ mcpProfileCarry: false })),
  );

const notFound = () => HttpServerResponse.text("Not Found", { status: 404 });

/** The route's work with the server's auth passed in. A browser (it sends an Origin) is refused. */
const handleForkFeatures = Effect.fn("fork.features.handle")(function* (
  auth: Pick<EnvironmentAuth.EnvironmentAuth["Service"], "authenticateMcpClient">,
  readSettings: Effect.Success<typeof makeMcpProfileSettings>,
) {
  const request = yield* HttpServerRequest.HttpServerRequest;
  if (request.headers.origin !== undefined) return notFound();
  const client = yield* auth.authenticateMcpClient(request).pipe(Effect.option);
  if (Option.isNone(client) || !client.value.peerLink) return notFound();
  return HttpServerResponse.jsonUnsafe(yield* thisMachine(readSettings), {
    headers: { "cache-control": "no-store" },
  });
});

/** `GET /api/fork/features` in the server's routes (fork/forkLayers.ts). */
export const layer = Layer.unwrap(
  Effect.gen(function* () {
    const auth = yield* EnvironmentAuth.EnvironmentAuth;
    // Asked for here, so a server wired without one is a type error, not a route that silently
    // skips settings.json: the reader takes each only if it is there.
    yield* ServerSettingsService.ServerSettingsService;
    yield* ServerConfig.ServerConfig;
    yield* FileSystem.FileSystem;
    yield* ManagedPolicy.ManagedPolicy;
    const readSettings = yield* makeMcpProfileSettings;
    return HttpRouter.add("GET", FORK_FEATURES_PATH, handleForkFeatures(auth, readSettings));
  }),
);

/** The linked machine at `url` did not answer in time, or the request could not be sent. */
export class ForkFeaturesUnanswered extends Schema.TaggedError<ForkFeaturesUnanswered>()(
  "ForkFeaturesUnanswered",
  { message: Schema.String },
) {}

/**
 * Asks the linked machine at `url` (the address that answered as it) with its link `token`. Any
 * answer but this route's JSON, such as an older version's app page or a 404, reads as no features.
 */
export const readForkFeatures = Effect.fn("fork.features.read")(function* (
  http: HttpClient.HttpClient,
  peer: { readonly url: string; readonly token: string; readonly timeout: Duration.Input },
) {
  // The token goes only to this address: a redirect could forward it anywhere.
  const noRedirects = HttpClient.transform(http, (response) =>
    Effect.provideService(response, FetchHttpClient.RequestInit, { redirect: "manual" }),
  );
  // One deadline for the whole answer, its body too: a machine that sends the headers and then
  // stalls must not hold the delegation open.
  return yield* Effect.gen(function* () {
    const response = yield* noRedirects.execute(
      HttpClientRequest.get(`${peer.url}${FORK_FEATURES_PATH}`).pipe(
        HttpClientRequest.bearerToken(peer.token),
        HttpClientRequest.acceptJson,
      ),
    );
    if (response.status !== 200) return {} satisfies ForkFeatures;
    const body = yield* response.json.pipe(Effect.option);
    return Option.getOrElse(Option.flatMap(body, decodeForkFeatures), () => ({}) as ForkFeatures);
  }).pipe(
    Effect.timeout(peer.timeout),
    Effect.mapError(() => new ForkFeaturesUnanswered({ message: `${peer.url} did not answer.` })),
  );
});
