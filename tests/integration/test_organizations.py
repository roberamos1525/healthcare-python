"""Organization scenarios: ownership, settings, invitations, memberships, platform administration and clinics."""

import re
from urllib.parse import quote

from tests.integration.support import (
    add_member,
    create_organization,
    create_platform_admin,
    create_user,
    env,
    expect,
    extract_token,
    problem,
    read,
    unique_email,
)

_SLUG = re.compile(r"^[a-z0-9-]+$")


async def test_creating_an_organization_makes_the_creator_its_owner() -> None:
    """Registering an organization gives the creator an owner membership."""
    owner, organization = await create_organization("America/Chicago")
    me = read(await owner.get("/api/v1/auth/me"), 200)
    memberships = me["memberships"]
    assert len(memberships) == 1, memberships
    assert memberships[0]["organizationId"] == organization["id"] and memberships[0]["roles"] == ["owner"], memberships
    assert "organization.manage" in memberships[0]["permissions"], "owners manage the organization"
    assert organization["version"] == 1 and _SLUG.match(organization["slug"]), organization
    listed = read(await owner.get("/api/v1/organizations"), 200)
    assert len(listed["items"]) == 1 and listed["items"][0]["id"] == organization["id"], listed


async def test_updates_use_optimistic_concurrency() -> None:
    """Stale versions are rejected."""
    owner, organization = await create_organization("America/Chicago")
    path = f"/api/v1/organizations/{organization['id']}"
    updated = read(
        await owner.put(
            path,
            {
                "name": "Renamed Practice",
                "type": "multi_location_group",
                "timeZone": "America/Denver",
                "version": 1,
            },
        ),
        200,
    )
    assert updated["version"] == 2 and updated["type"] == "multi_location_group", updated
    stale = {"name": "Stale Write", "type": "clinic", "timeZone": "America/Denver", "version": 1}
    problem(await owner.put(path, stale), 409, "concurrency_conflict")


async def test_booking_policy_settings_can_be_read_and_validated() -> None:
    """Settings have defaults, offsets are ordered and ranges are checked."""
    owner, organization = await create_organization("America/Chicago")
    path = f"/api/v1/organizations/{organization['id']}/settings"
    defaults = read(await owner.get(path), 200)
    assert defaults["defaultAppointmentDurationMinutes"] == 30, defaults
    assert defaults["allowPatientSelfScheduling"] is True, defaults
    updated = read(
        await owner.put(
            path,
            {
                "defaultAppointmentDurationMinutes": 20,
                "minimumBookingNoticeMinutes": 120,
                "bookingHorizonDays": 60,
                "patientCancellationWindowHours": 12,
                "reminderOffsetsMinutes": [60, 2880],
                "allowPatientSelfScheduling": True,
                "autoConfirmPatientBookings": True,
            },
        ),
        200,
    )
    offsets = updated["reminderOffsetsMinutes"]
    assert offsets[0] == 2880 and offsets[1] == 60, f"offsets must be ordered descending: {offsets}"
    document = problem(
        await owner.put(
            path,
            {
                "defaultAppointmentDurationMinutes": 7,
                "minimumBookingNoticeMinutes": -1,
                "bookingHorizonDays": 0,
                "patientCancellationWindowHours": 12,
                "reminderOffsetsMinutes": [5],
                "allowPatientSelfScheduling": True,
                "autoConfirmPatientBookings": False,
            },
        ),
        400,
        "validation_failed",
    )
    for key in ("bookingHorizonDays", "reminderOffsetsMinutes"):
        assert key in document["errors"], document["errors"]


async def test_invited_staff_join_with_the_roles_they_were_given() -> None:
    """Invitations are accepted with the invited roles."""
    owner, organization = await create_organization("America/Chicago")
    organization_id = organization["id"]
    receptionist = await add_member(owner, organization_id, "receptionist")
    me = read(await receptionist.get("/api/v1/auth/me"), 200)
    assert len(me["memberships"]) == 1 and me["memberships"][0]["roles"] == ["receptionist"], me["memberships"]
    members = read(await owner.get(f"/api/v1/organizations/{organization_id}/members"), 200)
    assert members["totalItems"] == 2, f"expected 2 members, got {members['totalItems']}"
    invitations = read(await owner.get(f"/api/v1/organizations/{organization_id}/invitations"), 200)
    assert len(invitations["items"]) == 1 and invitations["items"][0]["status"] == "accepted", invitations


async def test_an_invitation_can_only_be_accepted_by_the_invited_address() -> None:
    """Invitations are bound to the invited email and forged tokens are rejected."""
    owner, organization = await create_organization("America/Chicago")
    organization_id = organization["id"]
    invited = unique_email("invited")
    expect(
        await owner.post(
            f"/api/v1/organizations/{organization_id}/invitations", {"email": invited, "roles": ["clinic_admin"]}
        ),
        201,
    )
    token = extract_token((await env().emails.wait_for(invited, "invited")).body)
    intruder = await create_user("intruder")
    problem(await intruder.post("/api/v1/invitations/accept", {"token": token}), 403, "invitation_email_mismatch")
    forged = organization_id.replace("-", "") + ".forged-token-value"
    problem(await intruder.post("/api/v1/invitations/accept", {"token": forged}), 400, "invalid_invitation")


async def test_staff_without_management_permissions_cannot_administer_the_organization() -> None:
    """Owner-only and admin-only operations are refused to front-desk staff."""
    owner, organization = await create_organization("America/Chicago")
    organization_id = organization["id"]
    receptionist = await add_member(owner, organization_id, "receptionist")
    problem(
        await receptionist.post(
            f"/api/v1/organizations/{organization_id}/invitations", {"email": unique_email("x"), "roles": ["staff"]}
        ),
        403,
        "forbidden",
    )
    problem(
        await receptionist.put(
            f"/api/v1/organizations/{organization_id}",
            {
                "name": "Hijacked",
                "type": "clinic",
                "timeZone": "America/Chicago",
                "version": 1,
            },
        ),
        403,
        "forbidden",
    )
    problem(await receptionist.post("/api/v1/clinics", {"name": "Unauthorized Clinic"}), 403, "forbidden")


async def test_the_last_owner_cannot_be_removed_or_demoted() -> None:
    """An organization always keeps an owner."""
    owner, organization = await create_organization("America/Chicago")
    organization_id = organization["id"]
    members = read(await owner.get(f"/api/v1/organizations/{organization_id}/members"), 200)
    membership = members["items"][0]["membershipId"]
    path = f"/api/v1/organizations/{organization_id}/members/{membership}"
    problem(await owner.put(path, {"roles": ["clinic_admin"], "status": "active"}), 422, "last_owner")
    problem(await owner.delete(path), 422, "last_owner")


async def test_clinic_administrators_cannot_change_an_owner_and_role_changes_apply_immediately() -> None:
    """Only owners change owners and role changes take effect at once."""
    owner, organization = await create_organization("America/Chicago")
    organization_id = organization["id"]
    admin = await add_member(owner, organization_id, "clinic_admin")
    members = read(await owner.get(f"/api/v1/organizations/{organization_id}/members"), 200)
    owner_membership = admin_membership = ""
    for member in members["items"]:
        if member["userId"] == owner.user_id:
            owner_membership = member["membershipId"]
        if member["userId"] == admin.user_id:
            admin_membership = member["membershipId"]
    members_path = f"/api/v1/organizations/{organization_id}/members"
    problem(
        await admin.put(f"{members_path}/{owner_membership}", {"roles": ["staff"], "status": "active"}),
        403,
        "owner_required",
    )
    expect(await admin.get(members_path), 200)
    expect(await owner.put(f"{members_path}/{admin_membership}", {"roles": ["staff"], "status": "active"}), 200)
    problem(await admin.post("/api/v1/clinics", {"name": "No Longer Allowed"}), 403, "forbidden")


async def test_platform_administrators_can_list_and_suspend_organizations() -> None:
    """Platform administration lists every organization and suspension applies immediately."""
    admin = await create_platform_admin()
    owner, organization = await create_organization("America/Chicago")
    organization_id = organization["id"]
    expect(await owner.get(f"/api/v1/organizations/{organization_id}"), 200)
    listed = read(await admin.get(f"/api/v1/organizations?all=true&search={quote(organization['name'], safe='')}"), 200)
    assert any(item["id"] == organization_id for item in listed["items"]), (
        "the organization must be listed for administrators"
    )
    problem(await owner.get("/api/v1/organizations?all=true"), 403, "forbidden")
    suspended = read(await admin.post(f"/api/v1/organizations/{organization_id}/suspend"), 200)
    assert suspended["status"] == "suspended", suspended["status"]
    problem(await owner.get("/api/v1/clinics"), 403, "organization_suspended")
    expect(await admin.post(f"/api/v1/organizations/{organization_id}/reinstate"), 200)
    expect(await owner.get("/api/v1/clinics"), 200)


async def test_clinics_and_locations_are_managed_with_validated_addresses_and_time_zones() -> None:
    """Clinics, locations and holidays are managed with validated input."""
    owner, _ = await create_organization("America/Chicago")
    clinic = read(await owner.post("/api/v1/clinics", {"name": "North Clinic", "phone": "+1 555 010 0150"}), 201)
    clinic_id = clinic["id"]
    document = problem(
        await owner.post(
            f"/api/v1/clinics/{clinic_id}/locations",
            {
                "name": "Bad",
                "addressLine1": "1 Main",
                "city": "Austin",
                "state": "XX",
                "postalCode": "7870",
                "timeZone": "Central Standard Time",
            },
        ),
        400,
        "validation_failed",
    )
    for key in ("state", "postalCode", "timeZone"):
        assert key in document["errors"], document["errors"]
    location = read(
        await owner.post(
            f"/api/v1/clinics/{clinic_id}/locations",
            {
                "name": "Uptown",
                "addressLine1": "200 Test Avenue",
                "city": "Dallas",
                "state": "TX",
                "postalCode": "75201",
                "timeZone": "America/Chicago",
            },
        ),
        201,
    )
    location_id = location["id"]
    renamed = read(
        await owner.put(
            f"/api/v1/locations/{location_id}",
            {
                "name": "Uptown Annex",
                "addressLine1": "200 Test Avenue",
                "city": "Dallas",
                "state": "TX",
                "postalCode": "75201-1234",
                "timeZone": "America/Chicago",
                "status": "active",
                "version": location["version"],
            },
        ),
        200,
    )
    assert renamed["name"] == "Uptown Annex" and renamed["version"] == 2, renamed
    locations = read(await owner.get(f"/api/v1/clinics/{clinic_id}/locations"), 200)
    assert len(locations["items"]) == 1 and locations["items"][0]["id"] == location_id, locations
    holiday = read(
        await owner.post(
            "/api/v1/clinic-holidays", {"date": "2030-12-25", "name": "Christmas Day", "locationId": location_id}
        ),
        201,
    )
    again = {"date": "2030-12-25", "name": "Again", "locationId": location_id}
    problem(await owner.post("/api/v1/clinic-holidays", again), 409, "holiday_exists")
    expect(await owner.delete(f"/api/v1/clinic-holidays/{holiday['id']}"), 204)


async def test_tenant_endpoints_require_an_organization_to_be_selected() -> None:
    """Tenant endpoints answer organization_required and invalid_organization_id."""
    owner, _ = await create_organization("America/Chicago")
    owner.organization = ""
    problem(await owner.get("/api/v1/clinics"), 400, "organization_required")
    owner.organization = "not-a-uuid"
    problem(await owner.get("/api/v1/clinics"), 400, "invalid_organization_id")
