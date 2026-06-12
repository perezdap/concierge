import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { FIELD_HELP, FieldHelp } from "./fieldHelp";

describe("FieldHelp", () => {
  afterEach(() => {
    cleanup();
  });

  it("renders help trigger with tooltip text from the shared map", () => {
    render(<FieldHelp helpKey="upstreamId" />);
    expect(screen.getByRole("button", { name: /Help: upstreamId/i })).toBeInTheDocument();
    expect(screen.getByRole("tooltip")).toHaveTextContent(FIELD_HELP.upstreamId);
  });

  it("documents Names accepts canonical and upstream-side names but not upstream id", () => {
    expect(FIELD_HELP.selectorNames).toMatch(/canonical/i);
    expect(FIELD_HELP.selectorNames).toMatch(/upstream-side name/i);
    expect(FIELD_HELP.selectorNames).toMatch(/not an upstream id/i);
  });

  it("assigns unique tooltip ids when the same help key appears more than once", () => {
    render(
      <>
        <FieldHelp helpKey="selectorTags" />
        <FieldHelp helpKey="selectorTags" />
      </>,
    );
    const ids = screen.getAllByRole("tooltip").map((node) => node.id);
    expect(new Set(ids).size).toBe(2);
  });
});
