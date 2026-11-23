// Smoke test: one pass through every performance scenario with a single virtual user, to confirm the
// environment works before running heavier profiles. Equivalent to `PROFILE=smoke perf/k6/load.js`
// but finishes after one iteration of each flow.
//   k6 run -e BASE_URL=http://localhost:8080 perf/k6/smoke.js
import { check } from 'k6';
import { availability, bookAndCancel, directorySearch, patientRecords, registration, setup as loadSetup, signIn } from './load.js';

export const options = {
  setupTimeout: '5m',
  vus: 1,
  iterations: 1,
  thresholds: { checks: ['rate==1.0'], unexpected_errors: ['rate==0'] },
};

/** Reuses the load test setup. */
export function setup() {
  return loadSetup();
}

/** Runs each flow once. */
export default function (data) {
  registration();
  signIn(data);
  directorySearch(data);
  availability(data);
  bookAndCancel(data);
  patientRecords(data);
  check(null, { 'smoke pass completed': () => true });
}
