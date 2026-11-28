/**
 * Fork: beside server-runtime.json, says this build checks agents' picks on the routes below
 * (modelPolicyCheck.ts). model-guard leaves those to T3 only for the process named here: an older
 * build loads the managed lock too but checks neither. A file of its own keeps upstream's files free.
 */
import { writeFileStringAtomically } from "@t3tools/shared/atomicWrite";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";

import * as ServerConfig from "../config.ts";
import type { PolicyRoute } from "./modelPolicyCheck.ts";

/** The marker's name, in the folder of the server's server-runtime.json. */
export const MODEL_POLICY_ROUTES_MARKER = "fork-capabilities.json";

/**
 * The routes this server checks against its model policy for an agent's call, whatever the
 * calling client: model-guard leaves exactly these to T3 (its SERVER_CHECKED_TOOLS).
 */
export const MODEL_POLICY_CHECKED_ROUTES = [
  "delegate_task",
  "create_threads",
] as const satisfies ReadonlyArray<PolicyRoute>;

const MarkerContents = Schema.Struct({
  version: Schema.Literal(1),
  pid: Schema.Number,
  modelPolicyRoutes: Schema.Array(Schema.String),
});
const encodeMarker = Schema.encodeEffect(Schema.fromJsonString(MarkerContents));

/** What the marker holds for the process `pid`. */
export const markerContents = (pid: number): typeof MarkerContents.Type => ({
  version: 1,
  pid,
  modelPolicyRoutes: [...MODEL_POLICY_CHECKED_ROUTES],
});

/** The marker beside `serverRuntimeStatePath`. */
export const markerPath = (path: Path.Path, serverRuntimeStatePath: string) =>
  path.join(path.dirname(serverRuntimeStatePath), MODEL_POLICY_ROUTES_MARKER);

/** In server.ts's runtime services (fork/forkLayers.ts). A failed write only logs: the guard then keeps checking. */
export const layer = Layer.effectDiscard(
  Effect.gen(function* () {
    const config = yield* ServerConfig.ServerConfig;
    const path = yield* Path.Path;
    const filePath = markerPath(path, config.serverRuntimeStatePath);
    yield* encodeMarker(markerContents(process.pid)).pipe(
      Effect.flatMap((json) => writeFileStringAtomically({ filePath, contents: `${json}\n` })),
      Effect.catchCause((cause) =>
        Effect.logWarning("model policy routes marker: write failed", { filePath, cause }),
      ),
    );
  }).pipe(Effect.withSpan("fork.modelPolicyRoutesMarker.write")),
);
