/**
 * Fork (HO5): a handoff's first message shows "Handoff N" in place of upstream's "New session"
 * divider. The scope (around upstream's fresh-session row in TimelineUserMessageRow.tsx) knows the
 * message; the row reads the label (V2LifecycleRow.tsx). A handoff message's id is
 * `handoff:<thread>:<run>` (fork/handoff on the server) and its text starts `[T3 handoff N]`.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import { createContext, use, type ReactNode } from "react";

const LabelContext = createContext<string | null>(null);

const NUMBER = /^\[T3 handoff (\d+)\]/;

/** "Handoff N" for a handoff's message, "Handoff" when its number is not in the text; else null. */
export function handoffLabel(
  item: { readonly type: string; readonly messageId?: string; readonly text?: string } | undefined,
): string | null {
  if (item?.type !== "user_message" || !item.messageId?.startsWith("handoff:")) return null;
  const number = NUMBER.exec(item.text ?? "")?.[1];
  return number === undefined ? "Handoff" : `Handoff ${number}`;
}

export function Scope(props: {
  readonly row: {
    readonly projectedItem?:
      | {
          readonly item?: {
            readonly type: string;
            readonly messageId?: string;
            readonly text?: string;
          };
        }
      | null
      | undefined;
  };
  readonly children: ReactNode;
}) {
  return (
    <LabelContext value={handoffLabel(props.row.projectedItem?.item)}>
      {props.children}
    </LabelContext>
  );
}

/** The handoff label for the fresh-session row inside a scope; null outside one or for another message. */
export function useLabel(): string | null {
  return use(LabelContext);
}
