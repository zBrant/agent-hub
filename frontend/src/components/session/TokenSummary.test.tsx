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
});
