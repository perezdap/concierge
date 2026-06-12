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

describe("Upstreams command input", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    mockedApi.listUpstreams.mockResolvedValue({ upstreams: [], source: "draft" });
    mockedApi.configDraft.mockResolvedValue({ config: null, version: null });
  });

  async function openCommandInput() {
    render(<Upstreams />);
    await waitFor(() => expect(mockedApi.listUpstreams).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Add upstream" }));
    return screen.getByLabelText(/Command \(space-separated\)/i);
  }

  it("keeps a trailing space while typing argv separators", async () => {
    const input = await openCommandInput();
    fireEvent.change(input, { target: { value: "python" } });
    expect(input).toHaveValue("python");
    fireEvent.change(input, { target: { value: "python " } });
    expect(input).toHaveValue("python ");
    fireEvent.change(input, { target: { value: "python -m my_server" } });
    expect(input).toHaveValue("python -m my_server");
  });

  it("parses command argv only when saving", async () => {
    const input = await openCommandInput();
    fireEvent.change(screen.getByLabelText("ID"), { target: { value: "demo" } });
    fireEvent.change(input, { target: { value: "python -m my_server" } });
    mockedApi.createUpstream.mockResolvedValue({
      upstream: { id: "demo", transport: "stdio", command: ["python", "-m", "my_server"] },
      version_id: "v1",
    });
    mockedApi.getUpstream.mockResolvedValue({
      upstream: { id: "demo", transport: "stdio", command: ["python", "-m", "my_server"] },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(mockedApi.createUpstream).toHaveBeenCalled());
    expect(mockedApi.createUpstream.mock.calls[0][0].command).toEqual([
      "python",
      "-m",
      "my_server",
    ]);
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

  it("shows inline help triggers on upstream form fields", async () => {
    await openNewStreamableUpstream();
    expect(screen.getByRole("button", { name: /Help: upstreamId/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Help: upstreamUrl/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Help: upstreamHeaders/i })).toBeInTheDocument();
  });
});

describe("Upstreams stdio environment textarea", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    mockedApi.listUpstreams.mockResolvedValue({ upstreams: [], source: "draft" });
    mockedApi.configDraft.mockResolvedValue({ config: null, version: null });
  });

  async function openNewStdioUpstream() {
    render(<Upstreams />);
    await waitFor(() => expect(mockedApi.listUpstreams).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Add upstream" }));
    fireEvent.change(screen.getByLabelText("ID"), { target: { value: "demo" } });
    return screen.getByPlaceholderText("BRAVE_API_KEY: your-key-here");
  }

  it("shows the env textarea only for stdio transport", async () => {
    render(<Upstreams />);
    await waitFor(() => expect(mockedApi.listUpstreams).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Add upstream" }));
    expect(screen.getByPlaceholderText("BRAVE_API_KEY: your-key-here")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Transport"), {
      target: { value: "streamable_http" },
    });
    expect(screen.queryByPlaceholderText("BRAVE_API_KEY: your-key-here")).not.toBeInTheDocument();
  });

  it("includes parsed env in save payload for stdio upstreams", async () => {
    const textarea = await openNewStdioUpstream();
    fireEvent.change(screen.getByLabelText(/Command \(space-separated\)/i), {
      target: { value: "npx -y @modelcontextprotocol/server-brave-search" },
    });
    fireEvent.change(textarea, {
      target: { value: "BRAVE_API_KEY: test-key-123" },
    });
    mockedApi.createUpstream.mockResolvedValue({
      upstream: {
        id: "demo",
        transport: "stdio",
        command: ["npx", "-y", "@modelcontextprotocol/server-brave-search"],
        env: { BRAVE_API_KEY: "***" },
      },
      version_id: "v1",
    });
    mockedApi.getUpstream.mockResolvedValue({
      upstream: {
        id: "demo",
        transport: "stdio",
        command: ["npx", "-y", "@modelcontextprotocol/server-brave-search"],
        env: { BRAVE_API_KEY: "***" },
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(mockedApi.createUpstream).toHaveBeenCalled());
    expect(mockedApi.createUpstream.mock.calls[0][0].env).toEqual({
      BRAVE_API_KEY: "test-key-123",
    });
  });

  it("surfaces validation errors for malformed env lines", async () => {
    const textarea = await openNewStdioUpstream();
    fireEvent.change(textarea, {
      target: { value: "BRAVE_API_KEY: ok\nBRAVE" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    expect(await screen.findByText('Line 2: expected "Key: Value" format')).toBeInTheDocument();
    expect(mockedApi.createUpstream).not.toHaveBeenCalled();
  });

  it("preserves write-only env when redacted value is unchanged on save", async () => {
    mockedApi.listUpstreams.mockResolvedValue({
      upstreams: [{ id: "demo", transport: "stdio" }],
      source: "draft",
    });
    mockedApi.getUpstream.mockResolvedValue({
      upstream: {
        id: "demo",
        transport: "stdio",
        command: ["python", "-m", "server"],
        env: { BRAVE_API_KEY: "***" },
      },
    });
    mockedApi.updateUpstream.mockResolvedValue({
      upstream: {
        id: "demo",
        transport: "stdio",
        command: ["python", "-m", "server"],
        env: { BRAVE_API_KEY: "***" },
      },
      version_id: "v2",
    });
    render(<Upstreams />);
    await waitFor(() => expect(mockedApi.listUpstreams).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /demo stdio/i }));
    await waitFor(() => expect(mockedApi.getUpstream).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(mockedApi.updateUpstream).toHaveBeenCalled());
    expect(mockedApi.updateUpstream.mock.calls[0][1].env).toEqual({
      BRAVE_API_KEY: { __write_only__: true },
    });
  });

  it("shows inline help for the stdio env field", async () => {
    await openNewStdioUpstream();
    expect(screen.getByRole("button", { name: /Help: upstreamEnv/i })).toBeInTheDocument();
  });
});
