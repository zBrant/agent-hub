// @vitest-environment jsdom

import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { AgentEvent } from "@/api/events";
import { EventFeed } from "@/components/session/EventFeed";

const events: readonly AgentEvent[] = [
  {
    type: "assistant_text",
    run_id: "run_one",
    ts: 1,
    text: "Oldest activity",
  },
  {
    type: "assistant_text",
    run_id: "run_one",
    ts: 2,
    text: "Newest activity",
  },
];

describe("event feed", () => {
  afterEach(cleanup);

  it("keeps its own scroll area and renders the latest activity first", () => {
    render(<EventFeed events={events} />);

    const feed = screen.getByRole("region", { name: "Event feed" });
    const rows = within(feed).getAllByText(/activity$/i);

    expect(rows.map((row) => row.textContent)).toEqual([
      "Newest activity",
      "Oldest activity",
    ]);
    expect(feed.className).toContain("overflow-y-auto");
    expect(screen.getByText("Latest first")).toBeTruthy();
  });
});
