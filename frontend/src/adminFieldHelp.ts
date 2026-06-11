/**
 * Inline help copy for the Upstreams and Profiles editor fields.
 *
 * Kept in one place so the tooltips stay in sync with the field reference in
 * `docs/ADMIN_CONSOLE.md` ("Configuring upstreams and profiles"). Only fields the
 * forms actually render are covered — config-only settings (default_categories,
 * default_risk, requires_auth, isolation) are intentionally omitted.
 */

export const UPSTREAM_HELP = {
  id: "Stable, unique identifier. Immutable after creation. A profile selector's Server field matches this exactly, and it prefixes every canonical name (<id>__<primitive>).",
  transport:
    "Connection type. Selects which fields render below: stdio (command), streamable_http (URL), sse_legacy (SSE/POST URLs), or custom (kind).",
  command: "stdio only. Space-separated argv, e.g. python -m my_server.",
  url: "streamable_http only. The MCP endpoint URL.",
  sseUrl: "sse_legacy only. The event-stream URL.",
  postUrl: "sse_legacy only. The message POST URL.",
  customKind: "custom only. The key of a registered custom-transport adapter.",
  headers:
    "One Name: value per line. Values may use secret_ref:VAR indirection (resolved from the environment/secret store) and are redacted in API responses. Existing redacted values are preserved on save unless you type a new value.",
  defaultTags:
    "Comma-separated. Applied to every primitive cataloged from this upstream — they become each entry's tags, which a profile selector's Tags field matches against.",
} as const;

export const PROFILE_HELP = {
  name: "Identifier passed to gateway_use_profile. Immutable after creation.",
  autoApply:
    "When on, the profile's published set is applied at session initialize, so its tools appear in the first tools/list with no discover/enable round-trip.",
  description: "Free text describing what the profile publishes.",
  server:
    "Exact, case-sensitive match against an upstream ID. Blank = any server.",
  primitiveType:
    "Filter by kind: tool, resource, or prompt. 'any' (blank) applies no type filter.",
  tags: "Comma-separated. Entry must carry all listed tags (case-insensitive subset), matched against the upstream's Default tags. Blank = no tag filter.",
  categories:
    "Comma-separated. Same subset semantics as Tags, against the entry's categories. Blank = no category filter.",
  names:
    "Whitelist of primitive names — NOT a server id. Matches either the canonical form (<upstream-id>__<primitive>, e.g. bridgemind__create_task) or the bare upstream name (e.g. create_task). Either form works. Blank = any name.",
} as const;
