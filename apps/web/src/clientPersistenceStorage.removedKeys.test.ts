import { DEFAULT_CLIENT_SETTINGS } from "@t3tools/contracts";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

// Fork-only. An earlier fork build saved `anchorEnvironmentId` in the client settings.
// The setting is gone; settings that still hold it must load (an invalid document throws
// on read), keep the user's other choices, and lose the key on the next save.
const STORAGE_KEY = "t3code:client-settings:v1";

function stubWindow(): Storage {
  const store = new Map<string, string>();
  const localStorage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: (index: number) => [...store.keys()][index] ?? null,
    get length() {
      return store.size;
    },
  } satisfies Storage;
  vi.stubGlobal("window", { localStorage });
  vi.stubGlobal("localStorage", localStorage);
  return localStorage;
}

afterEach(() => {
  vi.resetModules();
  vi.unstubAllGlobals();
});

describe("browser client settings saved with a removed fork key", () => {
  const weights = { "environment-anchor": 100, "environment-other": 0 };

  it.each([
    ["an environment id", "environment-anchor"],
    ["null", null],
    ["a non-string", 42],
  ])("loads when the key holds %s and drops it on the next save", async (_label, anchor) => {
    const storage = stubWindow();
    storage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        anchorEnvironmentId: anchor,
        loadBalancingEnabled: true,
        loadBalancingWeights: weights,
      }),
    );
    const { readBrowserClientSettings, writeBrowserClientSettings } =
      await import("./clientPersistenceStorage");

    const settings = readBrowserClientSettings();
    expect(settings).toEqual({
      ...DEFAULT_CLIENT_SETTINGS,
      loadBalancingEnabled: true,
      loadBalancingWeights: weights,
    });

    writeBrowserClientSettings(settings!);
    expect(JSON.parse(storage.getItem(STORAGE_KEY)!)).not.toHaveProperty("anchorEnvironmentId");
  });
});
