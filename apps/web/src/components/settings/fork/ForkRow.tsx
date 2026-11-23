/**
 * Fork: a settings row whose every control follows the row's lock. Upstream's row greys out only its
 * main control under an administrator's lock, and its sub-controls only when it is server-scoped;
 * the Fork page's rows keep switches, lists and text boxes below the title, so those are made inert
 * too. The server refuses such a write anyway; this only keeps a click from ending in an error.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own settings are English only; upstream's translation catalogs hold no fork keys. */
import { isManagedSettingPath, type ServerSettings } from "@t3tools/contracts";
import { type ComponentProps, type ReactNode, useState } from "react";

import { useEnvironmentProviders } from "../../../state/environments";
import { EMPTY_SERVER_PROVIDERS } from "../../../state/server";
import { IntervalSecondsField } from "../IntervalSecondsField";
import { useSettingsScope } from "../SettingsScopeContext";
import { SettingResetButton, SettingsRow } from "../settingsLayout";
import { useScopedSettingsWriteAllowed } from "../useScopedSettings";
import { subRowResetValue } from "./forkRowReset.logic";

/** The selected environments, the primary's first. */
function useSelectedEnvironments() {
  const { environment, connectedEnvironments } = useSettingsScope();
  return [
    ...(environment ? [environment] : []),
    ...connectedEnvironments.filter(
      (candidate) => candidate.environmentId !== environment?.environmentId,
    ),
  ];
}

/** Every selected environment's accounts, the primary's first, as the agent slots rows list them. */
export function useSelectedProviders() {
  const providersByEnvironment = useEnvironmentProviders();
  return useSelectedEnvironments().flatMap(
    (candidate) => providersByEnvironment.get(candidate.environmentId) ?? EMPTY_SERVER_PROVIDERS,
  );
}

/**
 * {@link useSelectedProviders}, each with the environment it is on: a check that a machine's server
 * makes on its own catalog alone (the model policy's alias flag) reads each machine apart.
 */
export function useSelectedProvidersByEnvironment() {
  const providersByEnvironment = useEnvironmentProviders();
  return useSelectedEnvironments().flatMap((candidate) =>
    (providersByEnvironment.get(candidate.environmentId) ?? EMPTY_SERVER_PROVIDERS).map(
      (provider) => ({ ...provider, environmentId: candidate.environmentId }),
    ),
  );
}

/** Why the row's setting cannot be changed here: an administrator's lock or no write permission. */
export function useForkRowLock(paths: ReadonlyArray<string>): string | undefined {
  const { connectedEnvironments } = useSettingsScope();
  const writeAllowed = useScopedSettingsWriteAllowed();
  const organization = connectedEnvironments
    .map((environment) => environment.serverConfig?.managedPolicy)
    .find(
      (policy) => policy !== undefined && paths.some((path) => isManagedSettingPath(policy, path)),
    )?.organization;
  if (organization !== undefined) return `Managed by ${organization}.`;
  if (!writeAllowed) return "This connection lacks permission to change settings here.";
  return undefined;
}

/** A settings row locked by `lockPaths` (setting keys or dot paths): its control and children alike. */
export function ForkRow({
  lockPaths,
  children,
  ...props
}: ComponentProps<typeof SettingsRow> & { readonly lockPaths: ReadonlyArray<string> }) {
  const lock = useForkRowLock(lockPaths);
  return (
    <SettingsRow {...props} {...(lock === undefined ? {} : { managed: lock })}>
      {lock === undefined || !children ? (
        children
      ) : (
        <div inert className="opacity-50">
          {children}
        </div>
      )}
    </SettingsRow>
  );
}

/**
 * Upstream's number field, with the value bounded before it is saved. When the bound changes the
 * typed number, the field starts again from the saved value; otherwise it would keep showing the
 * typed number, as the saved value (and so the field's prop) may not change.
 */
export function BoundedNumberField({
  bound,
  onCommit,
  ...props
}: ComponentProps<typeof IntervalSecondsField> & { readonly bound: (value: number) => number }) {
  const [resets, setResets] = useState(0);
  return (
    <IntervalSecondsField
      key={resets}
      {...props}
      onCommit={(value, options) => {
        const bounded = bound(value);
        if (bounded !== value) setResets((count) => count + 1);
        onCommit(bounded, options);
      }}
    />
  );
}

/**
 * A sub-row's reset on a project screen whose value is the project's own: it puts back only that
 * row's fields from the environment's value and keeps the rest of the override (upstream's row
 * reset would clear the whole override). Null where there is nothing to reset (see
 * {@link subRowResetValue}).
 */
export function useSubRowReset<K extends "handoff" | "playtest">(
  key: K,
  shown: ServerSettings[K],
  isOwn: boolean,
  save: (next: ServerSettings[K]) => void,
) {
  const { targets, connectedEnvironments } = useSettingsScope();
  const target = targets[0];
  const environmentValue =
    target === undefined || target.projectId === null
      ? undefined
      : connectedEnvironments.find(
          (environment) => environment.environmentId === target.environmentId,
        )?.serverConfig?.settings[key];
  return (keys: ReadonlyArray<keyof ServerSettings[K]>, ariaLabel: string) => {
    if (!isOwn || environmentValue === undefined) return null;
    const next = subRowResetValue(key, shown, environmentValue, keys);
    return next === null ? null : (
      <SettingResetButton
        ariaLabel={ariaLabel}
        tooltip="Reset to the environment's value"
        onClick={() => save(next)}
      />
    );
  };
}

/**
 * A dropdown row's help: a lead line, then one line for every option (the chosen one stands out),
 * then a closing line. Upstream's select items hold only a label, so the choices are explained here.
 */
export function OptionLines({
  lead,
  options,
  selected,
  tail,
}: {
  readonly lead?: ReactNode;
  readonly options: ReadonlyArray<{ readonly value: string; readonly description: string }>;
  readonly selected?: string | null | undefined;
  readonly tail: ReactNode;
}) {
  return (
    <>
      {lead ? <span className="block">{lead}</span> : null}
      {options.map((option) => (
        <span
          key={option.value}
          className={option.value === selected ? "block font-medium text-foreground" : "block"}
        >
          {option.description}
        </span>
      ))}
      <span className="block">{tail}</span>
    </>
  );
}
