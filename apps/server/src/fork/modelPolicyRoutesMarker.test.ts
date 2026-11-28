// @effect-diagnostics nodeBuiltinImport:off - the marker is read back as model-guard reads it, from disk.
/**
 * Fork: the marker model-guard needs before it leaves `delegate_task` and `create_threads` to T3
 * (modelPolicyRoutesMarker.ts): written at start beside server-runtime.json, naming this process
 * and the routes. model-guard's own tests (fork-kit/model-guard/tests/guard.test.py) read this file's
 * name and route list from the source, so the two cannot drift apart.
 */
import * as NodeFS from "node:fs";
import * as NodePath from "node:path";

import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import type * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import type * as Path from "effect/Path";

import * as ServerConfig from "../config.ts";
import * as ForkLayers from "./forkLayers.ts";
import * as ModelPolicyRoutesMarker from "./modelPolicyRoutesMarker.ts";

const withConfig = <A, E>(
  effect: Effect.Effect<A, E, ServerConfig.ServerConfig | FileSystem.FileSystem | Path.Path>,
) =>
  effect.pipe(
    Effect.provide(
      ServerConfig.layerTest(process.cwd(), { prefix: "t3-model-policy-marker-" }).pipe(
        Layer.provideMerge(NodeServices.layer),
      ),
    ),
  );

it.effect("writes the marker beside server-runtime.json, naming this process and the routes", () =>
  withConfig(
    Effect.gen(function* () {
      const config = yield* ServerConfig.ServerConfig;
      const marker = NodePath.join(
        NodePath.dirname(config.serverRuntimeStatePath),
        "fork-capabilities.json",
      );
      assert.isFalse(NodeFS.existsSync(marker));
      yield* Layer.build(ModelPolicyRoutesMarker.layer).pipe(Effect.scoped);
      const stat = NodeFS.lstatSync(marker);
      assert.isTrue(stat.isFile());
      assert.deepStrictEqual(JSON.parse(NodeFS.readFileSync(marker, "utf8")), {
        version: 1,
        pid: process.pid,
        modelPolicyRoutes: ["delegate_task", "create_threads"],
      });
    }),
  ),
);

it.effect("a marker another process left is replaced by this one's", () =>
  withConfig(
    Effect.gen(function* () {
      const config = yield* ServerConfig.ServerConfig;
      const marker = NodePath.join(
        NodePath.dirname(config.serverRuntimeStatePath),
        ModelPolicyRoutesMarker.MODEL_POLICY_ROUTES_MARKER,
      );
      NodeFS.writeFileSync(marker, JSON.stringify(ModelPolicyRoutesMarker.markerContents(1)));
      yield* Layer.build(ModelPolicyRoutesMarker.layer).pipe(Effect.scoped);
      assert.strictEqual(JSON.parse(NodeFS.readFileSync(marker, "utf8")).pid, process.pid);
    }),
  ),
);

it.effect("a marker that cannot be written fails nothing: the guard then keeps checking", () =>
  withConfig(
    Effect.gen(function* () {
      const config = yield* ServerConfig.ServerConfig;
      const marker = NodePath.join(
        NodePath.dirname(config.serverRuntimeStatePath),
        ModelPolicyRoutesMarker.MODEL_POLICY_ROUTES_MARKER,
      );
      NodeFS.mkdirSync(marker);
      NodeFS.writeFileSync(NodePath.join(marker, "keep"), "x");
      yield* Layer.build(ModelPolicyRoutesMarker.layer).pipe(Effect.scoped);
      assert.isTrue(NodeFS.lstatSync(marker).isDirectory());
    }),
  ),
);

it("the server's runtime services write it", () => {
  const source = NodeFS.readFileSync(NodePath.join(import.meta.dirname, "forkLayers.ts"), "utf8");
  const services = source.slice(source.indexOf("export const layerRuntimeServices"));
  assert.include(services.slice(0, services.indexOf(");")), "ModelPolicyRoutesMarker.layer");
  assert.isDefined(ForkLayers.layerRuntimeServices);
});
