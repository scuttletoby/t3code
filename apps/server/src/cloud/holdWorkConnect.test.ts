import { EnvironmentAuthenticatedPrincipal } from "@t3tools/contracts";
import { describe, expect, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

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
      expect(calls).toEqual([]);
      // Reading the link and unlinking touch only this home's files: they still run.
      yield* cloudLink.linkState();
      yield* cloudLink.unlink();
      expect(calls).toEqual(["linkState", "unlink"]);
    }),
  );
});
