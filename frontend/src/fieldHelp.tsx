/**
 * Inline field help for admin config editors.
 * Copy is distilled from docs/ADMIN_CONSOLE.md ("Configuring upstreams and profiles").
 */
import {
  cloneElement,
  isValidElement,
  useId,
  type ReactElement,
  type ReactNode,
} from "react";

export const FIELD_HELP = {
  upstreamId:
    "Stable upstream identifier. Immutable after creation. Profile selectors match this exact string in Server, and it prefixes every canonical name (<id>__<primitive>).",
  upstreamTransport:
    "Connection type: stdio (local process), streamable_http (modern MCP HTTP), sse_legacy (HTTP+SSE), or custom (registered adapter kind). Controls which fields appear below.",
  upstreamCommand:
    "stdio only. Space-separated argv, e.g. python -m my_server.",
  upstreamUrl: "streamable_http only. The remote MCP endpoint URL.",
  upstreamSseUrl: "sse_legacy only. URL of the server-sent events stream.",
  upstreamPostUrl: "sse_legacy only. URL where JSON-RPC messages are POSTed.",
  upstreamCustomKind:
    "custom only. Key of a custom-transport adapter registered with the gateway.",
  upstreamHeaders:
    "streamable_http / sse_legacy only. One Name: value per line. Values may use secret_ref:VAR (resolved from env/secrets). Responses redact secrets; leave redacted lines unchanged on save unless replacing them.",
  upstreamDefaultTags:
    "Comma-separated tags applied to every primitive cataloged from this upstream. Profile selector Tags matches against these entry tags.",

  profileName:
    "Profile identifier passed to gateway_use_profile. Immutable after creation.",
  profileAutoApply:
    "When enabled, this profile's published set is applied at session initialize so tools appear in the first tools/list with no discover/enable round-trip.",
  profileDescription: "Optional free-text description for operators.",

  selectorServer:
    "Exact, case-sensitive match against an upstream ID. Blank matches any server.",
  selectorPrimitiveType:
    "Filter by primitive kind. “any” (blank) applies no type filter.",
  selectorTags:
    "Comma-separated. The catalog entry must include all listed tags (case-insensitive). Blank = no tag filter. Matches upstream Default tags.",
  selectorCategories:
    "Comma-separated. Same subset semantics as Tags, against entry categories. Blank = no category filter.",
  selectorNames:
    "Comma-separated whitelist of primitive names. Accepts canonical form (<upstream-id>__<primitive>, e.g. bridgemind__create_task) or upstream-side name (e.g. create_task). Blank = any name. This is not an upstream id — putting “bridgemind” here matches nothing. Copy names from Preview.",
} as const;

export type FieldHelpKey = keyof typeof FIELD_HELP;

type FieldHelpProps = {
  helpKey: FieldHelpKey;
};

export function FieldHelp({ helpKey }: FieldHelpProps) {
  const text = FIELD_HELP[helpKey];
  const popoverId = useId();

  return (
    <span className="field-help">
      <button
        type="button"
        className="field-help-trigger"
        aria-label={`Help: ${helpKey}`}
        aria-describedby={popoverId}
      >
        ?
      </button>
      <span id={popoverId} role="tooltip" className="field-help-popover">
        {text}
      </span>
    </span>
  );
}

type FieldLabelProps = {
  label: string;
  helpKey?: FieldHelpKey;
  children: ReactNode;
  className?: string;
};

export function FieldLabel({ label, helpKey, children, className }: FieldLabelProps) {
  const fieldId = useId();
  const control = isValidElement(children)
    ? cloneElement(children as ReactElement<{ id?: string }>, { id: fieldId })
    : children;

  return (
    <div className={className ? `field-label-wrap ${className}` : "field-label-wrap"}>
      <div className="field-label-row">
        <label htmlFor={fieldId}>{label}</label>
        {helpKey ? <FieldHelp helpKey={helpKey} /> : null}
      </div>
      {control}
    </div>
  );
}
