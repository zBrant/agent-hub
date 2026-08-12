// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DiffView } from "@/components/session/DiffView";

describe("diff view", () => {
  afterEach(cleanup);

  it("renders large patches incrementally", () => {
    const patch = Array.from(
      { length: 300 },
      (_, index) => `+line ${index + 1}`,
    ).join("\n");
    render(<DiffView patch={patch} />);

    expect(screen.getByText("250 / 300 lines")).toBeTruthy();
    expect(screen.queryByText("+line 300")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show next 50 lines" }));

    expect(screen.getByText("300 / 300 lines")).toBeTruthy();
    expect(screen.getByText("+line 300")).toBeTruthy();
  });

  it("bounds exceptionally long generated lines", () => {
    render(<DiffView patch={`+${"x".repeat(2_500)}`} />);

    expect(screen.getByText(/501 characters omitted/)).toBeTruthy();
    expect(screen.queryByText(`+${"x".repeat(2_500)}`)).toBeNull();
  });
});
