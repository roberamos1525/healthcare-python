// Concurrency test: many virtual users try to book the same appointment slot at the same moment.
// Exactly one booking may succeed; every other request must receive 409 and no request may fail otherwise.
//   k6 run -e BASE_URL=http://localhost:8080 -e CONTENDERS=50 perf/k6/booking-contention.js
import { check, fail } from 'k6';
import { Counter } from 'k6/metrics';
import exec from 'k6/execution';
import { createPractice, localDate, send } from './lib/api.js';

const CONTENDERS = Number(__ENV.CONTENDERS || 50);

export const options = {
  setupTimeout: '3m',
  scenarios: {
    race: { executor: 'per-vu-iterations', vus: CONTENDERS, iterations: 1, maxDuration: '1m' },
  },
  thresholds: {
    bookings_created: ['count==1'],
    unexpected_responses: ['count==0'],
  },
};

const created = new Counter('bookings_created');
const conflicts = new Counter('booking_conflicts');
const unexpected = new Counter('unexpected_responses');

/** Creates a practice and picks the slot every virtual user will race for. */
export function setup() {
  const practice = createPractice({ patients: 10 });
  const day = localDate(14);
  const availability = send('GET', `/api/v1/availability?providerId=${practice.providerId}&from=${day}&to=${day}`, undefined,
    { token: practice.ownerToken, organizationId: practice.organizationId });
  const slot = availability.json('slots')[10];
  if (!slot) fail('No slot is available for the contention test.');
  return { ...practice, slot };
}

/** Sends one booking request for the shared slot with a unique idempotency key. */
export default function (data) {
  const patientId = data.patientIds[exec.vu.idInTest % data.patientIds.length];
  const response = send('POST', '/api/v1/appointments',
    { patientId, providerId: data.providerId, locationId: data.slot.locationId, startsAt: data.slot.startsAt, visitType: 'in_person' },
    { token: data.ownerToken, organizationId: data.organizationId, idempotencyKey: `race-${exec.vu.idInTest}-${Date.now()}`, tag: 'contended_book' });
  if (response.status === 201) created.add(1);
  else if (response.status === 409) conflicts.add(1);
  else unexpected.add(1);
  check(response, { 'booked or conflict': (r) => r.status === 201 || r.status === 409 });
}

/** Confirms through the API that exactly one active appointment holds the slot. */
export function teardown(data) {
  const from = encodeURIComponent(data.slot.startsAt);
  const to = encodeURIComponent(data.slot.endsAt);
  const list = send('GET', `/api/v1/appointments?providerId=${data.providerId}&from=${from}&to=${to}&status=confirmed`, undefined,
    { token: data.ownerToken, organizationId: data.organizationId });
  const total = list.json('totalItems');
  if (total !== 1) fail(`Expected exactly one appointment in the contested slot, found ${total}.`);
}
