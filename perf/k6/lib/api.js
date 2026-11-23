// Shared helpers for the k6 performance tests. All data created here is synthetic.
import http from 'k6/http';
import { check, fail, sleep } from 'k6';

export const BASE_URL = (__ENV.BASE_URL || 'http://localhost:8080').replace(/\/$/, '');
export const MAILPIT_URL = (__ENV.MAILPIT_URL || 'http://localhost:8025').replace(/\/$/, '');
export const PASSWORD = 'Synthetic-Load-Password-1';
export const TIME_ZONE = 'America/Chicago';

let sequence = 0;

/** Returns a value that is unique across virtual users and iterations (time, randomness and a per-VU counter). */
export function uniqueId() {
  sequence += 1;
  return `${Date.now().toString(36)}${Math.floor(Math.random() * 1e12).toString(36)}${sequence.toString(36)}`;
}

/** Returns a synthetic email address on the reserved example.test domain. */
export function uniqueEmail(prefix) {
  return `${prefix}.${uniqueId()}@example.test`;
}

/** Builds request parameters with JSON headers, an optional bearer token, organization and idempotency key. */
export function params({ token, organizationId, idempotencyKey, tag } = {}) {
  const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (organizationId) headers['X-Organization-Id'] = organizationId;
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
  return { headers, tags: tag ? { name: tag } : undefined };
}

/** Sends a JSON request and returns the response. */
export function send(method, path, body, options = {}) {
  const payload = body === undefined ? null : JSON.stringify(body);
  return http.request(method, `${BASE_URL}${path}`, payload, params(options));
}

/** Sends a request during setup and fails the test run when the status is not the expected one. */
export function must(method, path, body, options, expectedStatus) {
  const response = send(method, path, body, options);
  if (response.status !== expectedStatus) {
    fail(`${method} ${path} returned ${response.status}: ${response.body}`);
  }
  return response.body ? response.json() : null;
}

/** Reads the newest email to an address from Mailpit and returns the token in its link. */
export function tokenFromMailpit(email, subjectPart) {
  for (let attempt = 0; attempt < 60; attempt++) {
    const search = http.get(`${MAILPIT_URL}/api/v1/search?query=${encodeURIComponent(`to:"${email}"`)}`, { tags: { name: 'mailpit' } });
    if (search.status === 200) {
      const message = (search.json('messages') || []).find((m) => (m.Subject || '').includes(subjectPart));
      if (message) {
        const full = http.get(`${MAILPIT_URL}/api/v1/message/${message.ID}`, { tags: { name: 'mailpit' } }).json();
        const match = /token=([^\s&]+)/.exec(full.Text || '');
        if (match) return decodeURIComponent(match[1]);
      }
    }
    sleep(0.25);
  }
  fail(`No "${subjectPart}" email arrived for ${email}. Is the API sending email to Mailpit (Email__Provider=Smtp)?`);
  return null;
}

/** Registers, verifies and signs in a new user; returns { email, token, userId }. */
export function createVerifiedUser(prefix) {
  const email = uniqueEmail(prefix);
  must('POST', '/api/v1/auth/register', { email, password: PASSWORD, firstName: 'Load', lastName: 'Synthetic' }, {}, 202);
  must('POST', '/api/v1/auth/verify-email', { token: tokenFromMailpit(email, 'Verify your email') }, {}, 204);
  return { email, ...login(email) };
}

/** Signs in and returns { token, userId }. */
export function login(email) {
  const body = must('POST', '/api/v1/auth/login', { email, password: PASSWORD }, {}, 200);
  return { token: body.accessToken, userId: body.user.id };
}

/**
 * Returns the date (YYYY-MM-DD) a number of days from today in US Central time. The k6 runtime has no
 * time zone database, so a fixed UTC-6 offset is used; near midnight this can select the adjacent day,
 * which is harmless for choosing test dates.
 */
export function localDate(daysAhead) {
  return new Date(Date.now() - 6 * 3_600_000 + daysAhead * 86_400_000).toISOString().slice(0, 10);
}

/** Returns a check-digit-valid synthetic NPI. */
export function syntheticNpi() {
  const base = '1' + Array.from({ length: 8 }, () => Math.floor(Math.random() * 10)).join('');
  for (let check = 0; check < 10; check++) {
    const digits = `80840${base}${check}`;
    let sum = 0;
    for (let i = digits.length - 1, double = false; i >= 0; i--, double = !double) {
      let d = Number(digits[i]);
      if (double) { d *= 2; if (d > 9) d -= 9; }
      sum += d;
    }
    if (sum % 10 === 0) return `${base}${check}`;
  }
  return null;
}

/**
 * Creates a complete practice: owner, permissive booking policy, clinic, location, an active provider
 * working every day 08:00-18:00 in 15-minute slots, and a number of synthetic patients.
 */
export function createPractice({ patients = 20 } = {}) {
  const owner = createVerifiedUser('perf.owner');
  const organization = must('POST', '/api/v1/organizations',
    { name: `Load Test Clinic ${uniqueId()}`, type: 'clinic', timeZone: TIME_ZONE }, { token: owner.token }, 201);
  const scope = { token: owner.token, organizationId: organization.id };
  must('PUT', `/api/v1/organizations/${organization.id}/settings`, {
    defaultAppointmentDurationMinutes: 15, minimumBookingNoticeMinutes: 0, bookingHorizonDays: 90, patientCancellationWindowHours: 0,
    reminderOffsetsMinutes: [1440, 120], allowPatientSelfScheduling: true, autoConfirmPatientBookings: true,
  }, scope, 200);
  const clinic = must('POST', '/api/v1/clinics', { name: 'Load Clinic' }, scope, 201);
  const location = must('POST', `/api/v1/clinics/${clinic.id}/locations`, {
    name: 'Load Location', addressLine1: '1 Test Way', city: 'Austin', state: 'TX', postalCode: '78701', timeZone: TIME_ZONE,
  }, scope, 201);
  const provider = must('POST', '/api/v1/providers', {
    firstName: 'Load', lastName: `Provider${uniqueId()}`, npi: syntheticNpi(), acceptingNewPatients: true, listedInDirectory: true,
    specialties: [{ name: 'Family Medicine', isPrimary: true }], locationIds: [location.id],
  }, scope, 201);
  const credential = must('POST', `/api/v1/providers/${provider.id}/credentials`, {
    type: 'medical_license', issuingAuthority: 'Texas Medical Board', identifier: `LOAD-${uniqueId()}`.slice(0, 30),
  }, scope, 201);
  must('POST', `/api/v1/providers/${provider.id}/credentials/${credential.id}/review`, { decision: 'verified' }, scope, 200);
  must('POST', `/api/v1/providers/${provider.id}/activate`, {}, scope, 200);
  for (let day = 0; day < 7; day++) {
    must('POST', '/api/v1/provider-schedules', {
      providerId: provider.id, locationId: location.id, dayOfWeek: day, startTime: '08:00', endTime: '18:00',
      slotDurationMinutes: 15, effectiveFrom: localDate(-1),
    }, scope, 201);
  }
  const patientIds = [];
  for (let i = 0; i < patients; i++) {
    const patient = must('POST', '/api/v1/patients', {
      firstName: 'Load', lastName: `Patient${i}`, dateOfBirth: '1980-01-15', administrativeGender: 'unknown',
    }, scope, 201);
    patientIds.push(patient.id);
  }
  return {
    ownerEmail: owner.email, ownerToken: owner.token, organizationId: organization.id, locationId: location.id,
    providerId: provider.id, patientIds,
  };
}

/** Records the outcome of a request as a k6 check and returns whether it matched. */
export function expectStatus(response, statuses, label) {
  const allowed = Array.isArray(statuses) ? statuses : [statuses];
  return check(response, { [`${label} status ${allowed.join('/')}`]: (r) => allowed.includes(r.status) });
}
