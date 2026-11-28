/**
 * Fork (owner questions): the switch `ownerQuestionsPage`, saved on this device. On, the Issues
 * entry opens "Waiting on you" (components/fork/ownerQuestions); off, upstream's Issues page.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { DEFAULT_OWNER_QUESTION_RULE } from "@t3tools/contracts";

import { updateClientSettings, useClientSettings } from "../../../hooks/useSettings";
import { ownerQuestionRuleText } from "../../fork/ownerQuestions/waitingOnYou.logic";
import { Switch } from "../../ui/switch";
import { SettingsRow, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";

export function OwnerQuestionsSettings() {
  const searchableSetting = useSearchableSetting();
  const enabled = useClientSettings((settings) => settings.ownerQuestionsPage);
  return (
    <SettingsSection id="owner-questions-section" title="Issues page">
      <SettingsRow
        {...searchableSetting("owner-questions-page")}
        devicePreference
        description={`On: Issues opens Waiting on you, the open owner questions of every project. Off: upstream's Issues page with every issue. ${ownerQuestionRuleText(DEFAULT_OWNER_QUESTION_RULE)} Saved on this device; on by default.`}
        control={
          <Switch
            checked={enabled}
            onCheckedChange={(checked) =>
              void updateClientSettings({ ownerQuestionsPage: checked })
            }
            aria-label="Issues opens Waiting on you"
          />
        }
      />
    </SettingsSection>
  );
}
