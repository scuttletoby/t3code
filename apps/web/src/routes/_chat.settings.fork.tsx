import { createFileRoute } from "@tanstack/react-router";
import { ForkSettingsPanel } from "../components/settings/fork/ForkSettingsPanel";

export const Route = createFileRoute("/_chat/settings/fork")({
  component: ForkSettingsPanel,
});
