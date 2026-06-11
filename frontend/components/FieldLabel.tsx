import { ReactNode } from "react";
import { FieldHelp } from "./FieldHelp";

export interface FieldLabelProps {
  /** Visible label text. */
  label: string;
  /** aria-describedby id for the help bubble (must be unique on the page). */
  helpId: string;
  /** Tooltip body. */
  helpText: string;
  /** Optional extra content to render inside the label row (e.g., checkbox itself). */
  children?: ReactNode;
}

/**
 * Convenience wrapper that renders the standard label-row markup used by
 * the admin editors:
 *   <span className="field-label-row">
 *     {label}
 *     <FieldHelp … />
 *   </span>
 *   {children}
 *
 * Eliminates the 20+ duplicated <span className="field-label-row"> blocks.
 */
export function FieldLabel({ label, helpId, helpText, children }: FieldLabelProps) {
  return (
    <>
      <span className="field-label-row">
        {label}
        <FieldHelp label={label} id={helpId} text={helpText} />
      </span>
      {children}
    </>
  );
}
