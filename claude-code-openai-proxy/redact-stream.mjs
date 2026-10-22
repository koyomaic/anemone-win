export { StreamProcessor } from './redact.mjs';
import { StreamProcessor } from './redact.mjs';

export function createStreamProcessor(redactor, map) {
  return new StreamProcessor(redactor, map);
}
