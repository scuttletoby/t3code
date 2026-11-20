// The fork features route mounted in the real router beside upstream's static fallback (`GET *`), as
// the server mounts it, with a fake auth, and asked by the real reader a delegating machine uses.
import { describe, expect, it } from "@effect/vitest";
import * as NodeHttpServer from "@effect/platform-node/NodeHttpServer";
import * as NodeServices from "@effect/platform-node/NodeServices";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Scope from "effect/Scope";
import {
  HttpClient,
  HttpClientRequest,
  HttpRouter,
  HttpServerRequest,
  HttpServerResponse,
} from "effect/http";

import { ServerSettingsError } from "@t3tools/contracts";

import * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import * as ServerConfig from "../config.ts";
import * as ServerHttp from "../http.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import * as ForkFeatures from "./forkFeatures.ts";

/** A linked machine's token, an MCP client's token, or none. */
const layerFakeAuth = Layer.succeed(EnvironmentAuth.EnvironmentAuth, {
  authenticateMcpClient: (request: HttpServerRequest.HttpServerRequest) => {
    const authorization = request.headers.authorization;
    if (authorization === "Bearer link-token") return Effect.succeed({ peerLink: true } as never);
    if (authorization === "Bearer client-token")
      return Effect.succeed({ peerLink: false } as never);
    return Effect.fail(new EnvironmentAuth.ServerAuthMissingCredentialError({}));
  },
} as unknown as EnvironmentAuth.EnvironmentAuth["Service"]);

/**
 * A server with upstream's static app page, plus this route when `withRoute` (this version), or
 * another answer at the features path (a proxy or a machine that is not this version).
 */
const serve = (
  withRoute:
    | boolean
    | Layer.Layer<
        never,
        never,
        | HttpRouter.HttpRouter
        | EnvironmentAuth.EnvironmentAuth
        | ServerConfig.ServerConfig
        | FileSystem.FileSystem
        | ManagedPolicy.ManagedPolicy
      >,
) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const path = yield* Path.Path;
    const config = yield* ServerConfig.ServerConfig;
    const staticDir = yield* fs.makeTempDirectoryScoped({ prefix: "t3-fork-features-" });
    yield* fs.writeFileString(path.join(staticDir, "index.html"), "<!doctype html><p>app");
    const layerApp =
      withRoute === false
        ? ServerHttp.layerStaticAndDevRoute
        : Layer.merge(
            withRoute === true ? ForkFeatures.layer : withRoute,
            ServerHttp.layerStaticAndDevRoute,
          );
    const services = yield* Layer.build(
      HttpRouter.serve(layerApp, { disableListenLog: true, disableLogger: true }).pipe(
        Layer.provideMerge(layerFakeAuth),
        Layer.provideMerge(ServerSettingsService.ServerSettingsService.layerTest()),
        Layer.provideMerge(ManagedPolicy.layerTest()),
        Layer.provideMerge(ServerConfig.layer({ ...config, staticDir })),
        Layer.provideMerge(NodeHttpServer.layerTest),
      ),
    );
    const client = Context.get(services, HttpClient.HttpClient);
    const settings = Context.get(services, ServerSettingsService.ServerSettingsService);
    return {
      client,
      settings,
      /** What the delegating machine's reader makes of this server, with `token`. */
      read: (token: string) =>
        ForkFeatures.readForkFeatures(client, { url: "", token, timeout: "5 seconds" }),
      get: (headers: Record<string, string>) =>
        client.execute(HttpClientRequest.get(ForkFeatures.FORK_FEATURES_PATH, { headers })),
    };
  });

const provided = <A, E>(
  effect: Effect.Effect<
    A,
    E,
    ServerConfig.ServerConfig | FileSystem.FileSystem | Path.Path | Scope.Scope
  >,
) =>
  effect.pipe(
    Effect.scoped,
    Effect.provide(
      ServerConfig.layerTest(process.cwd(), { prefix: "t3-fork-features-config-" }).pipe(
        Layer.provideMerge(NodeServices.layer),
      ),
    ),
  );

describe("fork features route in the server's router", () => {
  it.effect("answers a linked machine's token, and wins over the static fallback", () =>
    provided(
      Effect.gen(function* () {
        const server = yield* serve(true);
        expect(yield* server.read("link-token")).toEqual({ mcpProfileCarry: true });
        // Asked again, the same answer: nothing is used up by asking.
        expect(yield* server.read("link-token")).toEqual({ mcpProfileCarry: true });
        const answer = yield* server.get({ authorization: "Bearer link-token" });
        expect(answer.headers["cache-control"]).toBe("no-store");
        // Upstream's fallback still serves the app for every other path.
        expect((yield* server.client.execute(HttpClientRequest.get("/some/page"))).status).toBe(
          200,
        );
      }),
    ),
  );

  it.effect("answers false while this machine is set to refuse carried profiles (#640)", () =>
    provided(
      Effect.gen(function* () {
        const server = yield* serve(true);
        yield* server.settings.updateSettings({ mcpCarriedProfiles: "refuse" });
        expect(yield* server.read("link-token")).toEqual({ mcpProfileCarry: false });
        // Each ask reads the setting again: set back, the next ask says yes.
        yield* server.settings.updateSettings({ mcpCarriedProfiles: "apply" });
        expect(yield* server.read("link-token")).toEqual({ mcpProfileCarry: true });
      }),
    ),
  );

  /** A server whose settings read fails (it reads provider secrets too). */
  const failingSettings = ForkFeatures.layer.pipe(
    Layer.provide(
      Layer.mock(ServerSettingsService.ServerSettingsService)({
        getSettings: Effect.fail(
          new ServerSettingsError({
            settingsPath: "settings.json",
            operation: "read-secret",
            cause: new Error("secret store unreadable"),
          }),
        ),
      }),
    ),
  );

  /** Writes settings.json: its text, or "unreadable" (a directory in its place). */
  const writeSettingsFile = (file: string) =>
    Effect.gen(function* () {
      const { settingsPath } = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      if (file === "unreadable") yield* fs.makeDirectory(settingsPath, { recursive: true });
      else yield* fs.writeFileString(settingsPath, file);
    });

  // The route reads the settings as a Claude turn does: the same answer in each state.
  it.effect.each<readonly [string, string | undefined, boolean]>([
    ["no settings.json: the defaults, apply", undefined, true],
    ["a settings.json that says apply", JSON.stringify({ mcpCarriedProfiles: "apply" }), true],
    ["a settings.json that says refuse", JSON.stringify({ mcpCarriedProfiles: "refuse" }), false],
    ["a settings.json that does not decode", "{ not json", false],
    ["an empty settings.json", "", false],
    ["a settings.json that cannot be read either", "unreadable", false],
  ])("a failed settings read with %s answers as the turn reads it", ([, file, expected]) =>
    provided(
      Effect.gen(function* () {
        if (file !== undefined) yield* writeSettingsFile(file);
        const server = yield* serve(failingSettings);
        expect(yield* server.read("link-token")).toEqual({ mcpProfileCarry: expected });
        // Asked again, the same answer.
        expect(yield* server.read("link-token")).toEqual({ mcpProfileCarry: expected });
      }),
    ),
  );

  it.effect(
    "a settings.json that does not decode answers false though the read gave the defaults (#661)",
    () =>
      provided(
        Effect.gen(function* () {
          // Upstream gives every setting its default for a file that was broken at load (or went
          // bad since), apply too; the file may say refuse.
          const server = yield* serve(true);
          expect(yield* server.read("link-token")).toEqual({ mcpProfileCarry: true });
          yield* writeSettingsFile("{ not json");
          expect(yield* server.read("link-token")).toEqual({ mcpProfileCarry: false });
          // Fixed, the next ask says yes again.
          yield* writeSettingsFile("{}");
          expect(yield* server.read("link-token")).toEqual({ mcpProfileCarry: true });
        }),
      ),
  );

  it.effect.each<readonly [string, Record<string, string>]>([
    ["no token", {}],
    ["an MCP client's token", { authorization: "Bearer client-token" }],
    ["a token it does not know", { authorization: "Bearer other" }],
    [
      "a browser, even with a linked machine's token",
      {
        authorization: "Bearer link-token",
        origin: "http://evil.test",
      },
    ],
  ])("%s gets upstream's plain 404", ([, headers]) =>
    provided(
      Effect.gen(function* () {
        const server = yield* serve(true);
        const response = yield* server.get(headers);
        expect(response.status).toBe(404);
        expect(yield* response.text).toBe("Not Found");
      }),
    ),
  );

  it.effect("a redirect is not followed, even to an address that answers yes", () =>
    provided(
      Effect.gen(function* () {
        const moved = "/moved/features";
        const server = yield* serve(
          Layer.mergeAll(
            HttpRouter.add(
              "GET",
              ForkFeatures.FORK_FEATURES_PATH,
              HttpServerResponse.redirect(moved, { status: 302 }),
            ),
            HttpRouter.add("GET", moved, HttpServerResponse.jsonUnsafe({ mcpProfileCarry: true })),
          ),
        );
        // A plain fetch follows it to the yes, so only the reader's own setting refuses it.
        const followed = yield* server.get({ authorization: "Bearer link-token" });
        expect(yield* followed.json).toEqual({ mcpProfileCarry: true });
        expect(yield* server.read("link-token")).toEqual({});
      }),
    ),
  );

  it.effect.each([500, 404, 201])(
    "this route's JSON with status %s reads as no features",
    (status) =>
      provided(
        Effect.gen(function* () {
          const server = yield* serve(
            HttpRouter.add(
              "GET",
              ForkFeatures.FORK_FEATURES_PATH,
              HttpServerResponse.jsonUnsafe({ mcpProfileCarry: true }, { status }),
            ),
          );
          expect((yield* server.get({ authorization: "Bearer link-token" })).status).toBe(status);
          expect(yield* server.read("link-token")).toEqual({});
        }),
      ),
  );

  it.effect("an older server answers with its app page, which reads as no features", () =>
    provided(
      Effect.gen(function* () {
        const server = yield* serve(false);
        const page = yield* server.get({ authorization: "Bearer link-token" });
        expect(page.status).toBe(200);
        expect(yield* server.read("link-token")).toEqual({});
      }),
    ),
  );
});
