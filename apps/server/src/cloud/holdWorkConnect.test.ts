import * as NodeServices from "@effect/platform-node/NodeServices";
import { EnvironmentAuthenticatedPrincipal, ThreadId } from "@t3tools/contracts";
import { describe, expect, it } from "@effect/vitest";
import * as Cause from "effect/Cause";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";

import * as ServerConfig from "../config.ts";
import * as AgentAwarenessRelay from "../relay/AgentAwarenessRelay.ts";
import * as WorkAdmission from "../workAdmission.ts";
import * as CloudLink from "./CloudLink.ts";
import * as HoldWorkConnect from "./holdWorkConnect.ts";

const ORIGIN = "http://127.0.0.1:1";

/** A CloudLink whose every method records its call and succeeds with "ran". */
const recordingCloudLink = () => {
  const calls: Array<string> = [];
  const service = new Proxy(
    {},
    {
      get: (_target, name) => () => {
        calls.push(String(name));
        return Effect.succeed("ran");
      },
    },
  ) as CloudLink.CloudLink["Service"];
  return { calls, service };
};

const cloudLinkFor = (heldFromBoot: boolean) =>
  Effect.gen(function* () {
    const recording = recordingCloudLink();
    const gate = yield* WorkAdmission.makeWorkAdmissionGate({ heldFromBoot });
    const cloudLink = yield* Effect.service(CloudLink.CloudLink).pipe(
      Effect.provide(
        HoldWorkConnect.layer(Layer.succeed(CloudLink.CloudLink, recording.service)).pipe(
          Layer.provide(Layer.succeed(WorkAdmission.WorkAdmission, gate)),
          Layer.provide(ServerConfig.layerTest(process.cwd(), { prefix: "t3-hold-work-link-" })),
          Layer.provide(NodeServices.layer),
        ),
      ),
    );
    return { ...recording, cloudLink };
  });

describe("holdWorkConnect", () => {
  it.effect("a normal server uses the CloudLink it was given", () =>
    Effect.gen(function* () {
      const { calls, cloudLink } = yield* cloudLinkFor(false);
      expect(yield* cloudLink.startManagedTunnelIfOriginConfirmed(ORIGIN)).toBe("ran");
      expect(yield* cloudLink.releaseManagedTunnelOnShutdown()).toBe("ran");
      expect(calls).toEqual([
        "startManagedTunnelIfOriginConfirmed",
        "releaseManagedTunnelOnShutdown",
      ]);
    }),
  );

  it.effect("a held server starts, registers and releases no tunnel and refuses a new link", () =>
    Effect.gen(function* () {
      const { calls, cloudLink } = yield* cloudLinkFor(true);
      // Startup and shutdown: each does nothing.
      expect(yield* cloudLink.reconcileDesiredLinkIfStillDesired(ORIGIN)).toBeNull();
      expect(yield* cloudLink.registerManagedTunnelRecovery(ORIGIN)).toEqual({
        status: "not_linked",
      });
      expect(yield* cloudLink.recoverManagedTunnel(ORIGIN)).toBe(false);
      expect(yield* cloudLink.startManagedTunnelIfOriginConfirmed(ORIGIN)).toBe(false);
      expect(yield* cloudLink.releaseManagedTunnelOnShutdown()).toBe(false);
      // A client's request to link or change the link: refused, with the reason.
      const refused = yield* Effect.all([
        Effect.flip(cloudLink.linkProof(undefined as never, undefined as never)),
        Effect.flip(cloudLink.applyRelayConfig(undefined as never)),
        Effect.flip(cloudLink.updatePreferences(undefined as never)),
        Effect.flip(cloudLink.answerHealthRequest(undefined as never)),
        Effect.flip(cloudLink.mintCredential(undefined as never)),
      ]).pipe(Effect.provideService(EnvironmentAuthenticatedPrincipal, undefined as never));
      for (const error of refused) {
        expect(error).toEqual(HoldWorkConnect.heldConnectRefusal());
        expect(error.message).toContain("--hold-work");
      }
      // Unlink too: the web Unlink follows it with a relay delete of the shared environment id.
      const unlinkRefused = yield* Effect.flip(cloudLink.unlink());
      expect(unlinkRefused).toEqual(HoldWorkConnect.heldConnectRefusal());
      expect(calls).toEqual([]);
      // Reading the link state touches nothing: it still runs.
      yield* cloudLink.linkState();
      expect(calls).toEqual(["linkState"]);
    }),
  );
});

/** An agent activity publisher whose every method records its call. */
const relayFor = (heldFromBoot: boolean) =>
  Effect.gen(function* () {
    const calls: Array<string> = [];
    const record = (name: string) => () => Effect.sync(() => void calls.push(name));
    const gate = yield* WorkAdmission.makeWorkAdmissionGate({ heldFromBoot });
    const relay = yield* Effect.service(AgentAwarenessRelay.AgentAwarenessRelay).pipe(
      Effect.provide(
        HoldWorkConnect.agentActivityLayer(
          Layer.succeed(
            AgentAwarenessRelay.AgentAwarenessRelay,
            AgentAwarenessRelay.AgentAwarenessRelay.of({
              publishThread: record("publishThread"),
              drain: record("drain")(),
              requestCatchUp: record("requestCatchUp"),
              start: record("start"),
            }),
          ),
        ).pipe(Layer.provide(Layer.succeed(WorkAdmission.WorkAdmission, gate))),
      ),
    );
    return { calls, relay };
  });

const useRelay = (relay: AgentAwarenessRelay.AgentAwarenessRelay["Service"]) =>
  Effect.gen(function* () {
    yield* Effect.scoped(relay.start());
    yield* relay.publishThread(ThreadId.make("thread:hold-work"));
    yield* relay.requestCatchUp();
    yield* relay.drain;
  });

describe("holdWorkConnect agent activity", () => {
  it.effect("a normal server uses the publisher it was given", () =>
    Effect.gen(function* () {
      const { calls, relay } = yield* relayFor(false);
      yield* useRelay(relay);
      expect(calls).toEqual(["start", "publishThread", "requestCatchUp", "drain"]);
    }),
  );

  it.effect("a held server starts no publisher and publishes nothing", () =>
    Effect.gen(function* () {
      const { calls, relay } = yield* relayFor(true);
      yield* useRelay(relay);
      expect(calls).toEqual([]);
    }),
  );
});

describe("holdWorkConnect held-copy marker", () => {
  /** Builds the held or normal CloudLink layer on `baseDir`; the fake CloudLink notes whether the marker was there when it was built. */
  const buildOn = (baseDir: string, heldFromBoot: boolean) =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const marker = `${baseDir}/${HoldWorkConnect.HELD_COPY_MARKER_FILE}`;
      const markerAtBuild: Array<boolean> = [];
      const gate = yield* WorkAdmission.makeWorkAdmissionGate({ heldFromBoot });
      const fake = Layer.effect(
        CloudLink.CloudLink,
        Effect.gen(function* () {
          markerAtBuild.push(yield* fs.exists(marker));
          return recordingCloudLink().service;
        }),
      );
      yield* Effect.service(CloudLink.CloudLink).pipe(
        Effect.provide(
          HoldWorkConnect.layer(fake).pipe(
            Layer.provide(Layer.succeed(WorkAdmission.WorkAdmission, gate)),
            Layer.provide(ServerConfig.layerTest(process.cwd(), baseDir)),
          ),
        ),
      );
      return { marker, markerAtBuild };
    });

  it.effect(
    "a held boot marks its home before CloudLink is built; a normal boot keeps the mark",
    () =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const baseDir = yield* fs.makeTempDirectoryScoped({ prefix: "t3-hold-work-mark-" });
        const normal = yield* buildOn(baseDir, false);
        expect(yield* fs.exists(normal.marker)).toBe(false);

        const held = yield* buildOn(baseDir, true);
        expect(held.markerAtBuild).toEqual([true]);
        const written = yield* fs.readFileString(held.marker);
        expect(written).toContain("--hold-work");

        // A normal boot of the copy later does not clear it: that boot is itself the unsafe step.
        yield* buildOn(baseDir, false);
        expect(yield* fs.readFileString(held.marker)).toBe(written);
      }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect(
    "a second held start keeps the marker as it is, even one cut off empty or unreadable",
    () =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        for (const mode of [0o644, 0o000]) {
          const baseDir = yield* fs.makeTempDirectoryScoped({ prefix: "t3-hold-work-mark-" });
          const marker = `${baseDir}/${HoldWorkConnect.HELD_COPY_MARKER_FILE}`;
          // What a start stopped right after creating the file leaves behind.
          yield* fs.writeFileString(marker, "");
          yield* fs.chmod(marker, mode);
          const again = yield* buildOn(baseDir, true);
          expect(again.markerAtBuild).toEqual([true]);
          yield* fs.chmod(marker, 0o644);
          expect(yield* fs.readFileString(marker)).toBe("");
        }
      }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );
});

describe("holdWorkConnect CLI refusal on a held copy's home", () => {
  const refusalFor = (home: {
    readonly state?: Record<string, unknown>;
    readonly marker?: "file" | "empty" | "unreadable" | "directory";
    readonly secrets?: boolean;
    readonly missing?: boolean;
    readonly unsearchable?: boolean;
  }) =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const root = yield* fs.makeTempDirectoryScoped({ prefix: "t3-hold-work-cli-" });
      const baseDir = home.missing ? `${root}/never-made` : root;
      const serverRuntimeStatePath = `${baseDir}/userdata/server-runtime.json`;
      const marker = `${baseDir}/${HoldWorkConnect.HELD_COPY_MARKER_FILE}`;
      if (!home.missing) yield* fs.makeDirectory(`${baseDir}/userdata`, { recursive: true });
      if (home.state) yield* fs.writeFileString(serverRuntimeStatePath, JSON.stringify(home.state));
      if (home.secrets) {
        // A linked home copied before markers existed: its link files, and no marker.
        yield* fs.makeDirectory(`${baseDir}/userdata/secrets`, { recursive: true });
        yield* fs.writeFileString(
          `${baseDir}/userdata/secrets/cloud-cli-desired-link.bin`,
          "managed",
        );
      }
      if (home.marker === "directory") yield* fs.makeDirectory(marker);
      else if (home.marker !== undefined) {
        yield* fs.writeFileString(marker, home.marker === "empty" ? "" : "{not json");
        if (home.marker === "unreadable") yield* fs.chmod(marker, 0o000);
      }
      if (home.unsearchable) yield* fs.chmod(baseDir, 0o600);
      const exit = yield* HoldWorkConnect.refuseCliOnHeldCopy({
        baseDir,
        serverRuntimeStatePath,
      }).pipe(Effect.exit);
      if (home.unsearchable) yield* fs.chmod(baseDir, 0o700);
      return { marker, exit };
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer));
  const running = {
    version: 1,
    pid: process.pid,
    port: 1,
    origin: ORIGIN,
    startedAt: "2026-01-01T00:00:00.000Z",
  };
  const refusal = (exit: Exit.Exit<void, unknown>) =>
    Exit.isFailure(exit) ? Cause.squash(exit.cause) : null;

  it.effect("refuses while this home's server runs held", () =>
    Effect.gen(function* () {
      const { exit } = yield* refusalFor({ state: { ...running, workHeld: true } });
      const error = refusal(exit);
      expect(error).toBeInstanceOf(HoldWorkConnect.HeldCopyRefusedError);
      expect(String((error as Error).message)).toContain("--hold-work");
    }),
  );

  it.effect.each([
    { label: "a marked home with no server", marker: "file" as const },
    { label: "a marker a start cut off empty", marker: "empty" as const },
    { label: "an unreadable marker", marker: "unreadable" as const },
    { label: "a directory in the marker's place", marker: "directory" as const },
    // Checking fails (no search permission on the home): counted as there.
    { label: "a marker that cannot be checked", marker: "file" as const, unsearchable: true },
  ])("refuses on $label, and says how to clear it", ({ marker, unsearchable }) =>
    Effect.gen(function* () {
      const result = yield* refusalFor({ marker, unsearchable, state: running });
      const error = refusal(result.exit);
      expect(error).toBeInstanceOf(HoldWorkConnect.HeldCopyRefusedError);
      const message = String((error as Error).message);
      expect(message).toContain(result.marker);
      expect(message).toContain("delete");
    }),
  );

  it.effect(
    "lets it run for a normal server, no server, a held server that is gone, an empty home, or a home linked before markers",
    () =>
      Effect.gen(function* () {
        expect(Exit.isSuccess((yield* refusalFor({ state: running })).exit)).toBe(true);
        expect(Exit.isSuccess((yield* refusalFor({})).exit)).toBe(true);
        // A pid that cannot be alive: an older held server stopped and left its record behind.
        expect(
          Exit.isSuccess(
            (yield* refusalFor({ state: { ...running, pid: 2 ** 22 + 1, workHeld: true } })).exit,
          ),
        ).toBe(true);
        expect(Exit.isSuccess((yield* refusalFor({ missing: true })).exit)).toBe(true);
        // Not protected: nothing tells such a copy from the live home until it is started held.
        expect(Exit.isSuccess((yield* refusalFor({ secrets: true })).exit)).toBe(true);
      }),
  );
});
