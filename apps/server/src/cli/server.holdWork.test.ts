// @effect-diagnostics nodeBuiltinImport:off - CLI integration uses temporary Node paths.
// Fork (go-live kit): `serve --hold-work` is refused for the background service and on the default
// home (it would mark the live home as a copy).
import * as NodePath from "node:path";

import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, expect, it } from "@effect/vitest";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as NetService from "@t3tools/shared/Net";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import { Command } from "effect/cli";

import packageJson from "../../package.json" with { type: "json" };
import { ServiceLauncherHostProcess } from "../cloud/serviceLauncherClient.ts";
import {
  SERVICE_LAUNCHER_CONTEXT_ENV,
  SERVICE_LAUNCHER_PROTOCOL,
} from "../cloud/serviceProtocol.ts";
import { cli } from "../binCli.ts";
import { refuseHoldWorkForBootService, refuseHoldWorkOnDefaultHome } from "./forkHoldWork.ts";

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

describe("serve --hold-work needs a home named with --base-dir, never the default one", () => {
  /** A temp folder stands in for the user's home folder, so `~/.t3` is `<root>/.t3`. */
  const onHome = <A, E, R>(body: (root: string) => Effect.Effect<A, E, R>) =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const root = yield* fs.makeTempDirectoryScoped({ prefix: "t3-hold-work-home-" });
      return yield* body(root).pipe(Effect.provideService(HostProcess.HomeDirectory, root));
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer));

  const refusal = (flag: Option.Option<string>) =>
    refuseHoldWorkOnDefaultHome(flag).pipe(
      Effect.flip,
      Effect.map((error) => error.message),
    );

  it.effect("refuses no --base-dir, or a blank one, and says what to run", () =>
    onHome(() =>
      Effect.gen(function* () {
        for (const flag of [Option.none(), Option.some(""), Option.some("  ")]) {
          const message = yield* refusal(flag);
          expect(message).toContain("--hold-work is refused without --base-dir");
          expect(message).toContain("--base-dir <copy>");
          expect(message).toContain("hold-work-copy.txt");
        }
      }),
    ),
  );

  it.effect("refuses the default home however it is spelled, or a symlink to it", () =>
    onHome((root) =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const defaultHome = NodePath.join(root, ".t3");
        yield* fs.makeDirectory(defaultHome);
        yield* fs.symlink(defaultHome, NodePath.join(root, "live-link"));
        for (const flag of [
          defaultHome,
          `${defaultHome}/`,
          "~/.t3",
          NodePath.join(root, "x", "..", ".t3"),
          NodePath.join(root, "live-link"),
        ]) {
          const message = yield* refusal(Option.some(flag));
          expect(message).toContain(
            `--hold-work is refused on the default T3 home (${defaultHome})`,
          );
        }
      }),
    ),
  );

  it.effect("lets a named copy through, made or not", () =>
    onHome((root) =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        yield* refuseHoldWorkOnDefaultHome(Option.some(NodePath.join(root, "copy", "home")));
        yield* fs.makeDirectory(NodePath.join(root, "made"));
        yield* refuseHoldWorkOnDefaultHome(Option.some(NodePath.join(root, "made")));
        // Beside the default home, not the default home itself.
        yield* refuseHoldWorkOnDefaultHome(Option.some("~/.t3-copy"));
      }),
    ),
  );

  it.effect("serve --hold-work with no --base-dir is refused before the default home is made", () =>
    onHome((root) =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const { host, env } = launcher({ connected: false });
        const exit = yield* Command.runWith(cli, { version: "0.0.0" })([
          "serve",
          "--hold-work",
        ]).pipe(
          Effect.provideService(ServiceLauncherHostProcess, host),
          Effect.provideService(HostProcess.Environment, env),
          Effect.provide(Layer.mergeAll(NodeServices.layer, NetService.layer)),
          Effect.exit,
        );
        assert.isTrue(Exit.isFailure(exit));
        assert.include(
          String(Exit.isFailure(exit) ? exit.cause : ""),
          "--hold-work is refused without --base-dir",
        );
        assert.isFalse(yield* fs.exists(NodePath.join(root, ".t3")));
      }).pipe(Effect.provide(NodeCrypto.layer)),
    ),
  );
});
