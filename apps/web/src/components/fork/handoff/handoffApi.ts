/**
 * Fork (HO5): the web app's two handoff calls to its own server (the primary environment):
 * a thread's handoff state and "Hand off now". A thread on another environment has neither.
 * Auth as the app's other HTTP calls (environments/primary/httpLayer.ts): the session cookie when
 * the page is served by that server, else the desktop's bearer token.
 */
import {
  HANDOFF_NOW_PATH,
  HANDOFF_STATE_PATH,
  type HandoffRouteAnswer,
  type HandoffRouteState,
  type ScopedThreadRef,
} from "@t3tools/contracts";

import { readDesktopPrimaryBearerToken } from "../../../environments/primary/desktopAuth";
import { resolvePrimaryEnvironmentHttpUrl } from "../../../environments/primary/target";
import { readPrimaryEnvironmentId } from "../../../state/environments";

/** The thread lives on the server this app talks to over HTTP. */
function onPrimaryEnvironment(threadRef: ScopedThreadRef): boolean {
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

/** Null when it cannot be read (another environment, an older server, a slow or failed call). */
export async function readHandoffState(
  threadRef: ScopedThreadRef,
  options: { readonly timeoutMs?: number; readonly signal?: AbortSignal } = {},
): Promise<HandoffRouteState | null> {
  if (!onPrimaryEnvironment(threadRef)) return null;
  const signals = [
    ...(options.signal ? [options.signal] : []),
    ...(options.timeoutMs !== undefined ? [AbortSignal.timeout(options.timeoutMs)] : []),
  ];
  try {
    const response = await primaryFetch(HANDOFF_STATE_PATH, {
      method: "GET",
      searchParams: { threadId: threadRef.threadId },
      ...(signals.length > 0 ? { signal: AbortSignal.any(signals) } : {}),
    });
    if (!response.ok) return null;
    return (await response.json()) as HandoffRouteState;
  } catch {
    return null;
  }
}

/** A person's "Hand off now". Throws when the server could not be asked. */
export async function askHandoffNow(threadRef: ScopedThreadRef): Promise<HandoffRouteAnswer> {
  const response = await primaryFetch(HANDOFF_NOW_PATH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ threadId: threadRef.threadId }),
  });
  if (!response.ok) throw new Error(`The server answered ${response.status}.`);
  return (await response.json()) as HandoffRouteAnswer;
}
