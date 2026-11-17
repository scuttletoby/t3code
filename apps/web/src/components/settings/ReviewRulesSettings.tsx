/**
 * Fork: the review rules panel (`reviewRules`). Shows whether the rules are on and the values a
 * review lens would run with; one button turns the recommended preset on, one turns it off. Every
 * write is an ordinary settings patch from the person at the screen; agents cannot change it.
 */
import type { ReviewRules } from "@t3tools/contracts";
import { useState } from "react";

import {
  AlertDialog,
  AlertDialogClose,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogPopup,
  AlertDialogTitle,
} from "../ui/alert-dialog";
import { Button } from "../ui/button";
import {
  REVIEW_RULES_BUTTON_VALUE,
  type ReviewRulesPresetChoice,
  effectiveReviewRulesRows,
  planReviewRulesOverrideClear,
  planReviewRulesWrite,
  reviewRulesDescription,
  reviewRulesSaveError,
  reviewRulesState,
  reviewRulesSummary,
  unreadableReviewRulesText,
} from "./reviewRulesSettings.logic";
import { useSettingsScope } from "./SettingsScopeContext";
import { SettingsRow, SettingsSection } from "./settingsLayout";
import {
  useClearScopedSettings,
  useScopedSettingSource,
  useScopedSettingsMixed,
  useUpdateScopedSettings,
} from "./useScopedSettings";

interface PendingConfirm {
  readonly title: string;
  readonly reasons: ReadonlyArray<string>;
  readonly action: string;
  readonly run: () => void;
}

const BUTTON_LABELS: Record<ReviewRulesPresetChoice, string> = {
  recommended: "Use recommended review rules",
  off: "Turn off",
};

export function ReviewRulesSettings({ isProjectScope }: { readonly isProjectScope: boolean }) {
  const { targets } = useSettingsScope();
  const updateSettings = useUpdateScopedSettings();
  const clearScopedSettings = useClearScopedSettings();
  const valuesMixed = useScopedSettingsMixed(["reviewRules"]);
  const source = useScopedSettingSource(["reviewRules"]);
  const mixed = valuesMixed || source === "mixed";
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<PendingConfirm | null>(null);

  // The value each target shows: in a project, its override when it has one, else the environment's.
  const shown: ReviewRules | undefined = targets[0]?.settings.reviewRules;
  // The values a save replaces: in a project, only its own override (none: a new one is written).
  const stored = targets.map((target) =>
    isProjectScope
      ? target.sources.reviewRules === "project"
        ? target.settings.reviewRules
        : undefined
      : target.settings.reviewRules,
  );
  const projectOverrides = isProjectScope && source === "project";

  const save = (choice: ReviewRulesPresetChoice) => {
    if (saving) return;
    setSaving(true);
    setSaveError(null);
    updateSettings(
      { reviewRules: { ...REVIEW_RULES_BUTTON_VALUE[choice] } },
      {
        onSaved: () => setSaving(false),
        onFailed: (failure) => {
          setSaving(false);
          setSaveError(reviewRulesSaveError(failure));
        },
      },
    );
  };

  const choose = (choice: ReviewRulesPresetChoice) => {
    const plan = planReviewRulesWrite(stored, choice);
    if (plan.kind === "same") return;
    if (plan.kind === "write") {
      save(choice);
      return;
    }
    setConfirm({
      title: choice === "recommended" ? "Replace with the recommended preset?" : "Turn off?",
      reasons: plan.reasons,
      action: choice === "recommended" ? "Replace" : "Turn off",
      run: () => save(choice),
    });
  };

  const clearOverride = () => {
    const plan = planReviewRulesOverrideClear(stored);
    if (plan.kind !== "confirm") {
      clearScopedSettings(["reviewRules"]);
      return;
    }
    setConfirm({
      title: "Use the environment's review rules?",
      reasons: plan.reasons,
      action: "Use the environment's",
      run: () => clearScopedSettings(["reviewRules"]),
    });
  };

  const buttonFor = (choice: ReviewRulesPresetChoice) => (
    <Button
      size="xs"
      variant={choice === "recommended" ? "default" : "outline"}
      disabled={saving || planReviewRulesWrite(stored, choice).kind === "same"}
      onClick={() => choose(choice)}
    >
      {BUTTON_LABELS[choice]}
    </Button>
  );

  const state = reviewRulesState(shown);
  const unreadableText = mixed ? null : unreadableReviewRulesText(shown);
  const rows = mixed ? [] : effectiveReviewRulesRows(shown);

  return (
    <SettingsSection id="review-rules-section" title="Review rules">
      <SettingsRow
        serverScoped
        id="review-rules"
        title="Review rules"
        settingKeys={["reviewRules"]}
        mixed={mixed}
        onResetOverride={clearOverride}
        status={mixed ? null : reviewRulesSummary(shown)}
        description={
          mixed
            ? "The selected environments or checkouts have different review rules. Select one to see them; a button here saves the same value on every one."
            : reviewRulesDescription(shown, { isProjectScope, projectOverrides })
        }
        control={
          <div className="flex flex-wrap items-center gap-2">
            {buttonFor("recommended")}
            {buttonFor("off")}
          </div>
        }
      >
        {saveError !== null ? (
          <p role="alert" className="pb-2 text-xs text-destructive-foreground">
            {saveError}
          </p>
        ) : null}
        {state === "unreadable" && unreadableText !== null ? (
          <div role="status" className="mb-2 rounded-lg border border-warning/40 p-3 text-xs">
            <p className="font-medium text-foreground">
              The stored review rules are not valid, so they read as off.
            </p>
            <p className="mt-1 text-muted-foreground">
              Fix them in the settings file, or replace them with a button above (you are asked
              first). As stored:
            </p>
            <code className="mt-1 block break-all text-muted-foreground">{unreadableText}</code>
          </div>
        ) : null}
        {rows.length > 0 ? (
          <dl role="group" aria-label="Effective review rules" className="mb-2 grid gap-2 text-xs">
            {rows.map((row) => (
              <div key={row.key} className="grid gap-0.5">
                <dt className="flex items-center gap-2 text-muted-foreground">
                  {row.label}
                  <span className="rounded border px-1 text-3xs uppercase tracking-wide">
                    {row.fromPreset ? "preset" : "set by hand"}
                  </span>
                </dt>
                <dd className="break-words text-foreground">{row.value}</dd>
              </div>
            ))}
          </dl>
        ) : null}
      </SettingsRow>
      <AlertDialog
        open={confirm !== null}
        onOpenChange={(open) => {
          if (!open) setConfirm(null);
        }}
      >
        {confirm !== null ? (
          <AlertDialogPopup>
            <AlertDialogHeader>
              <AlertDialogTitle>{confirm.title}</AlertDialogTitle>
              {confirm.reasons.map((reason) => (
                <AlertDialogDescription key={reason}>{reason}</AlertDialogDescription>
              ))}
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogClose render={<Button variant="outline" />}>Cancel</AlertDialogClose>
              <Button
                variant="destructive"
                onClick={() => {
                  const run = confirm.run;
                  setConfirm(null);
                  run();
                }}
              >
                {confirm.action}
              </Button>
            </AlertDialogFooter>
          </AlertDialogPopup>
        ) : null}
      </AlertDialog>
    </SettingsSection>
  );
}
