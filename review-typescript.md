- `FieldHelp.tsx:24-27`: `id` prop used directly as DOM `id` (no prefix/suffix), collision risk between pages on shared route (`ph-name` vs `uh-id` safe here but fragile if both routes ever rendered concurrently or SPA route reuse adds duplicate).
- `FieldHelp.tsx:15`: Prop `label` used solely for `data-field`; TypeScript does not enforce uniqueness or required relationship to `id`.
- `Upstreams.tsx:266,276,297,309,326,337,355,368,382,395`: Ten <span class="field-label-row"> blocks; exact duplication in `Profiles.tsx:250,272,291,300,325,336,351`.
- `adminFieldHelp.ts:1-35`: Flat string map is correct and maintainable; no type-level guard that keys match actual field usage in callers.
- `Upstreams.tsx:3-4` / `Profiles.tsx:3-4`: Direct sibling import (`../adminFieldHelp`, `../../components/FieldHelp`) — correct module boundary but both pages import same two modules; opportunity for domain-level barrel.
- `styles.css:492-496`: Tooltip visibility driven purely by `.field-help:hover` + ancestor `:focus-within`; no encapsulation — any future label refactor can silently break tooltips.
- `Profiles.tsx:269`: Auto-apply FieldHelp placed *after* checkbox text (order-only difference from other labels) — correct visually but inconsistent markup pattern.

**Suggested fixes:**
- `FieldHelp.tsx:15`: Add runtime unique-id generation or require explicit unique `describedById` distinct from display `label`.
- Extract `<FieldLabel helpId={id} label={label} helpText={text}>` wrapper to collapse the 20 repeated `<span class="field-label-row">…</span>` blocks.
- Add `adminFieldHelp.ts` test that enumerates `UPSTREAM_HELP`/`PROFILE_HELP` keys and asserts each is referenced by an `aria-describedby` in the pages.
- Move tooltip CSS into `FieldHelp.tsx` module (CSS modules or `<style jsx>`) to eliminate global class coupling.