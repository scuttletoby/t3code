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
import { Waits } from "../waits/WaitWorker.ts";
import budget from "./forkAgentContextBudget.json" with { type: "json" };

// Registration only stores tool definitions, so the handlers' services are never called.
const toolHandlerServices = Layer.mergeAll(
  Layer.mock(ThreadManagementService)({}),
  Layer.mock(Waits)({}),
).pipe(Layer.provideMerge(NodeServices.layer));

const utf8Bytes = (text: string) => Buffer.byteLength(text, "utf8");

const layerForkTools = McpHttpServer.layerForkTools.pipe(
  Layer.provideMerge(McpHttpServer.layerInProcessMcpServer),
  Layer.provide(toolHandlerServices),
);

it.effect("fork tool context matches the fork's committed budget", () =>
  Effect.gen(function* () {
    const mcp = yield* McpServer.McpServer;
    const measured = {
      // The model sees these fields. outputSchema and annotations are protocol metadata.
      mcpToolListJsonBytes: utf8Bytes(
        JSON.stringify(
          mcp.tools.map(({ tool }) => ({
            name: tool.name,
            description: tool.description,
            inputSchema: tool.inputSchema,
          })),
        ),
      ),
    };

    expect(
      measured,
      "Set forkAgentContextBudget.json to the measured value. Before you cut text to fit, run the evals that cover it (evals/README.md).",
    ).toEqual(budget);
  }).pipe(Effect.provide(layerForkTools)),
);
