/**
 * Fork: `GET /api/fork/features`, what this fork version does for a linked machine, and the reader a
 * linked machine asks it with. Answered only to a linked machine's own token; any other caller gets
 * upstream's plain 404, so it learns nothing, not even that this is the fork.
 */
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
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

export const FORK_FEATURES_PATH = "/api/fork/features";

/** The answer. A reader counts a feature only when it is `true`; anything else is "not this one". */
export const ForkFeatures = Schema.Struct({ mcpProfileCarry: Schema.optionalKey(Schema.Boolean) });
export type ForkFeatures = typeof ForkFeatures.Type;
const decodeForkFeatures = Schema.decodeUnknownOption(ForkFeatures);

/** What this version answers. */
const THIS_VERSION: ForkFeatures = { mcpProfileCarry: true };

const notFound = () => HttpServerResponse.text("Not Found", { status: 404 });

/** The route's work with the server's auth passed in. A browser (it sends an Origin) is refused. */
const handleForkFeatures = Effect.fn("fork.features.handle")(function* (
  auth: Pick<EnvironmentAuth.EnvironmentAuth["Service"], "authenticateMcpClient">,
) {
  const request = yield* HttpServerRequest.HttpServerRequest;
  if (request.headers.origin !== undefined) return notFound();
  const client = yield* auth.authenticateMcpClient(request).pipe(Effect.option);
  if (Option.isNone(client) || !client.value.peerLink) return notFound();
  return HttpServerResponse.jsonUnsafe(THIS_VERSION, { headers: { "cache-control": "no-store" } });
});

/** `GET /api/fork/features` in the server's routes (fork/forkLayers.ts). */
export const layer = Layer.unwrap(
  Effect.gen(function* () {
    const auth = yield* EnvironmentAuth.EnvironmentAuth;
    return HttpRouter.add("GET", FORK_FEATURES_PATH, handleForkFeatures(auth));
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
  const response = yield* noRedirects
    .execute(
      HttpClientRequest.get(`${peer.url}${FORK_FEATURES_PATH}`).pipe(
        HttpClientRequest.bearerToken(peer.token),
        HttpClientRequest.acceptJson,
      ),
    )
    .pipe(
      Effect.timeout(peer.timeout),
      Effect.mapError(() => new ForkFeaturesUnanswered({ message: `${peer.url} did not answer.` })),
    );
  if (response.status !== 200) return {} satisfies ForkFeatures;
  const body = yield* response.json.pipe(Effect.option);
  return Option.getOrElse(Option.flatMap(body, decodeForkFeatures), () => ({}) as ForkFeatures);
});
