## Review Findings (adversarial: Tests & Validation)

- Impact of role-based selector changes: Upstreams.test.tsx now selects Transport via `getByRole("combobox", { name: "Transport" })` (line 52) and ID via `getByRole("textbox", { name: "ID" })` (line 55) — both stable post-FieldHelp. Existing tests continue to pass (all assertions use these selectors).
- Profiles.test.tsx: only one test present (describes "auto-apply help badge", lines 35-50). It verifies clicking the badge does not toggle the checkbox by using `container.querySelector('[data-field="Auto-apply"]')` (line 43). No positive/negative FieldHelp tooltip assert; no coverage for other 8 FieldHelp instances.
- Brittleness introduced by label-structure changes: labels now wrap `<span className="field-label-row">` containing label text + `<FieldHelp>`. `getByRole("textbox", { name: /Label/ })` still finds input by the visible label text per ARIA, so no breakage. However, automatic label association for `getByLabelText` would break on any extra span nesting — the migration already switched to role-based selectors to avoid regression.
- Depth vs breadth of verification for tooltip feature:
  • Profiles.test.tsx covers only the "does not toggle" regression (line 49), one data-field selector, and zero tooltip visibility/keyboard behavior assertions.
  • Upstreams.test.tsx contains zero FieldHelp coverage at all, despite 10 FieldHelp usages across Upstreams.tsx (IDs uh-id, uh-transport…uh-tags).
  • No test confirms FieldHelp data-field attribute, tooltip open/close state, or ARIA-describedby wiring.
- Root cause of shallow coverage: Profiles.test.tsx openNewProfile helper returns only the render container and does not expose helpers for FieldHelp elements or mock tooltip portal. Upstreams.test helpers (openNewStreamableUpstream, openTagsInput) stop at the input being tested and never traverse the label-row parent.
- Suggested minimal fix (preserving scope): extend Profiles.test.tsx to assert `getByRole("button", { name: "?" })` exists for "Auto-apply" and one additional field; add identical smoke check inside an Upstreams describe block so future regressions are caught. No locator changes are required.

## Acceptance Contract - Initial Review
- review-done: concrete file:line-backed findings written with suggested fixes.