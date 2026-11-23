// Mixed workload: registration, sign-in, directory search, availability, booking with cancellation and
// patient record access. Load level is chosen with PROFILE=smoke|baseline|stress (default baseline).
//   k6 run -e BASE_URL=http://localhost:8080 -e PROFILE=baseline perf/k6/load.js
// The API under test must send email to Mailpit (used to verify setup accounts) and should run with
// RateLimiting__Enabled=false, because every virtual user shares the same staff account.
import { sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { createPractice, createVerifiedUser, expectStatus, localDate, PASSWORD, send, uniqueEmail, uniqueId } from './lib/api.js';

// Access tokens last 10 minutes and are issued in setup, so a single run must stay shorter than that.

const PROFILES = {
  smoke: { scale: 1, stages: [{ duration: '30s', target: 1 }] },
  baseline: { scale: 1, stages: [{ duration: '30s', target: 5 }, { duration: '1m', target: 10 }, { duration: '1m', target: 20 }, { duration: '30s', target: 0 }] },
  stress: { scale: 1, stages: [{ duration: '30s', target: 20 }, { duration: '1m', target: 50 }, { duration: '1m', target: 100 }, { duration: '30s', target: 0 }] },
};
const profile = PROFILES[__ENV.PROFILE || 'baseline'];
if (!profile) throw new Error(`Unknown PROFILE "${__ENV.PROFILE}". Use smoke, baseline or stress.`);

/** Scales the stage targets for one scenario so the mix stays proportional at every load level. */
function stages(weight) {
  return profile.stages.map((s) => ({ duration: s.duration, target: Math.max(s.target === 0 ? 0 : 1, Math.round(s.target * weight)) }));
}

/** Builds a ramping scenario that runs the named exported function. */
function scenario(exec, weight) {
  return { executor: 'ramping-vus', exec, startVUs: 0, stages: stages(weight), gracefulRampDown: '10s' };
}

export const options = {
  setupTimeout: '5m',
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    registration: scenario('registration', 0.05),
    sign_in: scenario('signIn', 0.10),
    directory_search: scenario('directorySearch', 0.20),
    availability: scenario('availability', 0.30),
    booking: scenario('bookAndCancel', 0.15),
    patient_records: scenario('patientRecords', 0.20),
  },
  thresholds: {
    // Engineering targets from the product requirements; a failed threshold fails the run.
    'http_req_duration{name:availability}': ['p(95)<300'],
    'http_req_duration{name:directory_search}': ['p(95)<300'],
    'http_req_duration{name:patient_read}': ['p(95)<300'],
    'http_req_duration{name:appointment_book}': ['p(95)<500'],
    unexpected_errors: ['rate<0.01'],
  },
};

const unexpectedErrors = new Rate('unexpected_errors');
const bookingConflicts = new Counter('booking_conflicts');
const bookingsCreated = new Counter('bookings_created');
const bookingLatency = new Trend('booking_latency', true);

/** Creates the practice and a pool of verified accounts used by the sign-in scenario. */
export function setup() {
  const practice = createPractice({ patients: 50 });
  const signInUsers = Array.from({ length: 5 }, () => createVerifiedUser('perf.signin').email);
  const portal = createVerifiedUser('perf.portal');
  return { ...practice, signInUsers, portalToken: portal.token };
}

/** Registers a new account (password hashing makes this deliberately CPU-intensive). */
export function registration() {
  const response = send('POST', '/api/v1/auth/register',
    { email: uniqueEmail('perf.register'), password: PASSWORD, firstName: 'Load', lastName: 'Synthetic' }, { tag: 'register' });
  unexpectedErrors.add(!expectStatus(response, 202, 'register'));
  sleep(1);
}

/** Signs in with one of the pre-verified accounts. */
export function signIn(data) {
  const email = data.signInUsers[Math.floor(Math.random() * data.signInUsers.length)];
  const response = send('POST', '/api/v1/auth/login', { email, password: PASSWORD }, { tag: 'login' });
  unexpectedErrors.add(!expectStatus(response, 200, 'login'));
  sleep(1);
}

/** Searches the provider directory as a signed-in patient. */
export function directorySearch(data) {
  const response = send('GET', '/api/v1/directory/providers?specialty=family&state=TX&pageSize=20', undefined,
    { token: data.portalToken, tag: 'directory_search' });
  unexpectedErrors.add(!expectStatus(response, 200, 'directory search'));
  sleep(0.5);
}

/** Reads one day of availability for the provider. */
export function availability(data) {
  const day = localDate(1 + Math.floor(Math.random() * 28));
  const response = send('GET', `/api/v1/availability?providerId=${data.providerId}&from=${day}&to=${day}`, undefined,
    { token: data.ownerToken, organizationId: data.organizationId, tag: 'availability' });
  unexpectedErrors.add(!expectStatus(response, 200, 'availability'));
  sleep(0.5);
}

/** Books a random open slot and cancels it again; a slot taken by another user (409) is an expected outcome. */
export function bookAndCancel(data) {
  const scope = { token: data.ownerToken, organizationId: data.organizationId };
  const day = localDate(1 + Math.floor(Math.random() * 60));
  const slots = send('GET', `/api/v1/availability?providerId=${data.providerId}&from=${day}&to=${day}`, undefined, { ...scope, tag: 'availability' });
  if (!expectStatus(slots, 200, 'booking availability')) {
    unexpectedErrors.add(true);
    return;
  }
  const open = slots.json('slots');
  if (open.length === 0) return;
  const slot = open[Math.floor(Math.random() * open.length)];
  const patientId = data.patientIds[Math.floor(Math.random() * data.patientIds.length)];
  const booking = send('POST', '/api/v1/appointments',
    { patientId, providerId: data.providerId, locationId: slot.locationId, startsAt: slot.startsAt, visitType: 'in_person' },
    { ...scope, idempotencyKey: `perf-${uniqueId()}`, tag: 'appointment_book' });
  bookingLatency.add(booking.timings.duration);
  unexpectedErrors.add(!expectStatus(booking, [201, 409], 'book'));
  if (booking.status === 409) {
    bookingConflicts.add(1);
  } else if (booking.status === 201) {
    bookingsCreated.add(1);
    const cancel = send('POST', `/api/v1/appointments/${booking.json('id')}/cancel`, { reason: 'Load test' }, { ...scope, tag: 'appointment_cancel' });
    unexpectedErrors.add(!expectStatus(cancel, 200, 'cancel'));
  }
  sleep(1);
}

/** Reads a patient record and runs a name search, as front-desk staff do. */
export function patientRecords(data) {
  const scope = { token: data.ownerToken, organizationId: data.organizationId };
  const patientId = data.patientIds[Math.floor(Math.random() * data.patientIds.length)];
  const read = send('GET', `/api/v1/patients/${patientId}`, undefined, { ...scope, tag: 'patient_read' });
  const search = send('GET', '/api/v1/patients?search=patient1&pageSize=20', undefined, { ...scope, tag: 'patient_search' });
  unexpectedErrors.add(!expectStatus(read, 200, 'patient read'));
  unexpectedErrors.add(!expectStatus(search, 200, 'patient search'));
  sleep(0.5);
}
