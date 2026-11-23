"""Account scenarios: registration, sign-in, lockout, refresh rotation, sessions, recovery, MFA and suspension."""

import time

from healthcare.application.security import base32_decode, compute_code
from tests.integration.support import (
    create_platform_admin,
    create_user,
    env,
    expect,
    extract_token,
    new_client,
    problem,
    read,
    register,
    sign_in,
    unique_email,
)


async def test_a_new_user_can_register_verify_sign_in_and_read_their_profile() -> None:
    """The happy path of account creation."""
    client = await create_user("jane")
    me = read(await client.get("/api/v1/auth/me"), 200)
    user = me["user"]
    assert user["email"] == client.email and user["firstName"] == "Jane"
    assert user["emailVerified"] and not user["mfaEnabled"]
    assert me["memberships"] == [] and me["patientProfiles"] == []


async def test_sign_in_is_refused_until_the_email_is_verified() -> None:
    """Unverified accounts cannot obtain tokens."""
    client = new_client(unique_email("unverified"))
    await register(client, "Test")
    problem(
        await client.post("/api/v1/auth/login", {"email": client.email, "password": client.password}),
        403,
        "email_not_verified",
    )


async def test_wrong_passwords_and_unknown_accounts_receive_the_same_error() -> None:
    """Sign-in does not reveal whether an account exists."""
    client = await create_user("lookup")
    anonymous = new_client("anonymous@example.test")
    first = problem(
        await anonymous.post("/api/v1/auth/login", {"email": client.email, "password": "Not-The-Right-Password"}),
        401,
        "invalid_credentials",
    )
    second = problem(
        await anonymous.post(
            "/api/v1/auth/login", {"email": unique_email("nobody"), "password": "Whatever-Password-1"}
        ),
        401,
        "invalid_credentials",
    )
    assert first["detail"] == second["detail"]


async def test_registering_an_existing_email_looks_identical_and_notifies_the_owner() -> None:
    """Registration is enumeration-safe and the owner is told about the attempt."""
    existing = await create_user("existing")
    attacker = new_client(existing.email)
    expect(
        await attacker.post(
            "/api/v1/auth/register",
            {
                "email": existing.email.upper(),
                "password": "Another-Password-123",
                "firstName": "X",
                "lastName": "Y",
            },
        ),
        202,
    )
    await env().emails.wait_for(existing.email, "already have an account")
    await sign_in(existing)


async def test_the_account_locks_after_repeated_failed_sign_ins() -> None:
    """Five failed attempts lock the account even for the right password."""
    client = await create_user("lockout")
    anonymous = new_client("anonymous@example.test")
    for _ in range(5):
        await anonymous.post("/api/v1/auth/login", {"email": client.email, "password": "Wrong-Password-000"})
    problem(
        await anonymous.post("/api/v1/auth/login", {"email": client.email, "password": client.password}),
        401,
        "account_locked",
    )


async def test_refresh_tokens_rotate_and_a_replayed_token_revokes_the_session() -> None:
    """Refresh tokens are single use; replaying one revokes the whole session."""
    client = await create_user("refresh")
    original = client.refresh_token
    rotated = read(await client.post("/api/v1/auth/refresh", {"refreshToken": original}), 200)
    assert rotated["refreshToken"] != original
    problem(await client.post("/api/v1/auth/refresh", {"refreshToken": original}), 401, "invalid_refresh_token")
    # The replay is treated as token theft: the whole session, including the newest token, is revoked.
    problem(
        await client.post("/api/v1/auth/refresh", {"refreshToken": rotated["refreshToken"]}), 401, "session_expired"
    )
    client.access_token = rotated["accessToken"]
    problem(await client.get("/api/v1/auth/me"), 401, "session_expired")


async def test_logging_out_invalidates_the_access_token_immediately() -> None:
    """Sign-out takes effect at once."""
    client = await create_user("logout")
    expect(await client.get("/api/v1/auth/me"), 200)
    expect(await client.post("/api/v1/auth/logout"), 204)
    problem(await client.get("/api/v1/auth/me"), 401, "session_expired")
    problem(await client.post("/api/v1/auth/refresh", {"refreshToken": client.refresh_token}), 401, "session_expired")


async def test_users_can_list_and_revoke_their_sessions() -> None:
    """A user sees both sessions and can end the other one."""
    laptop = await create_user("sessions")
    phone = new_client(laptop.email)
    await sign_in(phone)
    sessions = read(await laptop.get("/api/v1/auth/sessions"), 200)
    assert len(sessions) == 2
    other = sessions[1] if sessions[0]["current"] else sessions[0]
    expect(await laptop.delete(f"/api/v1/auth/sessions/{other['id']}"), 204)
    problem(await phone.get("/api/v1/auth/me"), 401, "session_expired")
    expect(await laptop.get("/api/v1/auth/me"), 200)


async def test_password_reset_replaces_the_password_and_ends_existing_sessions() -> None:
    """The recovery flow sets a new password, signs out everywhere and cannot be replayed."""
    client = await create_user("reset")
    anonymous = new_client(client.email)
    expect(await anonymous.post("/api/v1/auth/password/forgot", {"email": client.email}), 202)
    token = extract_token((await env().emails.wait_for(client.email, "Reset your password")).body)
    expect(
        await anonymous.post("/api/v1/auth/password/reset", {"token": token, "newPassword": "A-Brand-New-Password-2"}),
        204,
    )
    problem(await client.get("/api/v1/auth/me"), 401, "session_expired")
    problem(
        await anonymous.post("/api/v1/auth/login", {"email": client.email, "password": client.password}),
        401,
        "invalid_credentials",
    )
    problem(
        await anonymous.post("/api/v1/auth/password/reset", {"token": token, "newPassword": "Yet-Another-Password-3"}),
        400,
        "invalid_token",
    )
    client.password = "A-Brand-New-Password-2"
    await sign_in(client)


async def test_forgot_password_does_not_reveal_whether_an_account_exists() -> None:
    """Unknown addresses get the same answer."""
    expect(
        await new_client("anonymous@example.test").post(
            "/api/v1/auth/password/forgot", {"email": unique_email("ghost")}
        ),
        202,
    )


async def test_multi_factor_authentication_is_enrolled_and_required_at_sign_in() -> None:
    """TOTP enrollment, replay protection and single-use recovery codes."""
    client = await create_user("mfa")
    enrollment = read(await client.post("/api/v1/auth/mfa/totp/enroll"), 200)
    secret = base32_decode(enrollment["secret"])
    step = int(time.time()) // 30
    recovery = read(await client.post("/api/v1/auth/mfa/totp/confirm", {"code": compute_code(secret, step)}), 200)
    assert len(recovery["recoveryCodes"]) == 10

    anonymous = new_client(client.email)
    first = read(await anonymous.post("/api/v1/auth/login", {"email": client.email, "password": client.password}), 200)
    assert first["mfaRequired"] and "accessToken" not in first
    # The code used for enrollment cannot be replayed; the next time step is accepted.
    problem(
        await anonymous.post(
            "/api/v1/auth/mfa/challenge", {"mfaToken": first["mfaToken"], "code": compute_code(secret, step)}
        ),
        401,
        "invalid_mfa_code",
    )
    completed = read(
        await anonymous.post(
            "/api/v1/auth/mfa/challenge",
            {
                "mfaToken": first["mfaToken"],
                "code": compute_code(secret, step + 1),
            },
        ),
        200,
    )
    assert completed["accessToken"]
    second = read(await anonymous.post("/api/v1/auth/login", {"email": client.email, "password": client.password}), 200)
    with_recovery = read(
        await anonymous.post(
            "/api/v1/auth/mfa/challenge",
            {
                "mfaToken": second["mfaToken"],
                "recoveryCode": recovery["recoveryCodes"][0].lower(),
            },
        ),
        200,
    )
    assert with_recovery["accessToken"]
    third = read(await anonymous.post("/api/v1/auth/login", {"email": client.email, "password": client.password}), 200)
    problem(
        await anonymous.post(
            "/api/v1/auth/mfa/challenge",
            {
                "mfaToken": third["mfaToken"],
                "recoveryCode": recovery["recoveryCodes"][0],
            },
        ),
        401,
        "invalid_mfa_code",
    )


async def test_a_suspended_account_is_signed_out_and_cannot_sign_in_until_reinstated() -> None:
    """Suspension takes effect immediately and reinstatement restores access."""
    admin = await create_platform_admin()
    user = await create_user("suspended")
    expect(await admin.post(f"/api/v1/users/{user.user_id}/suspend"), 204)
    problem(await user.get("/api/v1/auth/me"), 401, "session_expired")
    problem(
        await new_client("x@example.test").post("/api/v1/auth/login", {"email": user.email, "password": user.password}),
        403,
        "account_suspended",
    )
    expect(await admin.post(f"/api/v1/users/{user.user_id}/reinstate"), 204)
    await sign_in(user)


async def test_only_platform_administrators_can_suspend_accounts() -> None:
    """Regular users cannot suspend accounts."""
    user = await create_user("regular")
    other = await create_user("target")
    problem(await user.post(f"/api/v1/users/{other.user_id}/suspend"), 403, "forbidden")


async def test_invalid_input_returns_field_level_validation_errors() -> None:
    """Validation problems carry the reference's field keys and messages."""
    document = problem(
        await new_client("x@example.test").post(
            "/api/v1/auth/register",
            {
                "email": "not-an-email",
                "password": "short",
                "firstName": "",
                "lastName": "Doe",
            },
        ),
        400,
        "validation_failed",
    )
    for key in ("email", "password", "firstName"):
        assert key in document["errors"], document["errors"]
    assert document["errors"]["email"][0] == "'Email' is not a valid email address."


async def test_malformed_json_is_rejected_without_exposing_parser_details() -> None:
    """Binding errors never leak parser internals."""
    document = problem(
        await new_client("x@example.test").send("POST", "/api/v1/auth/login", '{"email": '), 400, "validation_failed"
    )
    for messages in document["errors"].values():
        for message in messages:
            assert "json" not in message and "System." not in message and "Traceback" not in message
    assert document["errors"]["email"][0] == "The value is missing or not valid."
    assert document["errors"]["request"][0] == "The request field is required."


async def test_users_can_update_their_profile_and_change_their_password() -> None:
    """Profile and password changes."""
    client = await create_user("profile")
    profile = read(
        await client.put("/api/v1/auth/me", {"firstName": "Morgan", "lastName": "Lee", "phone": "+1 555 010 0123"}), 200
    )
    assert profile["firstName"] == "Morgan"
    problem(
        await client.post(
            "/api/v1/auth/password/change",
            {
                "currentPassword": "Not-My-Password-1",
                "newPassword": "Changed-Password-456",
            },
        ),
        400,
        "validation_failed",
    )
    expect(
        await client.post(
            "/api/v1/auth/password/change",
            {
                "currentPassword": client.password,
                "newPassword": "Changed-Password-456",
            },
        ),
        204,
    )
    expect(await client.get("/api/v1/auth/me"), 200)
    client.password = "Changed-Password-456"
    await sign_in(client)
