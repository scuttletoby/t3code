/** Fork: a text field that saves on blur or Enter (multi-line: blur only) and shows why a value cannot be saved. */
import { useState } from "react";

import { Input } from "../../ui/input";
import { Textarea } from "../../ui/textarea";

export function CommitTextField({
  value,
  onCommit,
  ariaLabel,
  placeholder,
  multiline = false,
  disabled = false,
}: {
  readonly value: string;
  /** Saves `text`; returns why it cannot be saved, or null. `revert` puts the stored value back. */
  readonly onCommit: (text: string, revert: () => void) => string | null;
  readonly ariaLabel: string;
  readonly placeholder?: string;
  readonly multiline?: boolean;
  readonly disabled?: boolean;
}) {
  const [draft, setDraft] = useState({ saved: value, text: value });
  const [error, setError] = useState<string | null>(null);
  if (draft.saved !== value) setDraft({ saved: value, text: value });

  const commit = () => {
    if (draft.text === value) return;
    setError(onCommit(draft.text, () => setDraft({ saved: value, text: value })));
  };
  const change = (text: string) => {
    setError(null);
    setDraft({ ...draft, text });
  };

  return (
    <div className="flex w-full flex-col gap-1 sm:w-72">
      {multiline ? (
        <Textarea
          aria-label={ariaLabel}
          placeholder={placeholder}
          disabled={disabled}
          value={draft.text}
          onChange={(event) => change(event.target.value)}
          onBlur={commit}
        />
      ) : (
        <Input
          size="sm"
          aria-label={ariaLabel}
          placeholder={placeholder}
          disabled={disabled}
          value={draft.text}
          onChange={(event) => change(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") commit();
          }}
        />
      )}
      {error !== null ? (
        <p role="alert" className="text-xs text-destructive-foreground">
          {error}
        </p>
      ) : null}
    </div>
  );
}
