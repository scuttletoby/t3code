// The probe mounted in the real router beside upstream's static fallback (`GET *`), as the server
// mounts it, with fake thread and terminal state, and asked by the real reader.
import { describe, expect, it } from "@effect/vitest";
import * as NodeHttpServer from "@effect/platform-node/NodeHttpServer";
import * as NodeServices from "@effect/platform-node/NodeServices";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as NetAddress from "effect/net/NetAddress";
import * as Path from "effect/Path";
import { HttpClient, HttpClientRequest, HttpRouter, HttpServer } from "effect/http";

import * as ServerConfig from "../config.ts";
import * as ServerHttp from "../http.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as TerminalManager from "../terminal/Manager.ts";
import { readActivityProbe } from "../golive/activityProbeRead.ts";
import { ACTIVITY_PROBE_PATH, layerRoute } from "./activityProbe.ts";

const layerFakeState = Layer.mergeAll(
  Layer.succeed(ProjectionStore.ProjectionStoreV2, {
    getRecoveryThreadIds: () => Effect.succeed([]),
  } as never),
  Layer.succeed(TerminalManager.TerminalManager, {
    subscribeMetadata: () => Effect.succeed(() => {}),
  } as never),
);

describe("activity probe in the server's router", () => {
  it.effect("wins over the static fallback, and follows the setting without a restart", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const config = yield* ServerConfig.ServerConfig;
      const staticDir = yield* fs.makeTempDirectoryScoped({ prefix: "t3-activity-probe-" });
      yield* fs.writeFileString(path.join(staticDir, "index.html"), "<!doctype html><p>app");

      const layerApp = Layer.merge(layerRoute, ServerHttp.layerStaticAndDevRoute);
      const services = yield* Layer.build(
        HttpRouter.serve(layerApp, { disableListenLog: true, disableLogger: true }).pipe(
          Layer.provideMerge(layerFakeState),
          Layer.provideMerge(ServerSettings.ServerSettingsService.layerTest()),
          Layer.provideMerge(ServerConfig.layer({ ...config, staticDir })),
          Layer.provideMerge(NodeHttpServer.layerTest),
        ),
      );
      const client = Context.get(services, HttpClient.HttpClient);
      const settings = Context.get(services, ServerSettings.ServerSettingsService);
      const address = Context.get(services, HttpServer.HttpServer).address;
      const port = NetAddress.isInetAddress(address) ? address.port : 0;
      // A T3 home naming this test process as the server, at the router's port.
      const home = yield* fs.makeTempDirectoryScoped({ prefix: "t3-activity-probe-home-" });
      yield* fs.makeDirectory(path.join(home, "userdata"));
      yield* fs.writeFileString(
        path.join(home, "userdata", "server-runtime.json"),
        JSON.stringify({
          version: 1,
          pid: process.pid,
          port,
          origin: `http://127.0.0.1:${port}`,
          startedAt: "2026-11-19T10:00:00.000Z",
        }),
      );
      const read = () => Effect.promise(() => readActivityProbe(home));
      const get = (headers: Record<string, string> = {}) =>
        client.execute(HttpClientRequest.get(ACTIVITY_PROBE_PATH, { headers }));

      // Off by default: the route answers its own 404, not the web app.
      const off = yield* get();
      expect(off.status).toBe(404);
      expect(yield* off.json).toEqual({ error: "activity probe is off (server.activityProbe)" });
      // Upstream's fallback still serves the app for every other path.
      expect((yield* client.execute(HttpClientRequest.get("/some/page"))).status).toBe(200);
      expect(yield* read()).toMatchObject({ quiet: false, state: "off" });

      yield* settings.updateSettings({ activityProbe: true });
      const on = yield* get();
      expect(on.status).toBe(200);
      expect(yield* on.json).toMatchObject({ quiet: true, state: "running", pid: process.pid });
      // The real reader, with Node's fetch and its fetch-metadata header, is answered.
      expect(yield* read()).toMatchObject({ quiet: true, state: "quiet", pid: process.pid });

      // A browser on this machine gets the plain 404.
      const browser = yield* get({ "sec-fetch-site": "cross-site", "sec-fetch-dest": "image" });
      expect(browser.status).toBe(404);
      expect(yield* browser.text).toBe("Not Found");

      yield* settings.updateSettings({ activityProbe: false });
      expect((yield* get()).status).toBe(404);
    }).pipe(
      Effect.scoped,
      Effect.provide(
        ServerConfig.layerTest(process.cwd(), { prefix: "t3-activity-probe-config-" }).pipe(
          Layer.provideMerge(NodeServices.layer),
        ),
      ),
    ),
  );
});
