"""Provider and patient scenarios: credentials, schedules, availability, directory, records, consents and access."""

import re
import uuid
from datetime import timedelta

from tests.integration.support import (
    add_member,
    create_active_provider,
    create_patient,
    create_portal_patient,
    create_practice,
    create_user,
    env,
    expect,
    local_date,
    parse_instant,
    problem,
    random_npi,
    read,
    slots,
    utc_today,
)

_MRN = re.compile(r"^[0-9]{10}$")


async def test_a_provider_needs_a_verified_license_before_activation() -> None:
    """Activation requires a verified license and credentials are reviewed."""
    practice = await create_practice("Rivera")
    owner = practice.owner
    provider = read(
        await owner.post(
            "/api/v1/providers",
            {
                "firstName": "Sam",
                "lastName": "Okafor",
                "locationIds": [practice.location_id],
            },
        ),
        201,
    )
    provider_id = provider["id"]
    assert provider["status"] == "onboarding", f"new providers are onboarding: {provider['status']}"
    problem(await owner.post(f"/api/v1/providers/{provider_id}/activate"), 422, "provider_not_verified")
    credential = read(
        await owner.post(
            f"/api/v1/providers/{provider_id}/credentials",
            {
                "type": "medical_license",
                "issuingAuthority": "Texas Medical Board",
                "identifier": "TEST-LICENSE-42",
            },
        ),
        201,
    )
    review_path = f"/api/v1/providers/{provider_id}/credentials/{credential['id']}/review"
    expect(await owner.post(review_path, {"decision": "rejected", "notes": "Number not found"}), 200)
    problem(await owner.post(f"/api/v1/providers/{provider_id}/activate"), 422, "provider_not_verified")
    expect(await owner.post(review_path, {"decision": "verified"}), 200)
    active = read(await owner.post(f"/api/v1/providers/{provider_id}/activate"), 200)
    assert active["status"] == "active" and active["licenseVerified"] is True, active


async def test_provider_profiles_validate_the_npi_and_keep_it_unique() -> None:
    """The NPI check digit is validated and an NPI is used once."""
    practice = await create_practice("Rivera")
    npi = random_npi()
    problem(
        await practice.owner.post("/api/v1/providers", {"firstName": "A", "lastName": "B", "npi": "1234567890"}),
        400,
        "validation_failed",
    )
    expect(await practice.owner.post("/api/v1/providers", {"firstName": "A", "lastName": "B", "npi": npi}), 201)
    problem(
        await practice.owner.post("/api/v1/providers", {"firstName": "C", "lastName": "D", "npi": npi}),
        409,
        "npi_in_use",
    )


async def test_providers_can_be_searched_by_name_and_specialty() -> None:
    """Search by name and specialty, literal wildcards and sort validation."""
    practice = await create_practice("Rivera")
    expect(
        await practice.owner.post(
            "/api/v1/providers",
            {
                "firstName": "Priya",
                "lastName": "Natarajan",
                "specialties": [{"name": "Cardiology", "isPrimary": True}],
            },
        ),
        201,
    )
    by_name = read(await practice.owner.get("/api/v1/providers?search=nata"), 200)
    by_specialty = read(await practice.owner.get("/api/v1/providers?specialty=family"), 200)
    wildcard = read(await practice.owner.get("/api/v1/providers?search=%25"), 200)
    assert len(by_name["items"]) == 1 and by_name["items"][0]["lastName"] == "Natarajan", by_name["items"]
    assert len(by_specialty["items"]) == 1, by_specialty["items"]
    assert by_specialty["items"][0]["id"] == practice.provider_id, by_specialty["items"]
    assert len(wildcard["items"]) == 0, "wildcards must be matched literally"
    problem(await practice.owner.get("/api/v1/providers?sort=password"), 400, "validation_failed")


async def test_provider_updates_replace_specialties_and_use_optimistic_concurrency() -> None:
    """Profile updates replace specialties, list the primary first and check the version."""
    practice = await create_practice("Rivera")
    path = f"/api/v1/providers/{practice.provider_id}"
    current = read(await practice.owner.get(path), 200)
    updated = read(
        await practice.owner.put(
            path,
            {
                "firstName": "Elena",
                "lastName": "Rivera",
                "acceptingNewPatients": True,
                "listedInDirectory": True,
                "specialties": [{"name": "Pediatrics"}, {"name": "Family Medicine", "isPrimary": True}],
                "version": current["version"],
            },
        ),
        200,
    )
    specialties = updated["specialties"]
    assert len(specialties) == 2 and specialties[0]["name"] == "Family Medicine", (
        f"the primary specialty is listed first: {specialties}"
    )
    problem(
        await practice.owner.put(path, {"firstName": "Elena", "lastName": "Rivera", "version": current["version"]}),
        409,
        "concurrency_conflict",
    )
    problem(
        await practice.owner.put(
            path,
            {
                "firstName": "Elena",
                "lastName": "Rivera",
                "specialties": [{"name": "Same"}, {"name": "Same"}],
                "version": updated["version"],
            },
        ),
        409,
        "duplicate_specialty",
    )


async def test_weekly_schedules_cannot_overlap_and_must_be_at_an_assigned_location() -> None:
    """Weekly blocks cannot overlap and must be at one of the provider's locations."""
    practice = await create_practice("Rivera")
    owner = practice.owner
    today = utc_today().isoformat()
    problem(
        await owner.post(
            "/api/v1/provider-schedules",
            {
                "providerId": practice.provider_id,
                "locationId": practice.location_id,
                "dayOfWeek": 1,
                "startTime": "16:00",
                "endTime": "18:00",
                "effectiveFrom": today,
            },
        ),
        409,
        "schedule_overlap",
    )
    other = read(
        await owner.post(
            f"/api/v1/clinics/{practice.clinic_id}/locations",
            {
                "name": "Satellite",
                "addressLine1": "5 Test Road",
                "city": "Austin",
                "state": "TX",
                "postalCode": "78702",
                "timeZone": "America/Chicago",
            },
        ),
        201,
    )
    problem(
        await owner.post(
            "/api/v1/provider-schedules",
            {
                "providerId": practice.provider_id,
                "locationId": other["id"],
                "dayOfWeek": 1,
                "startTime": "18:00",
                "endTime": "20:00",
                "effectiveFrom": today,
            },
        ),
        422,
        "provider_not_at_location",
    )
    schedules = read(await owner.get(f"/api/v1/provider-schedules?providerId={practice.provider_id}"), 200)
    assert len(schedules) == 7, f"expected 7 blocks, got {len(schedules)}"
    monday = next(schedule for schedule in schedules if schedule["dayOfWeek"] == 1)
    moved = read(
        await owner.put(
            f"/api/v1/provider-schedules/{monday['id']}",
            {
                "providerId": practice.provider_id,
                "locationId": practice.location_id,
                "dayOfWeek": 1,
                "startTime": "09:00",
                "endTime": "13:00",
                "slotDurationMinutes": 20,
                "effectiveFrom": monday["effectiveFrom"],
            },
        ),
        200,
    )
    assert moved["startTime"] == "09:00", moved["startTime"]


async def test_availability_reflects_working_hours_time_off_and_holidays() -> None:
    """Slots follow working hours, time off and clinic holidays."""
    practice = await create_practice("Rivera")
    owner = practice.owner
    open_slots = await slots(owner, practice, 3)
    assert len(open_slots) == 18, f"expected 18 slots, got {len(open_slots)}"
    for slot in open_slots:
        duration = parse_instant(slot["endsAt"]) - parse_instant(slot["startsAt"])
        assert slot["timeZone"] == "America/Chicago" and duration == timedelta(minutes=30), slot
    exception = read(
        await owner.post(
            "/api/v1/schedule-exceptions",
            {
                "providerId": practice.provider_id,
                "type": "time_off",
                "startsAt": open_slots[0]["startsAt"],
                "endsAt": open_slots[3]["startsAt"],
                "reason": "Conference",
            },
        ),
        201,
    )
    assert exception["affectedAppointments"] == 0, exception["affectedAppointments"]
    remaining = await slots(owner, practice, 3)
    assert len(remaining) == 15, f"expected 15 slots after time off, got {len(remaining)}"
    holiday = local_date(practice.time_zone, 4)
    expect(await owner.post("/api/v1/clinic-holidays", {"date": holiday, "name": "Clinic closure"}), 201)
    closed = await slots(owner, practice, 4)
    assert len(closed) == 0, f"no slots on a holiday, got {len(closed)}"
    until = (utc_today() + timedelta(days=44)).isoformat()
    problem(
        await owner.get(f"/api/v1/availability?providerId={practice.provider_id}&from={holiday}&to={until}"),
        400,
        "validation_failed",
    )


async def test_listed_providers_appear_in_the_cross_organization_directory() -> None:
    """Listed active providers appear in the directory with their public profile."""
    last_name = ("Directory" + uuid.uuid4().hex)[:20]
    practice = await create_practice(last_name)
    seeker = await create_user("seeker")
    results = read(
        await seeker.get(f"/api/v1/directory/providers?search={last_name}&specialty=family&city=aus&state=TX"), 200
    )
    items = results["items"]
    assert len(items) == 1 and items[0]["id"] == practice.provider_id, items
    assert items[0]["organizationId"] == practice.organization_id, items
    assert "Family Medicine" in items[0]["specialties"], "the specialty is listed"
    assert items[0]["locations"][0]["city"] == "Austin", "the location is listed"
    expect(await practice.owner.post(f"/api/v1/providers/{practice.provider_id}/deactivate"), 200)
    after = read(await seeker.get(f"/api/v1/directory/providers?search={last_name}"), 200)
    assert len(after["items"]) == 0, "inactive providers leave the directory"


async def test_front_desk_staff_register_patients_and_every_record_access_is_audited() -> None:
    """Patients get an MRN, can be searched and every read is audited."""
    practice = await create_practice("Rivera")
    receptionist = await add_member(practice.owner, practice.organization_id, "receptionist")
    patient = await create_patient(receptionist, "Whitaker")
    assert _MRN.match(patient["medicalRecordNumber"]), f"unexpected MRN {patient['medicalRecordNumber']}"
    patient_id = patient["id"]
    expect(await receptionist.get(f"/api/v1/patients/{patient_id}"), 200)
    search = read(await receptionist.get("/api/v1/patients?search=whit"), 200)
    assert len(search["items"]) == 1 and search["items"][0]["id"] == patient_id, search["items"]
    by_mrn = read(await receptionist.get(f"/api/v1/patients?search={patient['medicalRecordNumber']}"), 200)
    assert len(by_mrn["items"]) == 1, "the MRN search finds the patient"
    audit = read(await practice.owner.get(f"/api/v1/audit-events?resourceId={patient_id}&pageSize=100"), 200)
    events = audit["items"]
    created = any(e["action"] == "patient.created" and e["actorUserId"] == receptionist.user_id for e in events)
    read_event = any(e["action"] == "patient.read" and e["actorUserId"] == receptionist.user_id for e in events)
    assert created and read_event, f"missing audit events {events}"
    problem(await receptionist.get("/api/v1/audit-events"), 403, "forbidden")


async def test_audit_metadata_never_contains_patient_details() -> None:
    """Audit metadata is free of protected health information."""
    practice = await create_practice("Rivera")
    expect(await practice.owner.get("/api/v1/patients?search=Jordan"), 200)
    metadata: str | None = await env().scalar(
        "SELECT string_agg(coalesce(metadata::text, ''), ' ') FROM platform.audit_events WHERE organization_id = $1",
        uuid.UUID(practice.organization_id),
    )
    if metadata is not None:
        assert "jordan" not in metadata.lower() and "1984" not in metadata, f"PHI in audit metadata: {metadata}"


async def test_providers_only_see_patients_in_their_care() -> None:
    """Clinicians get minimum-necessary access to patients."""
    practice = await create_practice("Rivera")
    doctor = await add_member(practice.owner, practice.organization_id, "provider")
    doctor_provider = await create_active_provider(practice.owner, practice.location_id, "Chen", doctor.user_id)
    before = read(await doctor.get("/api/v1/patients"), 200)
    assert len(before["items"]) == 0, "no patients before assignment"
    problem(await doctor.get(f"/api/v1/patients/{practice.patient_id}"), 404, "not_found")
    expect(
        await practice.owner.post(
            f"/api/v1/patients/{practice.patient_id}/care-relationships",
            {"providerId": doctor_provider, "type": "primary_care"},
        ),
        201,
    )
    after = read(await doctor.get("/api/v1/patients"), 200)
    assert len(after["items"]) == 1 and after["items"][0]["id"] == practice.patient_id, after["items"]
    expect(await doctor.get(f"/api/v1/patients/{practice.patient_id}"), 200)
    problem(
        await doctor.post(
            "/api/v1/patients",
            {
                "firstName": "No",
                "lastName": "Access",
                "dateOfBirth": "2000-01-01",
                "administrativeGender": "unknown",
            },
        ),
        403,
        "forbidden",
    )


async def test_patients_can_register_themselves_and_manage_only_their_own_contact_details() -> None:
    """Portal self-service with locked identity fields."""
    practice = await create_practice("Rivera")
    portal, patient = await create_portal_patient(practice)
    patient_id = patient["id"]
    me = read(await portal.get("/api/v1/auth/me"), 200)
    profiles = me["patientProfiles"]
    assert len(profiles) == 1 and profiles[0]["patientId"] == patient_id, profiles
    visible = read(await portal.get("/api/v1/patients"), 200)
    assert len(visible["items"]) == 1 and visible["items"][0]["id"] == patient_id, visible["items"]
    problem(await portal.get(f"/api/v1/patients/{practice.patient_id}"), 404, "not_found")
    problem(
        await portal.put(
            f"/api/v1/patients/{patient_id}",
            {
                "firstName": patient["firstName"],
                "lastName": patient["lastName"],
                "dateOfBirth": "1970-01-01",
                "administrativeGender": "female",
                "version": patient["version"],
            },
        ),
        422,
        "identity_fields_locked",
    )
    updated = read(
        await portal.put(
            f"/api/v1/patients/{patient_id}",
            {
                "firstName": patient["firstName"],
                "lastName": patient["lastName"],
                "dateOfBirth": patient["dateOfBirth"],
                "administrativeGender": "female",
                "phone": "+1 555 010 0177",
                "address": {"line1": "9 Sample Lane", "city": "Austin", "state": "TX", "postalCode": "78703"},
                "version": patient["version"],
            },
        ),
        200,
    )
    assert updated["phone"] == "+1 555 010 0177" and updated["address"]["city"] == "Austin", updated
    again = {"dateOfBirth": "1990-02-03", "administrativeGender": "female"}
    problem(await portal.post("/api/v1/patients/self-registration", again), 409, "already_registered")


async def test_emergency_contacts_keep_a_single_primary_contact() -> None:
    """Adding a new primary contact demotes the previous one."""
    practice = await create_practice("Rivera")
    path = f"/api/v1/patients/{practice.patient_id}/emergency-contacts"
    expect(
        await practice.owner.post(
            path,
            {
                "name": "Alex Sample",
                "relationship": "spouse",
                "phone": "+1 555 010 0111",
                "isPrimary": True,
            },
        ),
        201,
    )
    expect(
        await practice.owner.post(
            path,
            {
                "name": "Robin Sample",
                "relationship": "sibling",
                "phone": "+1 555 010 0112",
                "isPrimary": True,
            },
        ),
        201,
    )
    contacts = read(await practice.owner.get(path), 200)
    primary = [contact for contact in contacts if contact["isPrimary"] is True]
    for contact in primary:
        assert contact["name"] == "Robin Sample", f"the newest primary wins: {contact}"
    assert len(contacts) == 2 and len(primary) == 1, contacts


async def test_consents_are_recorded_once_and_revocation_is_kept_in_history() -> None:
    """A consent is granted once, revoked once and history keeps both records."""
    practice = await create_practice("Rivera")
    path = f"/api/v1/patients/{practice.patient_id}/consents"
    consent = read(
        await practice.owner.post(
            path,
            {
                "type": "sms_communication",
                "method": "electronic",
                "documentVersion": "2026-01",
            },
        ),
        201,
    )
    problem(
        await practice.owner.post(path, {"type": "sms_communication", "method": "electronic"}),
        409,
        "consent_already_granted",
    )
    revoked = read(await practice.owner.post(f"{path}/{consent['id']}/revoke"), 200)
    assert revoked["status"] == "revoked", revoked
    problem(await practice.owner.post(f"{path}/{consent['id']}/revoke"), 409, "consent_already_revoked")
    expect(await practice.owner.post(path, {"type": "sms_communication", "method": "paper"}), 201)
    history = read(await practice.owner.get(path), 200)
    assert len(history) == 2, f"history keeps both records: {history}"


async def test_read_only_staff_can_view_but_not_change_patients() -> None:
    """The staff role reads patients but cannot register them."""
    practice = await create_practice("Rivera")
    staff = await add_member(practice.owner, practice.organization_id, "staff")
    expect(await staff.get(f"/api/v1/patients/{practice.patient_id}"), 200)
    problem(
        await staff.post(
            "/api/v1/patients",
            {
                "firstName": "A",
                "lastName": "B",
                "dateOfBirth": "2001-01-01",
                "administrativeGender": "male",
            },
        ),
        403,
        "forbidden",
    )


async def test_patient_input_is_validated() -> None:
    """Patient fields are validated, including nested address keys."""
    practice = await create_practice("Rivera")
    document = problem(
        await practice.owner.post(
            "/api/v1/patients",
            {
                "firstName": "",
                "lastName": "Doe",
                "dateOfBirth": (utc_today() + timedelta(days=2)).isoformat(),
                "administrativeGender": "female",
                "email": "bad",
                "address": {"state": "Texas", "postalCode": "ABCDE"},
            },
        ),
        400,
        "validation_failed",
    )
    for key in ("firstName", "dateOfBirth", "email", "address.state"):
        assert key in document["errors"], document["errors"]
    assert document["errors"]["firstName"][0] == "'First name' must not be empty.", document["errors"]["firstName"]


async def test_care_relationships_can_be_ended() -> None:
    """Ending a relationship removes the provider's access and records the end date."""
    practice = await create_practice("Rivera")
    doctor = await add_member(practice.owner, practice.organization_id, "provider")
    doctor_provider = await create_active_provider(practice.owner, practice.location_id, "Chen", doctor.user_id)
    path = f"/api/v1/patients/{practice.patient_id}/care-relationships"
    relationship = read(await practice.owner.post(path, {"providerId": doctor_provider, "type": "specialist"}), 201)
    problem(
        await practice.owner.post(path, {"providerId": doctor_provider, "type": "specialist"}),
        409,
        "relationship_exists",
    )
    expect(await doctor.get(f"/api/v1/patients/{practice.patient_id}"), 200)
    expect(await practice.owner.delete(f"{path}/{relationship['id']}"), 204)
    problem(await doctor.get(f"/api/v1/patients/{practice.patient_id}"), 404, "not_found")
    ended = await env().scalar(
        "SELECT ended_on IS NOT NULL FROM patient.patient_provider_relationships WHERE id = $1",
        uuid.UUID(relationship["id"]),
    )
    assert ended is True, "the relationship records its end date"
