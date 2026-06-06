import { describe, expect, it } from "vitest";
import {
  headersToText,
  prepareHeadersForSave,
  validateHeadersText,
  WRITE_ONLY_HEADER,
} from "./upstreamHeaders";

describe("validateHeadersText", () => {
  it("accepts well-formed header lines", () => {
    const result = validateHeadersText("Authorization: secret_ref:token\nX-Custom: value");
    expect(result.errors).toEqual([]);
    expect(result.headers).toEqual({
      Authorization: "secret_ref:token",
      "X-Custom": "value",
    });
  });

  it("reports malformed lines instead of silently dropping them", () => {
    const result = validateHeadersText("Authorization: ok\nAuthoriz\nX-Bad");
    expect(result.errors).toEqual([
      'Line 2: expected "Key: Value" format',
      'Line 3: expected "Key: Value" format',
    ]);
    expect(result.headers).toEqual({ Authorization: "ok" });
  });

  it("preserves partial input in raw text until a colon is typed", () => {
    const partial = "Authoriz";
    const result = validateHeadersText(partial);
    expect(result.errors).toEqual(['Line 1: expected "Key: Value" format']);
    expect(result.headers).toEqual({});
    expect(partial).toBe("Authoriz");
  });
});

describe("headersToText", () => {
  it("round-trips structured headers for initial textarea state", () => {
    const headers = { Authorization: "***" };
    expect(headersToText(headers)).toBe("Authorization: ***");
  });
});

describe("prepareHeadersForSave", () => {
  it("preserves write-only secrets when redacted value is unchanged", () => {
    const baseline = { Authorization: "***" };
    const edited = { Authorization: "***" };
    expect(prepareHeadersForSave(edited, baseline)).toEqual({
      Authorization: WRITE_ONLY_HEADER,
    });
  });

  it("marks new secret_ref values as write-only when replacing a redacted header", () => {
    const baseline = { Authorization: "***" };
    const edited = { Authorization: "secret_ref:new-token" };
    expect(prepareHeadersForSave(edited, baseline)).toEqual({
      Authorization: WRITE_ONLY_HEADER,
    });
  });

  it("sends plain values when the operator replaces a redacted header with a new secret", () => {
    const baseline = { Authorization: "***" };
    const edited = { Authorization: "Bearer new-token" };
    expect(prepareHeadersForSave(edited, baseline)).toEqual({
      Authorization: "Bearer new-token",
    });
  });
});
