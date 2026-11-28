import { createMemoryHistory } from "@tanstack/react-router";
import { ProviderInstanceId } from "@t3tools/contracts";
import { DEFAULT_CLIENT_SETTINGS, type UnifiedSettings } from "@t3tools/contracts/settings";
import { createModelCapabilities } from "@t3tools/shared/model";
import type * as Context from "effect/Context";
import { expect, it, vi } from "vite-plus/test";
import { page } from "vite-plus/test/browser";

import { AppRoot } from "~/AppRoot";
// The route loads ChatView lazily, and its first transform takes seconds; importing it here keeps
// that out of the test's timeout.
import "~/components/ChatView";
import { __setClientSettingsForTests } from "~/hooks/useSettings";
import { getRouter } from "~/router";
import {
  CODEX_PROVIDER,
  ENVIRONMENT_ID,
  installFakeEnvironment,
  threadWithHistory,
  type FakeDriver,
} from "../../../../test/fakeEnvironment";
import { render } from "../../../../test/render";

// The socket is the only fake layer.
vi.mock(
  "../../../../../../packages/client-runtime/src/connection/driver.ts",
  async (importOriginal) => {
    const [actual, { fakeDriver }, Layer] = await Promise.all([
      importOriginal<{ readonly ConnectionDriver: Context.Key<unknown, FakeDriver> }>(),
      import("../../../../test/fakeEnvironment"),
      import("effect/Layer"),
    ]);
    return { ...actual, layer: Layer.succeed(actual.ConnectionDriver, fakeDriver) };
  },
);

vi.mock(import("~/rpc/requestLatencyState"), async (original) => ({
  ...(await original()),
  trackRpcRequestSent: () => {},
}));

// The fake server sends no settings, so the policy's mode is set where the app reads it.
vi.mock(import("~/hooks/useSettings"), async (original) => {
  const actual = await original();
  const withPolicy = (settings: UnifiedSettings): UnifiedSettings => ({
    ...settings,
    modelPolicy: { mode: "advise", allow: [] },
  });
  return {
    ...actual,
    useEnvironmentSettings: ((environmentId, selector) =>
      actual.useEnvironmentSettings(environmentId, (settings) =>
        selector ? selector(withPolicy(settings)) : withPolicy(settings),
      )) as typeof actual.useEnvironmentSettings,
  };
});

it("warns while the next turn names no effort, until one is picked", async () => {
  const provider = {
    ...CODEX_PROVIDER,
    models: [
      {
        slug: "gpt-5.4",
        name: "GPT-5.4",
        isCustom: false,
        capabilities: createModelCapabilities({
          optionDescriptors: [
            {
              id: "reasoningEffort",
              label: "Reasoning",
              type: "select",
              currentValue: "medium",
              options: [
                { id: "low", label: "Low" },
                { id: "medium", label: "Medium" },
                { id: "high", label: "High" },
              ],
            },
          ],
        }),
      },
    ],
  };
  const thread = {
    ...threadWithHistory("thread-no-effort", 1),
    modelSelection: { instanceId: ProviderInstanceId.make("codex"), model: "gpt-5.4" },
  };
  using _environment = installFakeEnvironment([thread], {
    acceptCommands: true,
    providers: [provider],
  });
  __setClientSettingsForTests({
    ...DEFAULT_CLIENT_SETTINGS,
    onboardingCompletedAt: "2026-01-01T00:00:00.000Z",
  });
  const router = getRouter(
    createMemoryHistory({ initialEntries: [`/${ENVIRONMENT_ID}/${thread.id}`] }),
  );
  await router.load();
  await using _app = await render(<AppRoot router={router} />, { router: false });
  await expect.element(page.getByText("Answer 0 for thread-no-effort.")).toBeVisible();

  const banner = page.getByText("No effort set");
  await expect.element(banner).toBeVisible();
  await expect
    .element(page.getByText("GPT-5.4 runs at its default level.", { exact: false }))
    .toBeVisible();

  // The trigger names the model and, for a model with an effort control, its level.
  await page.getByRole("button", { name: /^GPT-5\.4\b/ }).click();
  await page.getByRole("radio", { name: "High" }).click();
  await expect.element(banner).not.toBeInTheDocument();
});
