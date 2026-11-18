// Fork (`serve --hold-work`): `t3 connect` and `t3 up` on a held copy's home, where they are called.
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { expect, it } from "@effect/vitest";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as NetService from "@t3tools/shared/Net";
import * as Cause from "effect/Cause";
import * as ConfigProvider from "effect/ConfigProvider";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as TestConsole from "effect/testing/TestConsole";
import { Command } from "effect/cli";
import * as FetchHttpClient from "effect/http/FetchHttpClient";
import { vi } from "vite-plus/test";

import packageJson from "../../package.json" with { type: "json" };
import { makeCli } from "../binCli.ts";
import * as BootService from "../cloud/bootService.ts";
import * as HoldWorkConnect from "../cloud/holdWorkConnect.ts";
import { upCommand } from "./up.ts";

// `t3 up` offers T3 Connect only in builds with its public config.
vi.mock("../cloud/publicConfig.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../cloud/publicConfig.ts")>()),
  hasCloudPublicConfig: true,
}));

const RELAY = "https://relay.hold-work.test";

/** What a full copy of a linked home carries: its link and a CLI sign-in far from expiry. */
const LINKED_SECRETS: Record<string, string> = {
  "cloud-cli-desired-link": "managed",
  "cloud-linked-user-id": "user-hold-work",
  "cloud-relay-url": RELAY,
  "cloud-cli-oauth-token": JSON.stringify({
    accessToken: "access-hold-work",
    refreshToken: "refresh-hold-work",
    expiresAtEpochMs: 4_102_444_800_000,
  }),
};

type Run = "unlink" | "logout" | "link" | "login" | "publish" | "up" | "up-off";

const ARGS: Record<Exclude<Run, "up" | "up-off">, ReadonlyArray<string>> = {
  unlink: ["connect", "unlink"],
  logout: ["connect", "logout"],
  link: ["connect", "link", "--publish-only", "--headless"],
  login: ["connect", "login", "--headless"],
  // Turning publishing on writes `cloud-publish-agent-activity`, which the copy has not set.
  publish: ["connect", "publish"],
};

const runOnHome = (run: Run, options: { readonly marked: boolean }) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const baseDir = yield* fs.makeTempDirectoryScoped({ prefix: "t3-connect-held-copy-" });
    const secretsDir = `${baseDir}/userdata/secrets`;
    yield* fs.makeDirectory(secretsDir, { recursive: true });
    for (const [name, value] of Object.entries(LINKED_SECRETS)) {
      // `t3 up --connect` only links a home whose Connect is off.
      if (run === "up" && name === "cloud-cli-desired-link") continue;
      yield* fs.writeFileString(`${secretsDir}/${name}.bin`, value);
    }
    if (options.marked) {
      yield* fs.writeFileString(`${baseDir}/${HoldWorkConnect.HELD_COPY_MARKER_FILE}`, "{}");
    }
    // The Connect link and sign-in; any CLI run makes the server signing key when it is missing.
    const secrets = Effect.gen(function* () {
      const names = (yield* fs.readDirectory(secretsDir))
        .filter((name) => name.startsWith("cloud-"))
        .toSorted();
      const entries: Array<[string, string]> = [];
      for (const name of names) {
        entries.push([name, yield* fs.readFileString(`${secretsDir}/${name}`)]);
      }
      return Object.fromEntries(entries);
    });
    const before = yield* secrets;

    // Every request the CLI makes: relay, sign-in and relay client download alike.
    const requests: Array<string> = [];
    const fetch: typeof globalThis.fetch = (input, init) => {
      const url = input instanceof Request ? input.url : String(input);
      const method = init?.method ?? (input instanceof Request ? input.method : "GET");
      requests.push(`${method} ${url}`);
      return Promise.resolve(Response.json({ ok: true }));
    };
    const restarts: Array<string> = [];
    vi.spyOn(BootService, "layer").mockReturnValue(
      Layer.succeed(
        BootService.BootService,
        BootService.BootService.of({
          status: Effect.succeed({
            supported: true,
            installed: true,
            current: true,
            unitPath: "/home/me/.config/systemd/user/t3code.service",
            logPath: "/home/me/.t3/userdata/logs/boot-service.log",
          }),
          withStopped: (operation) => operation,
          restart: Effect.sync(() => restarts.push("restart")).pipe(Effect.as("skipped" as const)),
          install: () => Effect.die("a current service is not reinstalled"),
          uninstall: Effect.succeed(false),
        }),
      ),
    );
    if (run === "up" || run === "up-off") {
      yield* fs.writeFileString(`${baseDir}/service.env`, "T3CODE_LAN=false\n");
    }

    const command =
      run === "up" || run === "up-off"
        ? Command.runWith(upCommand, { version: packageJson.version })([
            "--base-dir",
            baseDir,
            run === "up" ? "--connect" : "--no-connect",
          ])
        : Command.runWith(makeCli({ cloudEnabled: true }), { version: "0.0.0" })([
            ...ARGS[run],
            "--base-dir",
            baseDir,
          ]);
    const exit = yield* command.pipe(
      Effect.provideService(FetchHttpClient.Fetch, fetch),
      Effect.provideService(HostProcess.Environment, {}),
      Effect.exit,
    );
    return {
      error: Exit.isFailure(exit) ? Cause.squash(exit.cause) : null,
      requests,
      restarts,
      secretsBefore: before,
      secretsAfter: yield* secrets,
    };
  }).pipe(
    Effect.scoped,
    Effect.provide(
      Layer.mergeAll(
        ConfigProvider.layer(
          ConfigProvider.fromEnv({
            env: {
              T3CODE_RELAY_URL: RELAY,
              // No managed policy, whatever this machine has: a missing file means none.
              T3CODE_DEV_MANAGED_POLICY_PATH: "/nonexistent/t3-hold-work-policy.json",
            },
          }),
        ),
        Layer.fresh(TestConsole.layer),
      ),
    ),
  );

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer, NetService.layer))(
  "t3 connect and t3 up on a held copy's home",
  (it) => {
    it.effect("unlink on an unmarked home sends the relay delete (what the refusals stop)", () =>
      Effect.gen(function* () {
        const result = yield* runOnHome("unlink", { marked: false });
        expect(result.error).toBeNull();
        expect(result.requests).toHaveLength(1);
        expect(result.requests[0]).toMatch(
          new RegExp(`^DELETE ${RELAY}/v1/client/environment-links/[^/]+$`),
        );
      }),
    );

    it.effect.each([
      { run: "unlink" as const },
      { run: "logout" as const },
      { run: "link" as const },
      { run: "login" as const },
      { run: "publish" as const },
      { run: "up" as const },
      // `t3 up` turning Connect off: its unlink ends in the relay delete.
      { run: "up-off" as const },
    ])("$run on a marked home is refused before any request or change", ({ run }) =>
      Effect.gen(function* () {
        const result = yield* runOnHome(run, { marked: true });
        expect(result.error).toBeInstanceOf(HoldWorkConnect.HeldCopyRefusedError);
        expect(result.requests).toEqual([]);
        expect(result.restarts).toEqual([]);
        expect(result.secretsAfter).toEqual(result.secretsBefore);
      }),
    );
  },
);
