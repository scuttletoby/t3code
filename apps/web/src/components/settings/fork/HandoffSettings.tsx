/**
 * Fork: handoff instead of compaction (`handoff`). A project's value replaces the environment's
 * whole, so a change on a project screen saves the whole value it shows as the project's own. Every
 * write is an ordinary settings patch from the person at the screen; agents cannot change it.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import {
  DEFAULT_HANDOFF_SETTINGS,
  HANDOFF_MIN_TOKENS,
  type HandoffMode,
  type HandoffSettings as HandoffValue,
  type HandoffSource,
} from "@t3tools/contracts";
import { useState } from "react";

import { useThreadShells } from "../../../state/entities";
import { Button } from "../../ui/button";
import { Input } from "../../ui/input";
import { Select, SelectItem, SelectPopup, SelectTrigger, SelectValue } from "../../ui/select";
import { AdvancedSettings } from "../AdvancedSettings";
import { IntervalSecondsField } from "../IntervalSecondsField";
import { SettingResetButton, SettingsRow, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import {
  useClearScopedSettings,
  useScopedSettingSource,
  useScopedSettings,
  useScopedSettingsMixed,
  useUpdateScopedSettings,
} from "../useScopedSettings";
import { CommitTextField } from "./CommitTextField";
import {
  HANDOFF_MODE_OPTIONS,
  formatArgvLines,
  handoffModeLabel,
  handoffSourceProblem,
  handoffThreadRows,
  parseArgvLines,
  threadModeProblem,
  withThreadMode,
} from "./handoffSettings.logic";

const MIXED =
  "The selected environments or projects have different handoff settings. Select one to edit them.";

const SOURCE_LABELS: Record<HandoffSource, string> = {
  agent: "The old session",
  command: "A command",
};

function ModeSelect({
  value,
  label,
  onChange,
}: {
  readonly value: HandoffMode;
  readonly label: string;
  readonly onChange: (mode: HandoffMode) => void;
}) {
  return (
    <Select
      value={value}
      onValueChange={(next) => {
        if (next !== null && next !== value) onChange(next as HandoffMode);
      }}
    >
      <SelectTrigger size="sm" className="w-full sm:w-32" aria-label={label}>
        <SelectValue>{handoffModeLabel(value)}</SelectValue>
      </SelectTrigger>
      <SelectPopup align="end" alignItemWithTrigger={false}>
        {HANDOFF_MODE_OPTIONS.map((option) => (
          <SelectItem hideIndicator key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectPopup>
    </Select>
  );
}

export function HandoffSettings({ isProjectScope }: { readonly isProjectScope: boolean }) {
  const searchableSetting = useSearchableSetting();
  const updateSettings = useUpdateScopedSettings();
  const clearScopedSettings = useClearScopedSettings();
  const source = useScopedSettingSource(["handoff"]);
  const mixed = useScopedSettingsMixed(["handoff"]) || source === "mixed";
  const shown = useScopedSettings((settings) => settings.handoff);
  const titles = new Map(useThreadShells().map((thread) => [thread.id as string, thread.title]));
  const [threadDraft, setThreadDraft] = useState<{ id: string; mode: HandoffMode }>({
    id: "",
    mode: "compact",
  });
  const [threadError, setThreadError] = useState<string | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);

  const save = (next: HandoffValue, onFailed?: () => void) =>
    updateSettings({ handoff: next }, onFailed ? { onFailed } : undefined);
  const option = HANDOFF_MODE_OPTIONS.find((candidate) => candidate.value === shown.mode);
  const scopeNote = !isProjectScope
    ? ""
    : source === "project"
      ? " This project's value replaces the environment's whole."
      : " A change here gives this project its own copy of the environment's value.";
  const rows = handoffThreadRows(shown.threads, titles);
  const addThread = () => {
    const problem = threadModeProblem(shown, threadDraft.id);
    if (problem !== null) {
      setThreadError(problem);
      return;
    }
    setThreadError(null);
    save(withThreadMode(shown, threadDraft.id.trim(), threadDraft.mode));
    setThreadDraft({ ...threadDraft, id: "" });
  };

  if (mixed) {
    return (
      <SettingsSection id="handoff-section" title="Handoff">
        <SettingsRow
          serverScoped
          {...searchableSetting("handoff")}
          settingKeys={["handoff"]}
          mixed
          description={MIXED}
        />
      </SettingsSection>
    );
  }

  return (
    <SettingsSection id="handoff-section" title="Handoff">
      <SettingsRow
        serverScoped
        {...searchableSetting("handoff")}
        settingKeys={["handoff"]}
        onResetOverride={() => clearScopedSettings(["handoff"])}
        description={`${option?.description ?? ""}${scopeNote}`}
        resetAction={
          !isProjectScope && shown.mode !== DEFAULT_HANDOFF_SETTINGS.mode ? (
            <SettingResetButton
              ariaLabel="Reset handoff mode to default"
              onClick={() => save({ ...shown, mode: DEFAULT_HANDOFF_SETTINGS.mode })}
            />
          ) : null
        }
        control={
          <ModeSelect
            value={shown.mode}
            label="Handoff mode"
            onChange={(mode) => save({ ...shown, mode })}
          />
        }
      />
      <SettingsRow
        {...searchableSetting("handoff-threads")}
        settingKeys={["handoff"]}
        description={
          rows.length === 0
            ? "No thread has its own mode. A thread's own mode wins over the mode above."
            : "A thread's own mode wins over the mode above, also over a project's."
        }
      >
        <div className="grid gap-2 pb-2">
          {rows.map((row) => (
            <div key={row.threadId} className="flex flex-wrap items-center gap-2 text-xs">
              <span className="min-w-0 flex-1 truncate">
                {row.title ?? <span className="text-muted-foreground">Unknown thread</span>}{" "}
                <span className="text-muted-foreground">{row.threadId}</span>
              </span>
              <ModeSelect
                value={row.mode}
                label={`Handoff mode for thread ${row.threadId}`}
                onChange={(mode) => save(withThreadMode(shown, row.threadId, mode))}
              />
              <Button
                size="xs"
                variant="destructive-outline"
                aria-label={`Remove the handoff mode of thread ${row.threadId}`}
                onClick={() => save(withThreadMode(shown, row.threadId, null))}
              >
                Remove
              </Button>
            </div>
          ))}
          <div className="flex flex-wrap items-center gap-2">
            <Input
              size="sm"
              className="min-w-0 flex-1"
              aria-label="Thread id"
              placeholder="Thread id"
              value={threadDraft.id}
              onChange={(event) => {
                setThreadError(null);
                setThreadDraft({ ...threadDraft, id: event.target.value });
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") addThread();
              }}
            />
            <ModeSelect
              value={threadDraft.mode}
              label="Mode for the new thread"
              onChange={(mode) => setThreadDraft({ ...threadDraft, mode })}
            />
            <Button size="xs" variant="outline" onClick={addThread}>
              Add thread
            </Button>
          </div>
          {threadError !== null ? (
            <p role="alert" className="text-xs text-destructive-foreground">
              {threadError}
            </p>
          ) : null}
        </div>
      </SettingsRow>
      <AdvancedSettings
        targetIds={["handoff-size", "handoff-idle", "handoff-source", "handoff-instructions"]}
      >
        <SettingsRow
          {...searchableSetting("handoff-size")}
          settingKeys={["handoff"]}
          description="A turn that ends with the thread at this size hands off."
          control={
            <div className="flex items-center gap-2">
              <IntervalSecondsField
                labels={{
                  decrease: "Decrease handoff size",
                  input: "Handoff size in tokens",
                  increase: "Increase handoff size",
                }}
                value={shown.sizeTokens}
                min={HANDOFF_MIN_TOKENS}
                step={10_000}
                onCommit={(value, options) =>
                  save(
                    { ...shown, sizeTokens: Math.max(HANDOFF_MIN_TOKENS, value) },
                    options.onFailed,
                  )
                }
              />
              <span className="text-xs text-muted-foreground">tokens</span>
            </div>
          }
        />
        <SettingsRow
          {...searchableSetting("handoff-idle")}
          settingKeys={["handoff"]}
          description={`The smallest idle thread handed off before its prompt cache runs out. 0: never; otherwise at least ${HANDOFF_MIN_TOKENS.toLocaleString("en")}.`}
          control={
            <div className="flex items-center gap-2">
              <IntervalSecondsField
                labels={{
                  decrease: "Decrease idle handoff size",
                  input: "Idle handoff size in tokens",
                  increase: "Increase idle handoff size",
                }}
                value={shown.idleTokens}
                min={0}
                step={5_000}
                onCommit={(value, options) =>
                  save(
                    {
                      ...shown,
                      idleTokens: value === 0 ? 0 : Math.max(HANDOFF_MIN_TOKENS, value),
                    },
                    options.onFailed,
                  )
                }
              />
              <span className="text-xs text-muted-foreground">tokens</span>
            </div>
          }
        />
        <SettingsRow
          {...searchableSetting("handoff-source")}
          settingKeys={["handoff"]}
          description="Who writes the handoff: the old session in one turn, or a read-only program (one argument per line, its full path first). Today it sets how early an idle handoff starts."
          control={
            <Select
              value={shown.source}
              onValueChange={(next) => {
                if (next === null || next === shown.source) return;
                const problem = handoffSourceProblem(next as HandoffSource, shown.command);
                setSourceError(problem);
                if (problem === null) save({ ...shown, source: next as HandoffSource });
              }}
            >
              <SelectTrigger size="sm" className="w-full sm:w-40" aria-label="Handoff written by">
                <SelectValue>{SOURCE_LABELS[shown.source]}</SelectValue>
              </SelectTrigger>
              <SelectPopup align="end" alignItemWithTrigger={false}>
                {(Object.keys(SOURCE_LABELS) as HandoffSource[]).map((value) => (
                  <SelectItem hideIndicator key={value} value={value}>
                    {SOURCE_LABELS[value]}
                  </SelectItem>
                ))}
              </SelectPopup>
            </Select>
          }
        >
          <div className="grid gap-1 pb-2">
            <CommitTextField
              multiline
              value={formatArgvLines(shown.command)}
              ariaLabel="Handoff command, one argument per line"
              placeholder={"/usr/local/bin/write-handoff\n--thread"}
              onCommit={(text, revert) => {
                const command = parseArgvLines(text);
                const problem = handoffSourceProblem(shown.source, command);
                if (problem !== null) return problem;
                const { command: _old, ...rest } = shown;
                save(command === undefined ? rest : { ...rest, command }, revert);
                return null;
              }}
            />
            {sourceError !== null ? (
              <p role="alert" className="text-xs text-destructive-foreground">
                {sourceError}
              </p>
            ) : null}
          </div>
        </SettingsRow>
        <SettingsRow
          {...searchableSetting("handoff-instructions")}
          settingKeys={["handoff"]}
          description="What the old session is asked to write, when it writes the handoff. Empty: T3's built-in text."
        >
          <div className="pb-2">
            <CommitTextField
              multiline
              value={shown.instructions}
              ariaLabel="Handoff instructions"
              onCommit={(text, revert) => {
                if (text.trim().length > 8000) return "At most 8000 characters.";
                save({ ...shown, instructions: text.trim() }, revert);
                return null;
              }}
            />
          </div>
        </SettingsRow>
      </AdvancedSettings>
    </SettingsSection>
  );
}
