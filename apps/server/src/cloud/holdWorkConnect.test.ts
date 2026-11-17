import * as NodeServices from "@effect/platform-node/NodeServices";
import { EnvironmentAuthenticatedPrincipal, ThreadId } from "@t3tools/contracts";
import { describe, expect, it } from "@effect/vitest";
import * as Cause from "effect/Cause";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";

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

describe("holdWorkConnect CLI unlink", () => {
  const refusalFor = (state: Record<string, unknown> | null) =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const dir = yield* fs.makeTempDirectoryScoped({ prefix: "t3-hold-work-cli-" });
      const statePath = `${dir}/server-runtime.json`;
      if (state !== null) yield* fs.writeFileString(statePath, JSON.stringify(state));
      return yield* HoldWorkConnect.refuseCliUnlinkWhileHeld(statePath).pipe(Effect.exit);
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer));
  const running = {
    version: 1,
    pid: process.pid,
    port: 1,
    origin: ORIGIN,
    startedAt: "2026-01-01T00:00:00.000Z",
  };

  it.effect("refuses while this home's server runs held", () =>
    Effect.gen(function* () {
      const exit = yield* refusalFor({ ...running, workHeld: true });
      expect(Exit.isFailure(exit)).toBe(true);
      const error = Exit.isFailure(exit) ? Cause.squash(exit.cause) : null;
      expect(error).toBeInstanceOf(HoldWorkConnect.HeldCliUnlinkRefusedError);
      expect(String((error as Error).message)).toContain("--hold-work");
    }),
  );

  it.effect("lets it run for a normal server, no server, or a held server that is gone", () =>
    Effect.gen(function* () {
      expect(Exit.isSuccess(yield* refusalFor(running))).toBe(true);
      expect(Exit.isSuccess(yield* refusalFor(null))).toBe(true);
      // A pid that cannot be alive: the held server stopped and left its record behind.
      expect(
        Exit.isSuccess(yield* refusalFor({ ...running, pid: 2 ** 22 + 1, workHeld: true })),
      ).toBe(true);
    }),
  );
});
