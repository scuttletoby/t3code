/**
 * Fork (fleet usage, fork issue 1146): at the top of the Usage tab's limits, every provider account
 * across the linked machines, once each, with the machines that use it, each window's room left
 * and reset, and where and how long ago it was read. An account or machine with no fresh answer is
 * grey and says unknown and why. Renders nothing while the server has no linked machine.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this section is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { cn } from "../../../lib/utils";
import { fleetUsageView } from "./fleetUsage.logic";
import { useForkFleetUsage } from "./fleetUsageAtom";

export function ForkFleetUsage(props: { readonly now: number }) {
  const view = fleetUsageView(useForkFleetUsage(), props.now);
  if (view === null) return null;
  return (
    <section
      className="space-y-3 rounded-xl border p-4"
      aria-label="Accounts across linked machines"
      data-testid="fork-fleet-usage"
    >
      <h2 className="text-sm font-medium">Across linked machines</h2>
      {view.accounts.length === 0 ? (
        <p className="text-sm text-muted-foreground">No machine reports a provider account.</p>
      ) : (
        <ul className="space-y-3">
          {view.accounts.map((account) => (
            <li
              key={account.key}
              className={cn("space-y-1", !account.known && "text-muted-foreground/70")}
              data-testid="fork-fleet-usage-account"
              data-state={account.known ? "known" : "unknown"}
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h3 className="text-sm font-medium">{account.title}</h3>
                <span className="text-xs text-muted-foreground">{account.machinesText}</span>
              </div>
              {account.windows.map((window) => (
                <div key={window.id} className="space-y-0.5">
                  <div className="flex justify-between gap-3 text-sm text-muted-foreground">
                    <span>{window.label}</span>
                    <span>
                      {window.left}% left{window.resetText ? ` · ${window.resetText}` : ""}
                    </span>
                  </div>
                  <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full rounded-full bg-primary"
                      style={{ width: `${window.left}%` }}
                    />
                  </div>
                </div>
              ))}
              <p className="text-xs text-muted-foreground" data-testid="fork-fleet-usage-detail">
                {account.detail}
              </p>
            </li>
          ))}
        </ul>
      )}
      <ul className="space-y-0.5 border-t pt-2 text-xs" aria-label="Machines">
        {view.machines.map((machine) => (
          <li
            key={machine.environmentId}
            className={machine.known ? "text-muted-foreground" : "text-muted-foreground/70"}
            data-testid="fork-fleet-usage-machine"
            data-state={machine.known ? "answered" : "unknown"}
          >
            {machine.text}
          </li>
        ))}
      </ul>
    </section>
  );
}
