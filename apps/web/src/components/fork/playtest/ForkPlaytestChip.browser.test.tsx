// Fork (playtest F5): the thread chip as a person sees it: shown while a session is on, End ends it
// through the server, and it goes when the session ends any other way. Each test names the
// question it answers.
import { EnvironmentId, ThreadId } from "@t3tools/contracts";
import { Atom } from "effect/reactivity";
import { describe, expect, it, vi } from "vite-plus/test";
import { userEvent } from "vite-plus/test/browser";

import "~/index.css";
import { render } from "../../../../test/render";

const state = vi.hoisted(() => ({
  session: null as unknown,
  canOperate: true,
  calls: [] as Array<{ label: string; input: unknown }>,
  endOutcome: "ended" as string,
}));

vi.mock(import("@t3tools/client-runtime/state/runtime"), async (original) => ({
  ...(await original()),
  runAtomCommand: (async (
    _registry: unknown,
    command: { label: string },
    target: { input: unknown },
  ) => {
    state.calls.push({ label: command.label, input: target.input });
    if (command.label === "end") {
      state.session = null;
      return { _tag: "Success", value: { outcome: state.endOutcome } };
    }
    return { _tag: "Success", value: state.session };
  }) as never,
}));
vi.mock(import("./playtestChipRpc"), () => ({
  forkPlaytestStatus: { label: "status" } as never,
  forkPlaytestEnd: { label: "end" } as never,
}));
const settingsAtom = vi.hoisted(() => ({ value: null as unknown }));
vi.mock(import("../../../state/server"), async (original) => ({
  ...(await original()),
  serverEnvironment: {
    ...(await original()).serverEnvironment,
    settingsValueAtom: (() => settingsAtom.value) as never,
  },
}));
vi.mock(import("../../../state/session"), async (original) => ({
  ...(await original()),
  useEnvironmentScope: (() => state.canOperate) as never,
}));

import { ForkPlaytestChip } from "./ForkPlaytestChip";

const ref = { environmentId: EnvironmentId.make("local"), threadId: ThreadId.make("thread-a") };
const SESSION = {
  id: "pt_abcdefghij",
  target: "dev-1",
  note: "",
  state: "active",
  startedAt: "2026-11-27T10:00:00.000Z",
  rounds: 1,
};

function setup(playtestOn: boolean, session: unknown) {
  settingsAtom.value = Atom.make({
    playtest: { enabled: playtestOn },
    projectSettingsOverrides: {},
  });
  state.session = session;
  state.calls = [];
  state.canOperate = true;
  state.endOutcome = "ended";
}

describe("ForkPlaytestChip", () => {
  it("playtest off on the server: no chip and no request at all", async () => {
    setup(false, SESSION);
    await using screen = await render(<ForkPlaytestChip threadRef={ref} />);
    await expect.element(screen.getByTestId("fork-playtest-chip")).not.toBeInTheDocument();
    expect(state.calls).toEqual([]);
  });

  it("a session on: the chip names the target; End ends it as the server's end, and the chip goes", async () => {
    setup(true, SESSION);
    await using screen = await render(<ForkPlaytestChip threadRef={ref} />);
    await expect
      .element(screen.getByTestId("fork-playtest-chip"))
      .toHaveTextContent("Playtest: dev-1End");
    await userEvent.click(screen.getByRole("button", { name: "End the playtest on dev-1" }));
    await expect.element(screen.getByTestId("fork-playtest-chip")).not.toBeInTheDocument();
    expect(state.calls.filter((call) => call.label === "end")).toEqual([
      { label: "end", input: { threadId: "thread-a" } },
    ]);
  });

  it("no session (none, or ended another way): no chip", async () => {
    setup(true, null);
    await using screen = await render(<ForkPlaytestChip threadRef={ref} />);
    await expect.poll(() => state.calls.length).toBeGreaterThan(0);
    await expect.element(screen.getByTestId("fork-playtest-chip")).not.toBeInTheDocument();
  });

  it("a connection that cannot operate threads sees the chip without End", async () => {
    setup(true, SESSION);
    state.canOperate = false;
    await using screen = await render(<ForkPlaytestChip threadRef={ref} />);
    await expect
      .element(screen.getByTestId("fork-playtest-chip"))
      .toHaveTextContent("Playtest: dev-1");
    await expect.element(screen.getByRole("button")).not.toBeInTheDocument();
  });
});
