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

/** A temp home, or `baseDir` when given (a marked copy's home, say). */
const homeConfig = (baseDir?: string) =>
  ServerConfig.layerTest(process.cwd(), baseDir ?? { prefix: "t3-hold-work-link-" });

const cloudLinkFor = (heldFromBoot: boolean, baseDir?: string) =>
  Effect.gen(function* () {
    const recording = recordingCloudLink();
    const gate = yield* WorkAdmission.makeWorkAdmissionGate({ heldFromBoot });
    const cloudLink = yield* Effect.service(CloudLink.CloudLink).pipe(
      Effect.provide(
        HoldWorkConnect.layer(Layer.succeed(CloudLink.CloudLink, recording.service)).pipe(
          Layer.provide(Layer.succeed(WorkAdmission.WorkAdmission, gate)),
          Layer.provide(homeConfig(baseDir)),
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
const relayFor = (heldFromBoot: boolean, baseDir?: string) =>
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
        ).pipe(
          Layer.provide(Layer.succeed(WorkAdmission.WorkAdmission, gate)),
          Layer.provide(homeConfig(baseDir)),
          Layer.provide(NodeServices.layer),
        ),
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
  const buildOn = (baseDir: string, heldFromBoot: boolean, markerAtBuild: Array<boolean> = []) =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const marker = `${baseDir}/${HoldWorkConnect.HELD_COPY_MARKER_FILE}`;
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

  it.effect("a held boot that cannot write its marker fails before CloudLink is built", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const baseDir = yield* fs.makeTempDirectoryScoped({ prefix: "t3-hold-work-mark-" });
      // A normal boot first lays out the home, so only the marker write meets the read-only home.
      yield* buildOn(baseDir, false);
      yield* fs.chmod(baseDir, 0o500);
      const built: Array<boolean> = [];
      const exit = yield* buildOn(baseDir, true, built).pipe(Effect.exit);
      yield* fs.chmod(baseDir, 0o700);
      // A held copy that booted unmarked would be unguarded once it stops.
      expect(Exit.isFailure(exit) ? Cause.squash(exit.cause) : null).toMatchObject({
        reason: { _tag: "PermissionDenied" },
      });
      expect(built).toEqual([]);
      expect(yield* fs.exists(`${baseDir}/${HoldWorkConnect.HELD_COPY_MARKER_FILE}`)).toBe(false);
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

describe("holdWorkConnect normal boot of a marked copy", () => {
  it.effect.each([
    { label: "a marker", marker: "file" as const },
    { label: "a marker a start cut off empty", marker: "empty" as const },
    { label: "a dangling symlink in the marker's place", marker: "dangling" as const },
  ])("holds Connect and agent activity on $label, as a held boot does", ({ marker }) =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const baseDir = yield* fs.makeTempDirectoryScoped({ prefix: "t3-hold-work-marked-" });
      const markerPath = `${baseDir}/${HoldWorkConnect.HELD_COPY_MARKER_FILE}`;
      if (marker === "dangling") yield* fs.symlink(`${baseDir}/gone`, markerPath);
      else yield* fs.writeFileString(markerPath, marker === "empty" ? "" : "marked");

      const { calls, cloudLink } = yield* cloudLinkFor(false, baseDir);
      expect(yield* cloudLink.reconcileDesiredLinkIfStillDesired(ORIGIN)).toBeNull();
      expect(yield* cloudLink.registerManagedTunnelRecovery(ORIGIN)).toEqual({
        status: "not_linked",
      });
      expect(yield* cloudLink.startManagedTunnelIfOriginConfirmed(ORIGIN)).toBe(false);
      // The shutdown release would delete the running server's tunnel under the shared id.
      expect(yield* cloudLink.releaseManagedTunnelOnShutdown()).toBe(false);
      const refused = yield* Effect.flip(cloudLink.unlink());
      expect(refused).toEqual(HoldWorkConnect.heldConnectRefusal(markerPath));
      expect(refused.message).toContain(markerPath);
      expect(calls).toEqual([]);

      const activity = yield* relayFor(false, baseDir);
      yield* useRelay(activity.relay);
      expect(activity.calls).toEqual([]);
      // Nothing touched the marker.
      if (marker !== "dangling") {
        expect(yield* fs.readFileString(markerPath)).toBe(marker === "empty" ? "" : "marked");
      }
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );
});

describe("holdWorkConnect CLI refusal on a held copy's home", () => {
  const refusalFor = (home: {
    readonly state?: Record<string, unknown>;
    readonly marker?: "file" | "empty" | "unreadable" | "directory" | "dangling";
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
      else if (home.marker === "dangling") yield* fs.symlink(`${baseDir}/gone`, marker);
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
    // The held boot's exclusive create sees it as there and keeps it, so the CLI must too.
    { label: "a dangling symlink in the marker's place", marker: "dangling" as const },
    // Checking fails (no search permission on the home): counted as there.
    { label: "a marker that cannot be checked", marker: "file" as const, unsearchable: true },
  ])("refuses on $label, and says how to clear it", ({ marker, unsearchable }) =>
    Effect.gen(function* () {
      const result = yield* refusalFor({
        marker,
        unsearchable: unsearchable === true,
        state: running,
      });
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

describe("holdWorkConnect one marker read per boot", () => {
  /** Both held layers built together, as one server builds them, on a FileSystem that counts marker checks. */
  const bootBoth = (baseDir: string, deleteMarkerWhileCloudLinkBuilds: boolean) =>
    Effect.gen(function* () {
      const markerPath = `${baseDir}/${HoldWorkConnect.HELD_COPY_MARKER_FILE}`;
      const checks: Array<string> = [];
      const countingFs = Layer.effect(
        FileSystem.FileSystem,
        Effect.gen(function* () {
          const fs = yield* FileSystem.FileSystem;
          return FileSystem.FileSystem.of({
            ...fs,
            exists: (path) => {
              if (path === markerPath) checks.push("exists");
              return fs.exists(path);
            },
          });
        }),
      ).pipe(Layer.provide(NodeServices.layer));
      const cloud = recordingCloudLink();
      const relayCalls: Array<string> = [];
      // A hand edit while the server starts: the marker is deleted as the real CloudLink is built.
      const fakeCloudLink = Layer.effect(
        CloudLink.CloudLink,
        Effect.gen(function* () {
          if (deleteMarkerWhileCloudLinkBuilds) {
            yield* (yield* FileSystem.FileSystem).remove(markerPath);
          }
          return cloud.service;
        }),
      );
      const fakeRelay = Layer.succeed(
        AgentAwarenessRelay.AgentAwarenessRelay,
        AgentAwarenessRelay.AgentAwarenessRelay.of({
          publishThread: () => Effect.sync(() => void relayCalls.push("publishThread")),
          drain: Effect.sync(() => void relayCalls.push("drain")),
          requestCatchUp: () => Effect.sync(() => void relayCalls.push("requestCatchUp")),
          start: () => Effect.sync(() => void relayCalls.push("start")),
        }),
      );
      const gate = yield* WorkAdmission.makeWorkAdmissionGate({ heldFromBoot: false });
      const [cloudLink, relay] = yield* Effect.all([
        Effect.service(CloudLink.CloudLink),
        Effect.service(AgentAwarenessRelay.AgentAwarenessRelay),
      ]).pipe(
        Effect.provide(
          Layer.mergeAll(
            HoldWorkConnect.layer(fakeCloudLink),
            HoldWorkConnect.agentActivityLayer(fakeRelay),
          ).pipe(
            Layer.provide(Layer.succeed(WorkAdmission.WorkAdmission, gate)),
            Layer.provide(homeConfig(baseDir)),
            Layer.provide(countingFs),
            Layer.provide(NodeServices.layer),
          ),
        ),
      );
      return { checks, cloud, cloudLink, relay, relayCalls };
    });

  it.effect.each([
    { label: "a marked home", deleteMarkerWhileCloudLinkBuilds: false },
    {
      label: "a marked home whose marker is deleted by hand during the boot",
      deleteMarkerWhileCloudLinkBuilds: true,
    },
  ])(
    "CloudLink and agent activity share one read on $label, so both hold",
    ({ deleteMarkerWhileCloudLinkBuilds }) =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const baseDir = yield* fs.makeTempDirectoryScoped({ prefix: "t3-hold-work-once-" });
        yield* fs.writeFileString(`${baseDir}/${HoldWorkConnect.HELD_COPY_MARKER_FILE}`, "marked");
        const boot = yield* bootBoth(baseDir, deleteMarkerWhileCloudLinkBuilds);
        expect(boot.checks).toEqual(["exists"]);
        expect(yield* boot.cloudLink.startManagedTunnelIfOriginConfirmed(ORIGIN)).toBe(false);
        expect(yield* boot.cloudLink.releaseManagedTunnelOnShutdown()).toBe(false);
        expect(boot.cloud.calls).toEqual([]);
        yield* useRelay(boot.relay);
        expect(boot.relayCalls).toEqual([]);
      }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect("an unmarked home is read once too, and neither part is held", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const baseDir = yield* fs.makeTempDirectoryScoped({ prefix: "t3-hold-work-once-" });
      const boot = yield* bootBoth(baseDir, false);
      expect(boot.checks).toEqual(["exists"]);
      expect(yield* boot.cloudLink.startManagedTunnelIfOriginConfirmed(ORIGIN)).toBe("ran");
      yield* useRelay(boot.relay);
      expect(boot.relayCalls).toEqual(["start", "publishThread", "requestCatchUp", "drain"]);
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );
});
