import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Upstreams from "./Upstreams";

vi.mock("../api", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      message: string,
      readonly status: number,
      readonly body: unknown,
    ) {
      super(message);
      this.name = "ApiError";
    }
  },
  api: {
    listUpstreams: vi.fn(),
    configDraft: vi.fn(),
    applyUpstreamsDraft: vi.fn(),
    createUpstream: vi.fn(),
    getUpstream: vi.fn(),
    updateUpstream: vi.fn(),
    deleteUpstream: vi.fn(),
    testUpstreamConnection: vi.fn(),
    refreshUpstreamCatalog: vi.fn(),
  },
}));

vi.mock("../../components/PendingChanges", () => ({
  PendingChanges: () => null,
}));

import { api } from "../api";

const mockedApi = vi.mocked(api);

describe("Upstreams headers textarea", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    mockedApi.listUpstreams.mockResolvedValue({ upstreams: [], source: "draft" });
    mockedApi.configDraft.mockResolvedValue({ config: null, version: null });
  });

  async function openNewStreamableUpstream() {
    render(<Upstreams />);
    await waitFor(() => expect(mockedApi.listUpstreams).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Add upstream" }));
    fireEvent.change(screen.getByLabelText("Transport"), {
      target: { value: "streamable_http" },
    });
    fireEvent.change(screen.getByLabelText("ID"), { target: { value: "demo" } });
    return screen.getByPlaceholderText("Authorization: secret_ref:...");
  }

  it("shows each keystroke while typing a header line", async () => {
    const textarea = await openNewStreamableUpstream();
    for (const ch of "Authoriz") {
      fireEvent.change(textarea, { target: { value: (textarea as HTMLTextAreaElement).value + ch } });
    }
    expect(textarea).toHaveValue("Authoriz");
  });

  it("surfaces validation errors when saving mixed valid and malformed header lines", async () => {
    const textarea = await openNewStreamableUpstream();
    fireEvent.change(textarea, {
      target: { value: "Authorization: secret_ref:token\nAuthoriz" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    expect(await screen.findByText('Line 2: expected "Key: Value" format')).toBeInTheDocument();
    expect(mockedApi.createUpstream).not.toHaveBeenCalled();
  });
});

describe("Upstreams default tags input", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    mockedApi.listUpstreams.mockResolvedValue({ upstreams: [], source: "draft" });
    mockedApi.configDraft.mockResolvedValue({ config: null, version: null });
  });

  async function openTagsInput() {
    render(<Upstreams />);
    await waitFor(() => expect(mockedApi.listUpstreams).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Add upstream" }));
    return screen.getByLabelText("Default tags (comma-separated)");
  }

  it("keeps the comma visible while typing a tag separator (regression #498f453d)", async () => {
    const input = await openTagsInput();
    fireEvent.change(input, { target: { value: "alpha" } });
    expect(input).toHaveValue("alpha");
    // Typing the comma separator must not make it disappear. The original bug
    // filtered empty trailing entries on every keystroke, so the comma could
    // never be typed (it round-tripped to "alpha").
    fireEvent.change(input, { target: { value: "alpha," } });
    expect((input as HTMLInputElement).value).toContain(",");
    fireEvent.change(input, { target: { value: "alpha, beta" } });
    expect(input).toHaveValue("alpha, beta");
  });
});
