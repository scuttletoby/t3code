import { describe, expect, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Ref from "effect/Ref";
import * as HttpServerRequest from "effect/http/HttpServerRequest";
import * as HttpServerResponse from "effect/http/HttpServerResponse";

import { type AutoUpdateActivity, noActivity } from "../cloud/autoUpdate.ts";
import {
  ACTIVITY_PROBE_PATH,
  ACTIVITY_PROBE_VERSION,
  type ActivityProbeServerState,
  activityProbeAnswer,
  handleActivityProbe,
  isLocalProbeRequest,
  trackServerState,
} from "./activityProbe.ts";

const server = { pid: 4242, serverVersion: "1.2.3" };

describe("activityProbeAnswer", () => {
  it("is quiet only while running with nothing working: a server with no threads is quiet", () => {
    expect(activityProbeAnswer({ ...server, state: "running", activity: noActivity })).toEqual({
      probeVersion: ACTIVITY_PROBE_VERSION,
      quiet: true,
      reason: null,
      state: "running",
      pid: 4242,
      serverVersion: "1.2.3",
      activity: noActivity,
    });
  });

  it.each([
    ["a turn that is starting", { activeRuns: 1 }, "1 running turn"],
    ["a queued turn", { queuedRuns: 2 }, "2 queued turns"],
    ["a pending approval", { pendingRequests: 1 }, "1 pending approval or question"],
    ["a background task", { backgroundTasks: 1 }, "1 background task"],
    ["a terminal command", { terminalCommands: 1 }, "1 running terminal command"],
  ] satisfies ReadonlyArray<readonly [string, Partial<AutoUpdateActivity>, string]>)(
    "is busy with %s",
    (_, counts, reason) => {
      const answer = activityProbeAnswer({
        ...server,
        state: "running",
        activity: { ...noActivity, ...counts },
      });
      expect(answer.quiet).toBe(false);
      expect(answer.reason).toBe(reason);
    },
  );

  it("is never quiet while stopping, even with nothing working", () => {
    expect(
      activityProbeAnswer({ ...server, state: "stopping", activity: noActivity }),
    ).toMatchObject({ quiet: false, reason: "T3 is stopping", state: "stopping" });
  });

  it("is busy when thread state cannot be read", () => {
    expect(activityProbeAnswer({ ...server, state: "running", activity: null })).toMatchObject({
      quiet: false,
      reason: "thread state could not be read",
      activity: null,
    });
  });
});

describe("isLocalProbeRequest", () => {
  it.each(["127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"])("answers %s", (peer) => {
    expect(isLocalProbeRequest(Option.some(peer), {})).toBe(true);
  });

  it.each(["192.168.1.5", "100.64.0.7", "::ffff:10.0.0.1", "fe80::1", ""])("refuses %s", (peer) => {
    expect(isLocalProbeRequest(Option.some(peer), {})).toBe(false);
  });

  it("refuses a request with no peer address", () => {
    expect(isLocalProbeRequest(Option.none(), {})).toBe(false);
  });

  // Tailscale Serve, cloudflared and other local proxies connect from loopback for remote callers.
  it.each([
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "forwarded",
    "x-real-ip",
    "cf-connecting-ip",
    "via",
  ])("refuses a loopback request a proxy forwarded (%s)", (header) => {
    expect(isLocalProbeRequest(Option.some("127.0.0.1"), { [header]: "203.0.113.9" })).toBe(false);
  });

  // A web page in a browser on this machine can reach loopback too: a cross-site fetch sends Origin,
  // and a page on a rebound name (DNS rebinding) sends its own Host.
  it("refuses a request a browser page made", () => {
    expect(isLocalProbeRequest(Option.some("127.0.0.1"), { origin: "https://example.test" })).toBe(
      false,
    );
    expect(
      isLocalProbeRequest(Option.some("127.0.0.1"), { host: "rebound.example.test:3773" }),
    ).toBe(false);
  });

  // A no-cors request (`<img src>`, `fetch(url, {mode: "no-cors"})`) carries no Origin, but every
  // browser request carries fetch metadata.
  it.each([
    { "sec-fetch-site": "cross-site" },
    { "sec-fetch-dest": "image" },
    { "sec-fetch-mode": "no-cors" },
    { "sec-fetch-mode": "navigate" },
    { "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty" },
  ])("refuses a request with browser fetch metadata (%o)", (metadata) => {
    expect(
      isLocalProbeRequest(Option.some("127.0.0.1"), { host: "127.0.0.1:3773", ...metadata }),
    ).toBe(false);
  });

  // Node's fetch sends this one header on every request; the reader uses it.
  it("answers Node's fetch, which sends sec-fetch-mode: cors alone", () => {
    expect(
      isLocalProbeRequest(Option.some("::ffff:127.0.0.1"), {
        host: "127.0.0.1:3773",
        "sec-fetch-mode": "cors",
      }),
    ).toBe(true);
  });

  it.each(["127.0.0.1:3773", "[::1]:3773", "localhost:3773", "localhost"])(
    "answers a local Host (%s)",
    (host) => {
      expect(isLocalProbeRequest(Option.some("127.0.0.1"), { host })).toBe(true);
    },
  );
});

// Upstream's global readiness middleware holds every HTTP request until the server accepts
// commands, so a just-started server gives the probe no answer (the reader's timeout says busy);
// only the way down needs its own state.
describe("trackServerState", () => {
  it.effect("is running while the routes are up and stopping once they are torn down", () =>
    Effect.gen(function* () {
      const seen = yield* Effect.scoped(
        Effect.gen(function* () {
          const state = yield* trackServerState;
          return { during: yield* state, state };
        }),
      );
      expect(seen.during).toBe("running");
      expect(yield* seen.state).toBe("stopping");
    }),
  );
});

/** JSON, or the text itself for a plain-text answer (the 404 for callers that are not local). */
const parseBody = (text: string): unknown => {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
};

describe("handleActivityProbe", () => {
  const request = (peer: string | null, headers: Record<string, string> = {}) =>
    HttpServerRequest.fromWeb(
      new Request(`http://127.0.0.1:3773${ACTIVITY_PROBE_PATH}`, { headers }),
    ).modify({ peerAddress: peer === null ? Option.none() : Option.some(peer) });

  const run = Effect.fn("test.runActivityProbe")(function* (
    input: {
      readonly enabled?: boolean;
      readonly state?: ActivityProbeServerState;
      readonly activity?: Effect.Effect<AutoUpdateActivity, string>;
      readonly peer?: string | null;
      readonly headers?: Record<string, string>;
    } = {},
  ) {
    const reads = yield* Ref.make(0);
    const response = yield* handleActivityProbe({
      ...server,
      enabled: Effect.succeed(input.enabled ?? true),
      state: Effect.succeed(input.state ?? "running"),
      activity: Ref.update(reads, (count) => count + 1).pipe(
        Effect.andThen(input.activity ?? Effect.succeed(noActivity)),
      ),
    }).pipe(
      Effect.provideService(
        HttpServerRequest.HttpServerRequest,
        request(input.peer === undefined ? "127.0.0.1" : input.peer, input.headers),
      ),
    );
    const web = HttpServerResponse.toWeb(response);
    const body = yield* Effect.promise(() => web.text()).pipe(Effect.map(parseBody));
    return { status: web.status, body, reads: yield* Ref.get(reads) };
  });

  it.effect("answers a local caller with the server's state and counts", () =>
    Effect.gen(function* () {
      const { status, body } = yield* run({
        activity: Effect.succeed({ ...noActivity, activeRuns: 1 }),
      });
      expect(status).toBe(200);
      expect(body).toMatchObject({
        probeVersion: ACTIVITY_PROBE_VERSION,
        quiet: false,
        reason: "1 running turn",
        state: "running",
        pid: 4242,
      });
    }),
  );

  it.effect("says not found, and reads nothing, while the setting is off", () =>
    Effect.gen(function* () {
      const { status, body, reads } = yield* run({ enabled: false });
      expect(status).toBe(404);
      expect(body).toEqual({ error: "activity probe is off (server.activityProbe)" });
      expect(reads).toBe(0);
    }),
  );

  // On or off, a caller that is not local learns nothing: not that this is the fork, not the setting.
  it.effect("gives every caller that is not local a plain 404, on or off, reading nothing", () =>
    Effect.gen(function* () {
      for (const enabled of [true, false]) {
        for (const input of [
          { peer: "192.168.1.5" },
          { peer: null },
          { peer: "127.0.0.1", headers: { "x-forwarded-for": "203.0.113.9" } },
          { peer: "127.0.0.1", headers: { "tailscale-user-login": "someone" } },
          { peer: "127.0.0.1", headers: { origin: "https://example.test" } },
          { peer: "127.0.0.1", headers: { "sec-fetch-mode": "no-cors" } },
        ]) {
          const { status, body, reads } = yield* run({ ...input, enabled });
          expect(status).toBe(404);
          expect(body).toBe("Not Found");
          expect(reads).toBe(0);
        }
      }
    }),
  );

  it.effect("answers busy, not an error, when thread state cannot be read", () =>
    Effect.gen(function* () {
      const { status, body } = yield* run({ activity: Effect.fail("database locked") });
      expect(status).toBe(200);
      expect(body).toMatchObject({ quiet: false, reason: "thread state could not be read" });
    }),
  );

  it.effect("does not read thread state while stopping", () =>
    Effect.gen(function* () {
      const { body, reads } = yield* run({ state: "stopping" });
      expect(body).toMatchObject({ quiet: false, state: "stopping", activity: null });
      expect(reads).toBe(0);
    }),
  );
});
