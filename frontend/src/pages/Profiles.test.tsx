import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Profiles from "./Profiles";

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
    listProfiles: vi.fn(),
    configDraft: vi.fn(),
    applyProfilesDraft: vi.fn(),
    createProfile: vi.fn(),
    getProfile: vi.fn(),
    updateProfile: vi.fn(),
    deleteProfile: vi.fn(),
    duplicateProfile: vi.fn(),
    previewProfile: vi.fn(),
  },
}));

vi.mock("../../components/PendingChanges", () => ({
  PendingChanges: () => null,
}));

import { api } from "../api";

const mockedApi = vi.mocked(api);

describe("Profiles auto-apply help badge", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    mockedApi.listProfiles.mockResolvedValue({ profiles: [], source: "draft" });
    mockedApi.configDraft.mockResolvedValue({ config: null, version: null });
  });

  async function openNewProfile() {
    const { container } = render(<Profiles />);
    await waitFor(() => expect(mockedApi.listProfiles).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Add profile" }));
    return container;
  }

  it("does not toggle the checkbox when its help badge is clicked", async () => {
    const container = await openNewProfile();
    const checkbox = screen.getByRole("checkbox", { name: /Auto-apply/ }) as HTMLInputElement;
    expect(checkbox.checked).toBe(false);

    const badge = container.querySelector('[data-field="Auto-apply"]');
    expect(badge).not.toBeNull();
    fireEvent.click(badge as Element);

    // The "?" is decorative help; clicking it must not flip auto_apply.
    expect(checkbox.checked).toBe(false);
  });
});
