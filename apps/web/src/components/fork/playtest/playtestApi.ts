/**
 * Fork (playtest F5): the web app's two playtest calls to its own server (the primary environment):
 * the thread's session that is on, and the person's End. A thread on another environment has
 * neither. Auth as the app's other HTTP calls (environments/primary/httpLayer.ts): the session
 * cookie when the page is served by that server, else the desktop's bearer token.
 */
import {
  PLAYTEST_END_PATH,
  PLAYTEST_STATE_PATH,
  type PlaytestRouteEndAnswer,
  type PlaytestRouteState,
  type PlaytestChipSession,
  type ScopedThreadRef,
} from "@t3tools/contracts";

import { readDesktopPrimaryBearerToken } from "../../../environments/primary/desktopAuth";
import { resolvePrimaryEnvironmentHttpUrl } from "../../../environments/primary/target";
import { readPrimaryEnvironmentId } from "../../../state/environments";

/** The thread lives on the server this app talks to over HTTP. */
export function onPrimaryEnvironment(threadRef: ScopedThreadRef): boolean {
  return readPrimaryEnvironmentId() === threadRef.environmentId;
}

function sameOriginBrowserPrimary(): boolean {
  if (
    typeof window === "undefined" ||
    window.desktopBridge !== undefined ||
    !window.location.origin.startsWith("http")
  ) {
    return false;
  }
  return new URL(resolvePrimaryEnvironmentHttpUrl("/")).origin === window.location.origin;
}

async function primaryFetch(
  path: string,
  init: RequestInit & { readonly searchParams?: Record<string, string> },
): Promise<Response> {
  const { searchParams, ...rest } = init;
  const url = resolvePrimaryEnvironmentHttpUrl(path, searchParams);
  if (sameOriginBrowserPrimary()) return fetch(url, { ...rest, credentials: "include" });
  const token = await readDesktopPrimaryBearerToken();
  const headers = new Headers(rest.headers);
  if (token) headers.set("authorization", `Bearer ${token}`);
  return fetch(url, { ...rest, headers, credentials: "omit" });
}

/**
 * The thread's session that is on, or null: none, the last one ended, another environment, an
 * older server without the route, or a failed call. A chip is never shown on a guess.
 */
export async function readPlaytestSession(
  threadRef: ScopedThreadRef,
  signal?: AbortSignal,
): Promise<PlaytestChipSession | null> {
  if (!onPrimaryEnvironment(threadRef)) return null;
  try {
    const response = await primaryFetch(PLAYTEST_STATE_PATH, {
      method: "GET",
      searchParams: { threadId: threadRef.threadId },
      ...(signal ? { signal } : {}),
    });
    if (!response.ok) return null;
    const state = (await response.json()) as PlaytestRouteState;
    return state.session ?? null;
  } catch {
    return null;
  }
}

/** The person's End: the tool's `end`. Throws when the server could not be asked. */
export async function endPlaytest(threadRef: ScopedThreadRef): Promise<PlaytestRouteEndAnswer> {
  const response = await primaryFetch(PLAYTEST_END_PATH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ threadId: threadRef.threadId }),
  });
  if (!response.ok) throw new Error(`The server answered ${response.status}.`);
  return (await response.json()) as PlaytestRouteEndAnswer;
}
