/** stdio upstream env editor — same Key: Value line format as HTTP headers. */
export {
  headersToText as envToText,
  isPreservedHeaderValue as isPreservedEnvValue,
  prepareHeadersForSave as prepareEnvForSave,
  validateHeadersText as validateEnvText,
  WRITE_ONLY_HEADER as WRITE_ONLY_ENV,
} from "./upstreamHeaders";
