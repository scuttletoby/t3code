/**
 * Fork (work panel, fork issue 1136): one bar for the fleet's agent sessions, split per machine
 * (hover a part, or open Per machine), the memory headroom, the CI runners line and each provider
 * account's room left. A part with no answer is grey and says unknown.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { useAtomValue } from "@effect/atom-react";
import type { EnvironmentId, ForkRepoQueue, ThreadId } from "@t3tools/contracts";
import { useState } from "react";

import { cn } from "../../../lib/utils";
import { Tooltip, TooltipPopup, TooltipTrigger } from "../../ui/tooltip";
import {
  agentsTotalText,
  ciRunnersText,
  fleetTotal,
  machineCapacity,
  memoryTotalText,
  partShare,
  partText,
  SOURCE_CI_RUNNERS,
  usageAccounts,
  type MachineCapacity,
} from "./capacity.logic";
import { fleetKey, forkFleetAtom, useForkUsageAccounts } from "./workPanelAtom";

export function CapacityBar(props: {
  readonly environmentId: EnvironmentId;
  readonly threadId: ThreadId;
  readonly repoQueues: ReadonlyArray<ForkRepoQueue>;
}) {
  const fleet = useAtomValue(forkFleetAtom(fleetKey(props.environmentId, props.threadId)));
  const accounts = usageAccounts(useForkUsageAccounts());
  const [expanded, setExpanded] = useState(false);
  const machines = fleet.map(machineCapacity);
  const agents = fleetTotal(machines, (machine) => machine.agents);
  const memory = fleetTotal(machines, (machine) => machine.memoryMb);

  return (
    <div className="flex flex-col gap-1.5" data-testid="fork-work-panel-capacity">
      <p className="text-2xs font-medium text-muted-foreground">Capacity</p>
      {machines.length === 0 ? (
        <p className="text-muted-foreground/70" data-testid="fork-work-panel-no-fleet">
          No machine is known to this device: capacity unknown.
        </p>
      ) : (
        <>
          <p data-testid="fork-work-panel-agents">{agentsTotalText(agents)}</p>
          <div
            className="flex h-2 w-full gap-0.5"
            role="list"
            aria-label="Agent sessions per machine"
          >
            {machines.map((machine) => (
              <MachineSegment key={machine.environmentId} machine={machine} />
            ))}
          </div>
          <p data-testid="fork-work-panel-memory">{memoryTotalText(memory)}</p>
          <button
            type="button"
            className="self-start text-2xs text-muted-foreground underline underline-offset-2"
            aria-expanded={expanded}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? "Hide machines" : "Per machine"}
          </button>
          {expanded ? (
            <ul className="flex flex-col gap-1" aria-label="Machines">
              {machines.map((machine) => (
                <li
                  key={machine.environmentId}
                  data-testid="fork-work-panel-machine"
                  data-state={machine.agents.state}
                  className={cn(
                    "flex flex-col",
                    machine.agents.state === "unknown" &&
                      machine.memoryMb.state === "unknown" &&
                      "text-muted-foreground/70",
                  )}
                >
                  <span className="font-medium">
                    {machine.label}
                    {machine.isThisMachine ? " (this thread's)" : ""}
                  </span>
                  <span className="text-muted-foreground">
                    Agents {partText(machine.agents, "agents")}
                  </span>
                  <span className="text-muted-foreground">
                    Memory {partText(machine.memoryMb, "memory")}
                    {machine.memoryFloorMb === null
                      ? ""
                      : ` · keeps ${Math.round(machine.memoryFloorMb / 1024)} GB free`}
                  </span>
                  <span className="text-2xs text-muted-foreground/80">
                    {machine.agents.source} {machine.memoryMb.source}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </>
      )}
      <Tooltip>
        <TooltipTrigger delay={200} render={<p data-testid="fork-work-panel-ci-runners" />}>
          {ciRunnersText(props.repoQueues)}
        </TooltipTrigger>
        <TooltipPopup side="left">{SOURCE_CI_RUNNERS}</TooltipPopup>
      </Tooltip>
      <p className="text-2xs font-medium text-muted-foreground">Usage left</p>
      {accounts.length === 0 ? (
        <p className="text-muted-foreground/70" data-testid="fork-work-panel-no-usage">
          No provider reports its limits: unknown.
        </p>
      ) : (
        <ul className="flex flex-col gap-1" aria-label="Usage left per account">
          {accounts.map((account) => (
            <li key={account.key} data-testid="fork-work-panel-usage" className="flex flex-col">
              <span className="truncate">{account.name}</span>
              <UsageBar label="Session" left={account.session} />
              <UsageBar label="Weekly" left={account.weekly} />
              {account.reason === null ? null : (
                <span className="text-2xs text-muted-foreground/70">unknown: {account.reason}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function MachineSegment({ machine }: { readonly machine: MachineCapacity }) {
  const share = partShare(machine.agents);
  return (
    <Tooltip>
      <TooltipTrigger
        delay={200}
        render={
          <div
            role="listitem"
            aria-label={`${machine.label}: ${partText(machine.agents, "agents")}`}
            data-state={machine.agents.state}
            className={cn(
              "relative h-full flex-1 overflow-hidden rounded-sm",
              machine.agents.state === "unknown" ? "bg-muted-foreground/25" : "bg-muted",
            )}
          />
        }
      >
        {share === null ? null : (
          <span
            className={cn(
              "absolute inset-y-0 left-0",
              share >= 1 ? "bg-destructive" : share >= 0.75 ? "bg-warning" : "bg-success",
            )}
            style={{ width: `${Math.round(share * 100)}%` }}
          />
        )}
      </TooltipTrigger>
      <TooltipPopup side="left">
        {machine.label}: agents {partText(machine.agents, "agents")}. {machine.agents.source}
      </TooltipPopup>
    </Tooltip>
  );
}

function UsageBar(props: { readonly label: string; readonly left: number | null }) {
  return (
    <span
      className="flex items-center gap-1.5 text-2xs text-muted-foreground"
      data-testid={`fork-work-panel-usage-${props.label.toLowerCase()}`}
      data-state={props.left === null ? "unknown" : "answered"}
    >
      <span className="w-12 shrink-0">{props.label}</span>
      <span
        className={cn(
          "relative h-1 flex-1 overflow-hidden rounded-full",
          props.left === null ? "bg-muted-foreground/25" : "bg-muted",
        )}
      >
        {props.left === null ? null : (
          <span
            className={cn(
              "absolute inset-y-0 left-0",
              props.left <= 10 ? "bg-destructive" : props.left <= 25 ? "bg-warning" : "bg-success",
            )}
            style={{ width: `${props.left}%` }}
          />
        )}
      </span>
      <span className="w-20 shrink-0 text-right">
        {props.left === null ? "unknown" : `${props.left}% left`}
      </span>
    </span>
  );
}
