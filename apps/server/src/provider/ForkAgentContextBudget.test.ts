/**
 * Fork: measures the tools the fork adds (`McpHttpServer.layerForkTools`) the same way
 * AgentContextBudget.test.ts measures upstream's, against the fork's own budget file. Upstream's
 * agentContextBudget.json and its CI check stay upstream's.
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { expect, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import { McpServer } from "effect/ai";

import * as McpHttpServer from "../mcp/McpHttpServer.ts";
import { ThreadManagementService } from "../orchestration-v2/ThreadManagementService.ts";
import { PeerForwarding } from "../peer/PeerForwarding.ts";
import { RemoteDelegation } from "../peer/RemoteDelegation.ts";
import { Playtest } from "../playtest/PlaytestLayer.ts";
import { LaneStore } from "../fork/lanes/LaneStore.ts";
import { LANE_TOOLS } from "../fork/lanes/laneToolsVisibility.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import { Waits } from "../waits/WaitWorker.ts";
import budget from "./forkAgentContextBudget.json" with { type: "json" };

// Registration only stores tool definitions, so the handlers' services are never called.
const toolHandlerServices = Layer.mergeAll(
  Layer.mock(ThreadManagementService)({}),
  Layer.mock(Waits)({}),
  Layer.mock(LaneStore)({}),
  Layer.mock(ServerSettingsService)({}),
  Layer.mock(RemoteDelegation)({}),
  Layer.mock(PeerForwarding)({}),
  Layer.mock(Playtest)({ filePath: "unused" }),
).pipe(Layer.provideMerge(NodeServices.layer));

const utf8Bytes = (text: string) => Buffer.byteLength(text, "utf8");

const layerForkTools = McpHttpServer.layerForkTools.pipe(
  Layer.provideMerge(McpHttpServer.layerInProcessMcpServer),
  Layer.provide(toolHandlerServices),
);

it.effect("fork tool context matches the fork's committed budget", () =>
  Effect.gen(function* () {
    const mcp = yield* McpServer.McpServer;
    // The model sees these fields. outputSchema and annotations are protocol metadata.
    const bytesOf = (tools: typeof mcp.tools) =>
      utf8Bytes(
        JSON.stringify(
          tools.map(({ tool }) => ({
            name: tool.name,
            description: tool.description,
            inputSchema: tool.inputSchema,
          })),
        ),
      );
    // The lane tools are sent only while server.lanes.enabled is on (off by default): the T3 tool
    // rule leaves them out of tools/list (mcpT3ToolFilter.test.ts), so they are counted apart.
    const lanes = mcp.tools.filter(({ tool }) => LANE_TOOLS.has(tool.name));
    expect(lanes.map(({ tool }) => tool.name).toSorted()).toEqual([...LANE_TOOLS].toSorted());
    const measured = {
      mcpToolListJsonBytes: bytesOf(mcp.tools.filter(({ tool }) => !LANE_TOOLS.has(tool.name))),
      laneToolsJsonBytesWhenLanesOn: bytesOf(lanes),
    };

    expect(
      measured,
      "Set forkAgentContextBudget.json to the measured value. Before you cut text to fit, run the evals that cover it (evals/README.md).",
    ).toEqual(budget);
  }).pipe(Effect.provide(layerForkTools)),
);
