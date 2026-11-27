/**
 * Fork (fleet usage, fork issue 1146): the collector's round over faked links. With no linked
 * machine nothing is asked; one round serves every reader for a minute; a slow machine is unknown
 * after ten seconds; a machine that goes silent keeps naming its accounts, as unknown.
 */
import type { EnvironmentId, ForkUsageSnapshot } from "@t3tools/contracts";
import { describe, expect, it } from "@effect/vitest";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Ref from "effect/Ref";
import * as TestClock from "effect/testing/TestClock";

import {
  type FleetDeps,
  makeCollector,
  MACHINE_TIMEOUT,
  read,
  REASON_TIMED_OUT,
  ROUND_TTL,
} from "./fleetUsage.ts";

const iso = (ms: number) => DateTime.formatIso(DateTime.makeUnsafe(ms));

const id = (value: string) => value as EnvironmentId;

const snapshot = (key: string, readAt = 0): ForkUsageSnapshot => ({
  readAt,
  accounts: [
    {
      key,
      shared: true,
      driver: "codex",
      name: "Codex",
      checkedAt: iso(readAt),
      windows: [{ id: "weekly", kind: "weekly", label: "Weekly", usedPercent: 10 }],
    },
  ],
});

type Answer = Effect.Success<ReturnType<FleetDeps["ask"]>>;

const world = (input: {
  links: Array<string>;
  ask?: (environmentId: EnvironmentId, call: number) => Effect.Effect<Answer>;
}) =>
  Effect.gen(function* () {
    const calls = yield* Ref.make<Array<string>>([]);
    const deps: FleetDeps = {
      here: Effect.succeed({ environmentId: id("here"), label: "Here" }),
      snapshotHere: Effect.succeed(snapshot("mine")),
      links: Effect.succeed(input.links.map((link) => ({ environmentId: id(link), label: link }))),
      ask: (environmentId) =>
        Ref.updateAndGet(calls, (all) => [...all, environmentId]).pipe(
          Effect.flatMap((all) =>
            input.ask === undefined
              ? Effect.succeed<Answer>({
                  _tag: "answered",
                  snapshot: snapshot(`of-${environmentId}`),
                })
              : input.ask(environmentId, all.length),
          ),
        ),
    };
    const collector = yield* makeCollector(deps);
    return { collector, calls };
  });

describe("fleet usage collector", () => {
  it.effect("asks nobody and says unlinked when no machine is linked", () =>
    Effect.gen(function* () {
      const { collector, calls } = yield* world({ links: [] });
      const answer = yield* collector.read;
      expect(answer.linked).toBe(false);
      expect(answer.machines.map((m) => m.environmentId)).toEqual(["here"]);
      expect(answer.accounts.map((a) => a.key)).toEqual(["mine"]);
      expect(yield* Ref.get(calls)).toEqual([]);
    }),
  );

  it.effect("never asks this server over a link to itself", () =>
    Effect.gen(function* () {
      const { collector, calls } = yield* world({ links: ["here"] });
      const answer = yield* collector.read;
      expect(answer.linked).toBe(false);
      expect(yield* Ref.get(calls)).toEqual([]);
    }),
  );

  it.effect("serves every reader from one round for a minute, then asks again", () =>
    Effect.gen(function* () {
      const { collector, calls } = yield* world({ links: ["b", "c"] });
      const first = yield* collector.read;
      yield* collector.read;
      expect(yield* Ref.get(calls)).toEqual(["b", "c"]);
      expect(first.linked).toBe(true);
      expect(first.accounts.map((a) => a.key)).toEqual(["mine", "of-b", "of-c"]);
      yield* TestClock.adjust(Duration.sum(ROUND_TTL, Duration.seconds(1)));
      yield* collector.read;
      expect(yield* Ref.get(calls)).toHaveLength(4);
    }),
  );

  it.effect(
    "shows a machine that does not answer in time unknown, the others as they answered",
    () =>
      Effect.gen(function* () {
        const { collector } = yield* world({
          links: ["slow", "b"],
          ask: (environmentId) =>
            environmentId === "slow"
              ? Effect.never
              : Effect.succeed({ _tag: "answered", snapshot: snapshot("of-b") }),
        });
        const fiber = yield* Effect.forkChild(collector.read);
        yield* TestClock.adjust(MACHINE_TIMEOUT);
        const answer = yield* Fiber.join(fiber);
        expect(answer.machines.find((m) => m.environmentId === "slow")).toMatchObject({
          state: "unknown",
          reason: REASON_TIMED_OUT,
        });
        expect(answer.machines.find((m) => m.environmentId === "b")?.state).toBe("answered");
      }),
  );

  it.effect("keeps naming a silent machine's accounts, unknown, after it stops answering", () =>
    Effect.gen(function* () {
      const { collector } = yield* world({
        links: ["b"],
        ask: (_environmentId, call) =>
          Effect.succeed<Answer>(
            call === 1
              ? { _tag: "answered", snapshot: snapshot("of-b") }
              : { _tag: "failed", reason: "The linked environment stopped answering." },
          ),
      });
      expect((yield* collector.read).accounts.find((a) => a.key === "of-b")?.state).toBe("known");
      yield* TestClock.adjust(Duration.sum(ROUND_TTL, Duration.seconds(1)));
      const later = yield* collector.read;
      expect(later.accounts.find((a) => a.key === "of-b")).toMatchObject({
        state: "unknown",
        reason: "b did not answer: The linked environment stopped answering.",
        windows: [],
      });
      expect(later.machines.find((m) => m.environmentId === "b")?.state).toBe("unknown");
    }),
  );

  it.effect("answers unlinked and empty on a server without the service", () =>
    Effect.gen(function* () {
      const answer = yield* read({});
      expect(answer).toMatchObject({ linked: false, machines: [], accounts: [] });
    }),
  );
});
