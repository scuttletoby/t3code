/** Test data only. */
/** The 1.5.18 CLI's `/usage --output-format json` shape, with made-up values. */
export const usageReport = (overrides: Record<string, unknown> = {}) =>
  JSON.stringify({
    conversation_id: "",
    status: "SUCCESS",
    response: "Gemini Models\tWeekly Limit Remaining\t95%",
    duration_seconds: 0,
    num_turns: 0,
    usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    command: {
      name: "usage",
      data: {
        description: "Quota",
        groups: [
          {
            name: "Gemini Models",
            buckets: [
              {
                id: "gemini-weekly",
                name: "Weekly Limit Remaining",
                window: "weekly",
                remaining_fraction: 0.95,
                reset_time: "2026-12-04T20:59:40Z",
              },
              {
                id: "gemini-5h",
                name: "Five Hour Limit Remaining",
                window: "5h",
                remaining_fraction: 0.8,
                reset_time: "2026-11-28T01:59:40Z",
              },
            ],
          },
          {
            name: "Claude and GPT models",
            buckets: [
              {
                id: "3p-weekly",
                name: "Weekly Limit Remaining",
                window: "weekly",
                remaining_fraction: 0,
                reset_time: "2026-12-02T17:21:33Z",
              },
              {
                id: "3p-5h",
                name: "Five Hour Limit Remaining",
                window: "5h",
                remaining_fraction: 1,
                disabled: true,
              },
            ],
          },
        ],
      },
    },
    ...overrides,
  });
