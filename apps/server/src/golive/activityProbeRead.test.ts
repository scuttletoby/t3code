// @effect-diagnostics nodeBuiltinImport:off - the tests build real T3 homes on disk.
import * as NodeFS from "node:fs";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";

import { afterEach, describe, expect, it } from "@effect/vitest";

import { ACTIVITY_PROBE_PATH, ACTIVITY_PROBE_VERSION } from "../fork/activityProbe.ts";
import { type ActivityProbeReadDeps, readActivityProbe } from "./activityProbeRead.ts";

const homes: Array<string> = [];
afterEach(() => {
  for (const home of homes.splice(0)) NodeFS.rmSync(home, { recursive: true, force: true });
});

const makeHome = (record?: unknown) => {
  const home = NodeFS.mkdtempSync(NodePath.join(NodeOS.tmpdir(), "activity-probe-"));
  homes.push(home);
  NodeFS.mkdirSync(NodePath.join(home, "userdata"));
  if (record !== undefined) {
    NodeFS.writeFileSync(
      NodePath.join(home, "userdata", "server-runtime.json"),
      typeof record === "string" ? record : JSON.stringify(record),
    );
  }
  return home;
};

const record = (overrides: Record<string, unknown> = {}) => ({
  version: 1,
  pid: 4242,
  port: 3773,
  origin: "http://127.0.0.1:3773",
  startedAt: "2026-11-19T10:00:00.000Z",
  serverVersion: "1.2.3",
  ...overrides,
});

const answer = (overrides: Record<string, unknown> = {}) => ({
  probeVersion: ACTIVITY_PROBE_VERSION,
  quiet: true,
  reason: null,
  state: "running",
  pid: 4242,
  serverVersion: "1.2.3",
  activity: {
    activeRuns: 0,
    queuedRuns: 0,
    pendingRequests: 0,
    backgroundTasks: 0,
    terminalCommands: 0,
  },
  ...overrides,
});

const deps = (
  reply: (url: string) => Promise<Response>,
  alive: ReadonlyArray<number> = [4242],
): ActivityProbeReadDeps & { readonly urls: Array<string> } => {
  const urls: Array<string> = [];
  return {
    urls,
    isAlive: (pid) => alive.includes(pid),
    fetch: (url) => {
      urls.push(url);
      return reply(url);
    },
    timeoutMs: 50,
  };
};

const json = (body: unknown, status = 200) =>
  Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );

describe("readActivityProbe", () => {
  it("is quiet when the running server says so, asked on loopback at its own port", async () => {
    const ask = deps(() => json(answer()));
    const reading = await readActivityProbe(makeHome(record()), ask);
    expect(reading).toMatchObject({ quiet: true, state: "quiet", reason: "nothing is running" });
    expect(reading.answer).toEqual(answer());
    expect(ask.urls).toEqual([`http://127.0.0.1:3773${ACTIVITY_PROBE_PATH}`]);
  });

  it("asks loopback even when the server listens on every address or a private one", async () => {
    for (const origin of ["http://127.0.0.1:4000", "http://100.64.0.7:4000"]) {
      const ask = deps(() => json(answer()));
      await readActivityProbe(makeHome(record({ port: 4000, origin })), ask);
      expect(ask.urls).toEqual([`http://127.0.0.1:4000${ACTIVITY_PROBE_PATH}`]);
    }
    const ask = deps(() => json(answer()));
    await readActivityProbe(makeHome(record({ port: 4000, origin: "http://[::1]:4000" })), ask);
    expect(ask.urls).toEqual([`http://[::1]:4000${ACTIVITY_PROBE_PATH}`]);
  });

  it("is busy, with the server's reason, when work is running", async () => {
    const reading = await readActivityProbe(
      makeHome(record()),
      deps(() =>
        json(answer({ quiet: false, reason: "1 running turn, 1 pending approval or question" })),
      ),
    );
    expect(reading).toMatchObject({
      quiet: false,
      state: "busy",
      reason: "1 running turn, 1 pending approval or question",
    });
  });

  it("is busy while the server is stopping", async () => {
    const reading = await readActivityProbe(
      makeHome(record()),
      deps(() =>
        json(answer({ quiet: false, state: "stopping", reason: "T3 is stopping", activity: null })),
      ),
    );
    expect(reading).toMatchObject({ quiet: false, state: "busy", reason: "T3 is stopping" });
  });

  it("says stopped, not quiet, with no runtime record or a dead process, and asks nothing", async () => {
    for (const home of [makeHome(), makeHome(record({ pid: 999_999 }))]) {
      const ask = deps(() => json(answer()));
      const reading = await readActivityProbe(home, ask);
      expect(reading).toMatchObject({ quiet: false, state: "stopped" });
      expect(ask.urls).toEqual([]);
    }
  });

  it("says stopped for a home that does not exist", async () => {
    const reading = await readActivityProbe(
      NodePath.join(makeHome(), "missing"),
      deps(() => json(answer())),
    );
    expect(reading).toMatchObject({ quiet: false, state: "stopped" });
  });

  it("is unknown for a runtime record it cannot read", async () => {
    for (const bad of ["{ not json", JSON.stringify({ version: 2, pid: 4242, port: 3773 }), "[]"]) {
      const ask = deps(() => json(answer()));
      const reading = await readActivityProbe(makeHome(bad), ask);
      expect(reading).toMatchObject({ quiet: false, state: "unknown" });
      expect(ask.urls).toEqual([]);
    }
  });

  it("is unreachable when nothing answers yet (a server that just started) or the connection fails", async () => {
    // Upstream holds every request until the server accepts commands: the probe times out.
    const starting = await readActivityProbe(
      makeHome(record()),
      deps((url) => new Promise<Response>(() => void url)),
    );
    expect(starting).toMatchObject({ quiet: false, state: "unreachable" });
    expect(starting.reason).toContain("did not answer");
    const refused = await readActivityProbe(
      makeHome(record()),
      deps(() => Promise.reject(new TypeError("fetch failed: ECONNREFUSED"))),
    );
    expect(refused).toMatchObject({ quiet: false, state: "unreachable" });
  });

  it("is unreachable, not a crash, when the answer's body fails after the headers came", async () => {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('{"probeVersion":1,'));
        controller.error(new TypeError("terminated"));
      },
    });
    const reading = await readActivityProbe(
      makeHome(record()),
      deps(() => Promise.resolve(new Response(body, { status: 200 }))),
    );
    expect(reading).toMatchObject({ quiet: false, state: "unreachable" });
    expect(reading.reason).toContain("terminated");
  });

  it("says off when the server has the probe turned off", async () => {
    const reading = await readActivityProbe(
      makeHome(record()),
      deps(() => json({ error: "activity probe is off (server.activityProbe)" }, 404)),
    );
    expect(reading).toMatchObject({ quiet: false, state: "off" });
    expect(reading.reason).toContain("server.activityProbe");
  });

  it("is unknown for an older server, which answers the path with its web app or a bare 404", async () => {
    const replies = [
      () =>
        Promise.resolve(
          new Response("<!doctype html><html></html>", {
            status: 200,
            headers: { "content-type": "text/html" },
          }),
        ),
      () => Promise.resolve(new Response("Not Found", { status: 404 })),
      () => json({ error: "forbidden" }, 403),
      () => json(answer({ probeVersion: 2 })),
      () => json(answer({ quiet: "yes" })),
      () => json(answer({ state: "running", reason: null, quiet: false })),
      // Quiet needs counts, all of them 0.
      () => json(answer({ activity: null })),
      () => json(answer({ activity: { ...answer().activity, backgroundTasks: 1 } })),
    ];
    for (const reply of replies) {
      const reading = await readActivityProbe(makeHome(record()), deps(reply));
      expect(reading).toMatchObject({ quiet: false, state: "unknown" });
    }
  });

  it("is unknown when another process answers on the recorded port", async () => {
    const reading = await readActivityProbe(
      makeHome(record()),
      deps(() => json(answer({ pid: 5151 })), [4242, 5151]),
    );
    expect(reading).toMatchObject({ quiet: false, state: "unknown" });
    expect(reading.reason).toContain("5151");
  });

  it("writes nothing to the home", async () => {
    const home = makeHome(record());
    const before = NodeFS.readdirSync(home, { recursive: true });
    await readActivityProbe(
      home,
      deps(() => json(answer())),
    );
    expect(NodeFS.readdirSync(home, { recursive: true })).toEqual(before);
  });
});
