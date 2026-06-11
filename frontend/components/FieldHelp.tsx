/**
 * Small inline help affordance: a "?" badge that reveals tooltip copy on hover
 * (mouse) or when its field gains focus (keyboard, via the label's `:focus-within`
 * in styles.css). Used by the Upstreams and Profiles editor fields.
 *
 * The "?" badge is `aria-hidden` so it stays out of the wrapping `<label>`'s
 * accessible name — the field caption alone names the input (matching the
 * `getByRole` name queries in the page tests). For assistive tech, the bubble
 * carries an `id`; the field's input references it with `aria-describedby`, so
 * the help is announced as the field's description rather than polluting its name.
 *
 * Clicking the badge is swallowed (`preventDefault`) so that tapping the "?"
 * inside a wrapping `<label>` never activates its control — most importantly it
 * must not toggle the Auto-apply checkbox.
 *
 * Help copy lives in `src/adminFieldHelp.ts` and mirrors `docs/ADMIN_CONSOLE.md`.
 */

export interface FieldHelpProps {
  /** Tooltip body. */
  text: string;
  /** Field name — surfaced as a `data-field` hook for debugging/tests. */
  label: string;
  /** Bubble id, referenced by the field input via `aria-describedby`. */
  id: string;
}

export function FieldHelp({ text, label, id }: FieldHelpProps) {
  return (
    <span
      className="field-help"
      data-field={label}
      aria-hidden="true"
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
      }}
    >
      ?
      <span id={id} role="tooltip" className="field-help-bubble">
        {text}
      </span>
    </span>
  );
}
