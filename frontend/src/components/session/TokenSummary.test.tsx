// @vitest-environment jsdom

import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TokenSummary } from "@/components/session/TokenSummary";

afterEach(cleanup);

describe("token summary", () => {
  it("renders all four fields from the real Phase 1 acceptance payload", () => {
    render(
      <TokenSummary
        summary={{
          run_id: "run_01KZD7M9H51BZTDEWHQVP9TFZT",
          trusted: true,
          tokens: {
            input_tokens: 9_060,
            output_tokens: 485,
            cache_read_tokens: 62_208,
            cache_write_tokens: 0,
            total_tokens: 71_753,
          },
          estimated_equivalent_cost_usd: 0.045477,
          cost_complete: true,
        }}
      />,
    );

    const usage = screen.getByRole("region", { name: "Usage" });
    expect(within(usage).getByText("72K tokens")).toBeTruthy();
    expect(within(usage).getByText("62K")).toBeTruthy();
    expect(within(usage).getByText("9.1K")).toBeTruthy();
    expect(within(usage).getByText("485")).toBeTruthy();
    expect(within(usage).getByText("0")).toBeTruthy();
    expect(within(usage).getByText("$0.0455")).toBeTruthy();
  });

  it.each([
    {
      runId: "run_01KZFTE126TS0270CJ5EZRZMJV",
      tokens: {
        input_tokens: 9_467,
        output_tokens: 709,
        cache_read_tokens: 62_208,
        cache_write_tokens: 0,
        total_tokens: 72_384,
      },
      cost: 0.0498545,
      expected: ["72K tokens", "62K", "9.5K", "709", "0", "$0.0499"],
    },
    {
      runId: "run_01KZFTE12WC6KD2P1AKT8TJARY",
      tokens: {
        input_tokens: 7_978,
        output_tokens: 573,
        cache_read_tokens: 49_152,
        cache_write_tokens: 0,
        total_tokens: 57_703,
      },
      cost: 0.040828,
      expected: ["58K tokens", "49K", "8K", "573", "0", "$0.0408"],
    },
  ])("renders the real Phase 2 payload for $runId", (fixture) => {
    render(
      <TokenSummary
        summary={{
          run_id: fixture.runId,
          trusted: true,
          tokens: fixture.tokens,
          estimated_equivalent_cost_usd: fixture.cost,
          cost_complete: true,
        }}
      />,
    );

    const usage = screen.getByRole("region", { name: "Usage" });
    for (const value of fixture.expected) {
      expect(within(usage).getByText(value)).toBeTruthy();
    }
  });
});
