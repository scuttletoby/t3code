// @effect-diagnostics nodeBuiltinImport:off - CLI integration uses temporary Node paths.
// Fork (go-live kit): `serve --hold-work` is refused for the background service.
import * as NodePath from "node:path";

import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as NetService from "@t3tools/shared/Net";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import { Command } from "effect/cli";

import packageJson from "../../package.json" with { type: "json" };
import { ServiceLauncherHostProcess } from "../cloud/serviceLauncherClient.ts";
import {
  SERVICE_LAUNCHER_CONTEXT_ENV,
  SERVICE_LAUNCHER_PROTOCOL,
} from "../cloud/serviceProtocol.ts";
import { cli } from "../binCli.ts";
import { refuseHoldWorkForBootService } from "./server.ts";

const launcher = (input: { readonly connected: boolean; readonly context?: string }) => ({
  host: {
    connected: input.connected,
    send: () => true,
    on: () => {},
    off: () => {},
  },
  env: input.context === undefined ? {} : { [SERVICE_LAUNCHER_CONTEXT_ENV]: input.context },
});

const validContext = JSON.stringify({
  protocol: SERVICE_LAUNCHER_PROTOCOL,
  childVersion: packageJson.version,
});

const check = (input: { readonly connected: boolean; readonly context?: string }) => {
  const { host, env } = launcher(input);
  return refuseHoldWorkForBootService.pipe(
    Effect.provideService(ServiceLauncherHostProcess, host),
    Effect.provideService(HostProcess.Environment, env),
    Effect.result,
  );
};

it.effect.each([
  { name: "the background service", connected: true, context: validContext, refused: true },
  { name: "an unreadable launcher context", connected: true, context: "{", refused: true },
  {
    name: "a launcher context for another version",
    connected: true,
    context: JSON.stringify({ protocol: SERVICE_LAUNCHER_PROTOCOL, childVersion: "0.0.0-other" }),
    refused: true,
  },
  { name: "a server started by hand", connected: false, refused: false },
  {
    name: "a context inherited without a launcher channel",
    connected: false,
    context: validContext,
    refused: false,
  },
])("refuses --hold-work for $name: $refused", ({ connected, context, refused }) =>
  Effect.gen(function* () {
    const result = yield* check(context === undefined ? { connected } : { connected, context });
    assert.equal(result._tag === "Failure", refused);
    if (result._tag === "Failure") {
      assert.include(result.failure.message, "--hold-work is refused for the background service");
    }
  }),
);

it.effect("parses serve --hold-work and refuses it under the launcher before creating state", () =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const root = yield* fs.makeTempDirectoryScoped({ prefix: "t3-hold-work-cli-" });
    const baseDir = NodePath.join(root, "state");
    const { host, env } = launcher({ connected: true, context: validContext });
    const exit = yield* Command.runWith(cli, { version: "0.0.0" })([
      "serve",
      "--hold-work",
      "--base-dir",
      baseDir,
    ]).pipe(
      Effect.provideService(ServiceLauncherHostProcess, host),
      Effect.provideService(HostProcess.Environment, env),
      Effect.provide(Layer.mergeAll(NodeServices.layer, NetService.layer)),
      Effect.exit,
    );
    assert.isTrue(Exit.isFailure(exit));
    assert.include(String(Exit.isFailure(exit) ? exit.cause : ""), "--hold-work is refused");
    assert.isFalse(yield* fs.exists(baseDir));
  }).pipe(Effect.provide(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))),
);
