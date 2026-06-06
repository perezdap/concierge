/** Matches backend ``REDACTED_VALUE`` and write-only display forms. */
export const REDACTED_HEADER = "***";
export const WRITE_ONLY_HEADER = { __write_only__: true as const };

export function headersToText(headers: Record<string, string> | undefined): string {
  if (!headers || Object.keys(headers).length === 0) {
    return "";
  }
  return Object.entries(headers)
    .map(([k, v]) => `${k}: ${v}`)
    .join("\n");
}

export function textToHeaders(text: string): Record<string, string> {
  return validateHeadersText(text).headers;
}

export function validateHeadersText(text: string): {
  headers: Record<string, string>;
  errors: string[];
} {
  const headers: Record<string, string> = {};
  const errors: string[] = [];
  for (const [index, line] of text.split("\n").entries()) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const colonIdx = trimmed.indexOf(":");
    if (colonIdx < 1) {
      errors.push(`Line ${index + 1}: expected "Key: Value" format`);
      continue;
    }
    headers[trimmed.slice(0, colonIdx).trim()] = trimmed.slice(colonIdx + 1).trim();
  }
  return { headers, errors };
}

export function isPreservedHeaderValue(value: string): boolean {
  return (
    value === REDACTED_HEADER ||
    value === "[REDACTED]" ||
    value.startsWith("secret_ref:")
  );
}

export function prepareHeadersForSave(
  edited: Record<string, string>,
  baseline: Record<string, string>,
): Record<string, string | typeof WRITE_ONLY_HEADER> {
  const out: Record<string, string | typeof WRITE_ONLY_HEADER> = {};
  for (const [key, val] of Object.entries(edited)) {
    const prior = baseline[key];
    const priorWasSecret =
      prior !== undefined &&
      (isPreservedHeaderValue(prior) || prior.startsWith("secret_ref:"));
    if (isPreservedHeaderValue(val) && priorWasSecret) {
      out[key] = WRITE_ONLY_HEADER;
    } else if (prior !== undefined && val === prior && prior.startsWith("secret_ref:")) {
      out[key] = WRITE_ONLY_HEADER;
    } else {
      out[key] = val;
    }
  }
  return out;
}
