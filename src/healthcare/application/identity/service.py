"""Account use cases: registration, sign-in, MFA, refresh, password recovery, sessions and suspension."""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from healthcare.application.access import AccessResolver
from healthcare.application.core.audit import AuditEntry, audit, truncate
from healthcare.application.core.caller import Caller
from healthcare.application.core.errors import (
    AppError,
    authentication_required,
    bad_request,
    conflict,
    field_error,
    forbidden,
    not_found,
    rule,
    unauthorized,
)
from healthcare.application.core.ports import (
    Clock,
    Database,
    FieldEncryptor,
    Mailer,
    PasswordHasher,
    Row,
    TokenIssuer,
    Tx,
    new_id,
)
from healthcare.application.core.validation import is_blank, trimmed
from healthcare.application.identity.contracts import (
    ChangePasswordRequest,
    ConfirmTotpRequest,
    CurrentUserResponse,
    DisableTotpRequest,
    EmailRequest,
    LoginRequest,
    LoginResponse,
    MembershipSummary,
    MfaChallengeRequest,
    PatientProfileSummary,
    RecoveryCodesResponse,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SessionView,
    TokenResponse,
    TotpEnrollmentResponse,
    UpdateProfileRequest,
    UserProfile,
    VerifyEmailRequest,
)
from healthcare.application.security import (
    base32_decode,
    base32_encode,
    build_otpauth_uri,
    generate_recovery_code,
    generate_token,
    hash_token,
    verify_code,
)
from healthcare.domain.authorization import sorted_permissions
from healthcare.domain.enums import AuditOutcome, UserStatus, UserTokenPurpose
from healthcare.domain.rules import is_locked_out, register_failed_sign_in

RECOVERY_CODE_COUNT: Final = 10

_USER_COLUMNS: Final = (
    "id, email, email_normalized, password_hash, first_name, last_name, phone, status, email_verified_at, "
    "platform_role, mfa_enabled, failed_login_count, lockout_until, created_at"
)


@dataclass(frozen=True, slots=True)
class AuthPolicy:
    """The authentication and session policy."""

    access_token_lifetime: timedelta
    session_idle_timeout: timedelta
    session_absolute: timedelta
    refresh_reuse_grace: timedelta
    max_failed_sign_ins: int
    lockout: timedelta
    email_verification_life: timedelta
    password_reset_life: timedelta
    mfa_challenge_life: timedelta
    mfa_issuer_name: str


def normalize_email(email: str) -> str:
    """Trim and lower-case an email address for comparison and storage."""
    return email.strip().lower()


def _profile(user: Row) -> UserProfile:
    """Build the public profile of a stored user."""
    return UserProfile.model_construct(
        id=user.id,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        phone=user.phone,
        status=UserStatus(user.status),
        email_verified=user.email_verified_at is not None,
        mfa_enabled=user.mfa_enabled,
        platform_role=user.platform_role,
        created_at=user.created_at,
    )


def _invalid_credentials() -> AppError:
    """Return the generic error for every failed password check."""
    return unauthorized("invalid_credentials", "The email or password is incorrect.")


def _invalid_refresh_token() -> AppError:
    """Return the generic error for every rejected refresh token."""
    return unauthorized("invalid_refresh_token", "Your session is no longer valid. Sign in again.")


def _invalid_link() -> AppError:
    """Return the error for unusable verification and reset links."""
    return bad_request("invalid_token", "This link is invalid or has expired. Request a new one.")


def _ensure_can_sign_in(user: Row) -> None:
    """Reject accounts that are unverified, suspended or deactivated."""
    if user.status == UserStatus.PENDING_VERIFICATION:
        raise forbidden("email_not_verified", "Verify your email address before signing in.")
    if user.status in (UserStatus.SUSPENDED, UserStatus.DEACTIVATED):
        raise forbidden("account_suspended", "This account is not active. Contact support for help.")


async def _find_user_by_email(tx: Tx, normalized: str) -> Row | None:
    """Load a user by normalized email."""
    return await tx.fetchrow(
        f"SELECT {_USER_COLUMNS} FROM identity.users WHERE email_normalized = :email", {"email": normalized}
    )


async def _find_user_by_id(tx: Tx, user_id: UUID) -> Row | None:
    """Load a user by id."""
    return await tx.fetchrow(f"SELECT {_USER_COLUMNS} FROM identity.users WHERE id = :id", {"id": user_id})


async def _create_token(tx: Tx, user_id: UUID, purpose: UserTokenPurpose, lifetime: timedelta, now: datetime) -> str:
    """Create a single-use token, consuming earlier unused tokens of the purpose, and return the raw value."""
    await tx.execute(
        """
        UPDATE identity.user_tokens SET consumed_at = :now
        WHERE user_id = :user AND purpose = :purpose AND consumed_at IS NULL
        """,
        {"user": user_id, "purpose": purpose.value, "now": now},
    )
    raw = generate_token(32)
    await tx.execute(
        """
        INSERT INTO identity.user_tokens (id, user_id, purpose, token_hash, expires_at, created_at)
        VALUES (:id, :user, :purpose, :hash, :expires, :now)
        """,
        {
            "id": new_id(),
            "user": user_id,
            "purpose": purpose.value,
            "hash": hash_token(raw),
            "expires": now + lifetime,
            "now": now,
        },
    )
    return raw


async def _find_usable_token(tx: Tx, raw: str, purpose: UserTokenPurpose, now: datetime) -> Row | None:
    """Find an unexpired, unused token by its raw value."""
    token = await tx.fetchrow(
        """
        SELECT id, user_id, consumed_at, expires_at FROM identity.user_tokens
        WHERE token_hash = :hash AND purpose = :purpose
        """,
        {"hash": hash_token(raw), "purpose": purpose.value},
    )
    if token is None or token.consumed_at is not None or not now < token.expires_at:
        return None
    return token


async def _consume_token(tx: Tx, token_id: UUID, now: datetime) -> bool:
    """Mark a token as used; return False when another request used it first."""
    affected = await tx.execute(
        "UPDATE identity.user_tokens SET consumed_at = :now WHERE id = :id AND consumed_at IS NULL",
        {"id": token_id, "now": now},
    )
    return affected == 1


async def _revoke_sessions(tx: Tx, user_id: UUID, reason: str, except_id: UUID | None, now: datetime) -> list[UUID]:
    """Revoke the user's active sessions (except one) and return their ids for cache invalidation."""
    rows = await tx.fetch(
        """
        UPDATE identity.sessions SET revoked_at = :now, revoked_reason = :reason
        WHERE user_id = :user AND revoked_at IS NULL AND (CAST(:except AS uuid) IS NULL OR id <> :except)
        RETURNING id
        """,
        {"user": user_id, "except": except_id, "now": now, "reason": reason},
    )
    return [row.id for row in rows]


async def _mark_email_verified(tx: Tx, user_id: UUID, now: datetime) -> None:
    """Record the verification time and activate an account waiting for verification."""
    await tx.execute(
        """
        UPDATE identity.users
        SET email_verified_at = coalesce(email_verified_at, :now),
            status = CASE WHEN status = 'pending_verification' THEN 'active' ELSE status END,
            updated_at = :now
        WHERE id = :id
        """,
        {"id": user_id, "now": now},
    )


class IdentityService:
    """Implements the account use cases."""

    def __init__(
        self,
        db: Database,
        hasher: PasswordHasher,
        tokens: TokenIssuer,
        mailer: Mailer,
        encryptor: FieldEncryptor,
        access: AccessResolver,
        clock: Clock,
        policy: AuthPolicy,
    ) -> None:
        """Wire the service to its ports."""
        self._db = db
        self._hasher = hasher
        self._tokens = tokens
        self._mailer = mailer
        self._encryptor = encryptor
        self._access = access
        self._clock = clock
        self._policy = policy

    async def _invalidate_sessions(self, session_ids: list[UUID]) -> None:
        """Remove cached state of revoked sessions."""
        for session_id in session_ids:
            await self._access.invalidate_session(session_id)

    async def register(self, caller: Caller, request: RegisterRequest) -> None:
        """Create an account awaiting verification; the outcome never reveals whether the address exists."""
        now = self._clock()
        email = normalize_email(request.email)
        async with self._db.transaction(caller.scope()) as tx:
            existing = await _find_user_by_email(tx, email)
        if existing is not None:
            await self._hasher.simulate(request.password)
            await self._notify_existing_account(caller, existing, now)
            return
        password_hash = await self._hasher.hash(request.password)
        user_id = new_id()
        try:
            async with self._db.transaction(caller.scope()) as tx:
                await tx.execute(
                    """
                    INSERT INTO identity.users (id, email, email_normalized, password_hash, first_name, last_name, phone,
                        status, mfa_enabled, failed_login_count, created_at, updated_at)
                    VALUES (:id, :email, :normalized, :hash, :first, :last, :phone, 'pending_verification', false, 0,
                        :now, :now)
                    """,
                    {
                        "id": user_id,
                        "email": request.email.strip(),
                        "normalized": email,
                        "hash": password_hash,
                        "first": request.first_name.strip(),
                        "last": request.last_name.strip(),
                        "phone": trimmed(request.phone),
                        "now": now,
                    },
                )
                token = await _create_token(
                    tx, user_id, UserTokenPurpose.EMAIL_VERIFICATION, self._policy.email_verification_life, now
                )
                await audit(tx, caller, now, AuditEntry("auth.register", "user", user_id, actor_user_id=user_id))
        except AppError as error:
            if error.code == "email_in_use":
                # A concurrent registration for the same address won the race; respond identically.
                return
            raise
        self._mailer.queue_email_verification(request.email.strip(), request.first_name.strip(), token)

    async def _notify_existing_account(self, caller: Caller, existing: Row, now: datetime) -> None:
        """Handle a registration attempt for an address that already has an account."""
        token: str | None = None
        async with self._db.transaction(caller.scope()) as tx:
            if existing.status == UserStatus.PENDING_VERIFICATION:
                token = await _create_token(
                    tx, existing.id, UserTokenPurpose.EMAIL_VERIFICATION, self._policy.email_verification_life, now
                )
            await audit(
                tx,
                caller,
                now,
                AuditEntry(
                    "auth.register",
                    "user",
                    existing.id,
                    AuditOutcome.DENIED,
                    {"reason": "duplicate_email"},
                    existing.id,
                ),
            )
        if token is not None:
            self._mailer.queue_email_verification(existing.email, existing.first_name, token)
        else:
            self._mailer.queue_account_already_exists(existing.email, existing.first_name)

    async def verify_email(self, caller: Caller, request: VerifyEmailRequest) -> None:
        """Confirm ownership of an email address with the emailed token and activate the account."""
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            token = await _find_usable_token(tx, request.token, UserTokenPurpose.EMAIL_VERIFICATION, now)
            if token is None or not await _consume_token(tx, token.id, now):
                raise _invalid_link()
            await _mark_email_verified(tx, token.user_id, now)
            await audit(
                tx, caller, now, AuditEntry("auth.email_verified", "user", token.user_id, actor_user_id=token.user_id)
            )

    async def resend_verification(self, caller: Caller, request: EmailRequest) -> None:
        """Send a new verification email when the account exists and is still unverified."""
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            user = await _find_user_by_email(tx, normalize_email(request.email))
            if user is None or user.status != UserStatus.PENDING_VERIFICATION:
                return
            token = await _create_token(
                tx, user.id, UserTokenPurpose.EMAIL_VERIFICATION, self._policy.email_verification_life, now
            )
        self._mailer.queue_email_verification(user.email, user.first_name, token)

    async def login(self, caller: Caller, request: LoginRequest) -> LoginResponse:
        """Verify the password and start a session, or return an MFA challenge when MFA is enabled."""
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            user = await _find_user_by_email(tx, normalize_email(request.email))
        if user is None or user.password_hash is None:
            await self._hasher.simulate(request.password)
            async with self._db.transaction(caller.scope()) as tx:
                await audit(
                    tx,
                    caller,
                    now,
                    AuditEntry(
                        "auth.login", "user", outcome=AuditOutcome.DENIED, metadata={"reason": "unknown_account"}
                    ),
                )
            raise _invalid_credentials()
        if is_locked_out(user.lockout_until, now):
            async with self._db.transaction(caller.scope()) as tx:
                await audit(
                    tx,
                    caller,
                    now,
                    AuditEntry("auth.login", "user", user.id, AuditOutcome.DENIED, {"reason": "locked_out"}, user.id),
                )
            raise unauthorized(
                "account_locked", "Too many failed sign-in attempts. Try again later or reset your password."
            )

        verification = await self._hasher.verify(user.password_hash, request.password)
        if not verification.succeeded:
            async with self._db.transaction(caller.scope()) as tx:
                await self._register_failed_sign_in(tx, user, now)
                await audit(
                    tx,
                    caller,
                    now,
                    AuditEntry(
                        "auth.login", "user", user.id, AuditOutcome.DENIED, {"reason": "invalid_password"}, user.id
                    ),
                )
            raise _invalid_credentials()
        _ensure_can_sign_in(user)
        rehash = await self._hasher.hash(request.password) if verification.needs_rehash else None

        async with self._db.transaction(caller.scope()) as tx:
            if rehash is not None:
                await tx.execute(
                    "UPDATE identity.users SET password_hash = :hash, updated_at = :now WHERE id = :id",
                    {"id": user.id, "hash": rehash, "now": now},
                )
            if user.mfa_enabled:
                challenge = await _create_token(
                    tx, user.id, UserTokenPurpose.MFA_CHALLENGE, self._policy.mfa_challenge_life, now
                )
                return LoginResponse.model_construct(mfa_required=True, mfa_token=challenge)
            return await self._start_session(tx, caller, user, False, now)

    async def _register_failed_sign_in(self, tx: Tx, user: Row, now: datetime) -> None:
        """Count a failed sign-in and lock the account once the attempt limit is reached."""
        count, lockout = register_failed_sign_in(
            user.failed_login_count, now, self._policy.max_failed_sign_ins, self._policy.lockout
        )
        await tx.execute(
            """
            UPDATE identity.users SET failed_login_count = :count, lockout_until = :lockout, updated_at = :now
            WHERE id = :id
            """,
            {"id": user.id, "count": count, "lockout": lockout or user.lockout_until, "now": now},
        )

    async def _start_session(
        self, tx: Tx, caller: Caller, user: Row, mfa_verified: bool, now: datetime
    ) -> LoginResponse:
        """Create the session, record the sign-in and return the token pair."""
        refresh_token = generate_token(32)
        session_id = new_id()
        expires_at = now + self._policy.session_absolute
        idle_expires_at = min(now + self._policy.session_idle_timeout, expires_at)
        await tx.execute(
            """
            INSERT INTO identity.sessions (id, user_id, refresh_token_hash, created_at, last_used_at, idle_expires_at,
                expires_at, mfa_verified, ip_address, user_agent)
            VALUES (:id, :user, :hash, :now, :now, :idle, :expires, :mfa, :ip, :agent)
            """,
            {
                "id": session_id,
                "user": user.id,
                "hash": hash_token(refresh_token),
                "now": now,
                "idle": idle_expires_at,
                "expires": expires_at,
                "mfa": mfa_verified,
                "ip": truncate(caller.ip_address, 45),
                "agent": truncate(caller.user_agent, 300),
            },
        )
        await tx.execute(
            """
            UPDATE identity.users SET failed_login_count = 0, lockout_until = NULL, last_login_at = :now, updated_at = :now
            WHERE id = :id
            """,
            {"id": user.id, "now": now},
        )
        await audit(tx, caller, now, AuditEntry("auth.login", "session", session_id, actor_user_id=user.id))
        token = self._tokens.issue(user.id, user.platform_role, session_id)
        return LoginResponse.model_construct(
            mfa_required=False,
            access_token=token.value,
            refresh_token=refresh_token,
            token_type="Bearer",
            expires_in=token.expires_in,
            user=_profile(user),
        )

    async def complete_mfa_challenge(self, caller: Caller, request: MfaChallengeRequest) -> LoginResponse:
        """Complete a sign-in protected by MFA with a TOTP code or a single-use recovery code."""
        now = self._clock()
        failure: AppError | None = None
        response: LoginResponse | None = None
        async with self._db.transaction(caller.scope()) as tx:
            challenge = await _find_usable_token(tx, request.mfa_token, UserTokenPurpose.MFA_CHALLENGE, now)
            if challenge is None:
                raise unauthorized("invalid_mfa_token", "This sign-in attempt has expired. Sign in again.")
            user = await _find_user_by_id(tx, challenge.user_id)
            if user is None:
                raise unauthorized("invalid_mfa_token", "This sign-in attempt has expired. Sign in again.")
            _ensure_can_sign_in(user)
            if is_locked_out(user.lockout_until, now):
                raise unauthorized("account_locked", "Too many failed sign-in attempts. Try again later.")
            if is_blank(request.recovery_code):
                verified = await self._verify_totp(tx, user.id, request.code, now)
            else:
                verified = await self._use_recovery_code(tx, caller, user.id, request.recovery_code or "", now)
            if not verified:
                await self._register_failed_sign_in(tx, user, now)
                await audit(
                    tx,
                    caller,
                    now,
                    AuditEntry("auth.mfa_challenge", "user", user.id, AuditOutcome.DENIED, actor_user_id=user.id),
                )
                # The failure is recorded (committed) before the error is returned.
                failure = unauthorized("invalid_mfa_code", "The verification code is incorrect.")
            else:
                if not await _consume_token(tx, challenge.id, now):
                    raise unauthorized("invalid_mfa_token", "This sign-in attempt has expired. Sign in again.")
                response = await self._start_session(tx, caller, user, True, now)
        if failure is not None:
            raise failure
        assert response is not None
        return response

    async def refresh(self, caller: Caller, request: RefreshRequest) -> TokenResponse:
        """Exchange a refresh token for a new pair; replaying a rotated token revokes the whole session."""
        now = self._clock()
        presented = hash_token(request.refresh_token)
        failure: AppError | None = None
        reused_session: UUID | None = None
        response: TokenResponse | None = None
        async with self._db.transaction(caller.scope()) as tx:
            current = await tx.fetchrow(
                """
                SELECT id, user_id, refresh_token_hash, rotated_at, revoked_at, idle_expires_at, expires_at
                FROM identity.sessions WHERE refresh_token_hash = :hash OR previous_refresh_token_hash = :hash
                """,
                {"hash": presented},
            )
            if current is None:
                failure = _invalid_refresh_token()
            elif current.refresh_token_hash != presented:
                failure = _invalid_refresh_token()
                within_grace = (
                    current.rotated_at is not None and now - current.rotated_at <= self._policy.refresh_reuse_grace
                )
                if not within_grace and current.revoked_at is None:
                    await tx.execute(
                        """
                        UPDATE identity.sessions SET revoked_at = :now, revoked_reason = 'refresh_token_reuse'
                        WHERE id = :id AND revoked_at IS NULL
                        """,
                        {"id": current.id, "now": now},
                    )
                    reused_session = current.id
                    await audit(
                        tx,
                        caller,
                        now,
                        AuditEntry(
                            "auth.refresh_token_reuse",
                            "session",
                            current.id,
                            AuditOutcome.DENIED,
                            actor_user_id=current.user_id,
                        ),
                    )
            else:
                user = await _find_user_by_id(tx, current.user_id)
                active = current.revoked_at is None and now < current.expires_at and now < current.idle_expires_at
                if not active or user is None or user.status != UserStatus.ACTIVE:
                    failure = unauthorized("session_expired", "Your session has ended. Sign in again.")
                else:
                    new_token = generate_token(32)
                    idle = min(now + self._policy.session_idle_timeout, current.expires_at)
                    # Conditional update: only one of several concurrent requests holding the token can rotate it.
                    rotated = await tx.execute(
                        """
                        UPDATE identity.sessions
                        SET previous_refresh_token_hash = :old, refresh_token_hash = :new, rotated_at = :now,
                            last_used_at = :now, idle_expires_at = :idle
                        WHERE id = :id AND refresh_token_hash = :old AND revoked_at IS NULL
                        """,
                        {"id": current.id, "old": presented, "new": hash_token(new_token), "now": now, "idle": idle},
                    )
                    if rotated == 0:
                        failure = _invalid_refresh_token()
                    else:
                        token = self._tokens.issue(user.id, user.platform_role, current.id)
                        response = TokenResponse.model_construct(
                            access_token=token.value,
                            refresh_token=new_token,
                            token_type="Bearer",
                            expires_in=token.expires_in,
                        )
        if reused_session is not None:
            await self._access.invalidate_session(reused_session)
        if failure is not None:
            raise failure
        assert response is not None
        return response

    async def logout(self, caller: Caller) -> None:
        """End the current session so its refresh token and access tokens stop working."""
        user_id = caller.require_user_id()
        if caller.session_id is None:
            return
        session_id = caller.session_id
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            exists = await tx.fetchval(
                "SELECT EXISTS (SELECT 1 FROM identity.sessions WHERE id = :id AND user_id = :user)",
                {"id": session_id, "user": user_id},
            )
            if not exists:
                return
            await tx.execute(
                """
                UPDATE identity.sessions SET revoked_at = :now, revoked_reason = 'logout'
                WHERE id = :id AND revoked_at IS NULL
                """,
                {"id": session_id, "now": now},
            )
            await audit(tx, caller, now, AuditEntry("auth.logout", "session", session_id))
        await self._access.invalidate_session(session_id)

    async def current_user(self, caller: Caller) -> CurrentUserResponse:
        """Return the signed-in user's profile with organization memberships and patient profiles."""
        user_id = caller.require_user_id()
        async with self._db.transaction(caller.scope()) as tx:
            user = await _find_user_by_id(tx, user_id)
            if user is None:
                raise authentication_required()
            membership_rows = await tx.fetch(
                """
                SELECT m.id, m.organization_id, o.name, m.roles
                FROM organization.organization_memberships m
                JOIN organization.organizations o ON o.id = m.organization_id
                WHERE m.user_id = :user AND m.status = 'active' AND o.status <> 'closed'
                ORDER BY o.name
                """,
                {"user": user_id},
            )
            profile_rows = await tx.fetch(
                """
                SELECT l.patient_id, l.organization_id, o.name
                FROM patient.patient_account_links l
                JOIN organization.organizations o ON o.id = l.organization_id
                WHERE l.user_id = :user
                ORDER BY o.name
                """,
                {"user": user_id},
            )
        memberships = [
            MembershipSummary.model_construct(
                membership_id=row.id,
                organization_id=row.organization_id,
                organization_name=row.name,
                roles=list(row.roles),
                permissions=sorted_permissions(row.roles),
            )
            for row in membership_rows
        ]
        profiles = [
            PatientProfileSummary.model_construct(
                patient_id=row.patient_id, organization_id=row.organization_id, organization_name=row.name
            )
            for row in profile_rows
        ]
        return CurrentUserResponse.model_construct(
            user=_profile(user), memberships=memberships, patient_profiles=profiles
        )

    async def update_profile(self, caller: Caller, request: UpdateProfileRequest) -> UserProfile:
        """Update the signed-in user's name and phone number."""
        user_id = caller.require_user_id()
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            user = await tx.fetchrow(
                f"""
                UPDATE identity.users SET first_name = :first, last_name = :last, phone = :phone, updated_at = :now
                WHERE id = :id RETURNING {_USER_COLUMNS}
                """,
                {
                    "id": user_id,
                    "first": request.first_name.strip(),
                    "last": request.last_name.strip(),
                    "phone": trimmed(request.phone),
                    "now": now,
                },
            )
            if user is None:
                raise authentication_required()
            await audit(tx, caller, now, AuditEntry("user.profile_updated", "user", user_id))
        return _profile(user)

    async def forgot_password(self, caller: Caller, request: EmailRequest) -> None:
        """Email a reset link when the address belongs to an account; always succeeds silently."""
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            user = await _find_user_by_email(tx, normalize_email(request.email))
            if user is None or user.status in (UserStatus.SUSPENDED, UserStatus.DEACTIVATED):
                return
            token = await _create_token(
                tx, user.id, UserTokenPurpose.PASSWORD_RESET, self._policy.password_reset_life, now
            )
            await audit(
                tx, caller, now, AuditEntry("auth.password_reset_requested", "user", user.id, actor_user_id=user.id)
            )
        self._mailer.queue_password_reset(user.email, user.first_name, token)

    async def reset_password(self, caller: Caller, request: ResetPasswordRequest) -> None:
        """Set a new password with an emailed reset token and sign the user out everywhere."""
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            token = await _find_usable_token(tx, request.token, UserTokenPurpose.PASSWORD_RESET, now)
        if token is None:
            raise _invalid_link()
        password_hash = await self._hasher.hash(request.new_password)
        async with self._db.transaction(caller.scope()) as tx:
            if not await _consume_token(tx, token.id, now):
                raise _invalid_link()
            await tx.execute(
                """
                UPDATE identity.users SET password_hash = :hash, failed_login_count = 0, lockout_until = NULL,
                    updated_at = :now
                WHERE id = :id
                """,
                {"id": token.user_id, "hash": password_hash, "now": now},
            )
            # Receiving the reset email proves control of the address.
            await _mark_email_verified(tx, token.user_id, now)
            revoked = await _revoke_sessions(tx, token.user_id, "password_reset", None, now)
            await audit(
                tx, caller, now, AuditEntry("auth.password_reset", "user", token.user_id, actor_user_id=token.user_id)
            )
        await self._invalidate_sessions(revoked)

    async def change_password(self, caller: Caller, request: ChangePasswordRequest) -> None:
        """Change the signed-in user's password and end their other sessions."""
        user_id = caller.require_user_id()
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            user = await _find_user_by_id(tx, user_id)
        if user is None:
            raise authentication_required()
        if (
            user.password_hash is None
            or not (await self._hasher.verify(user.password_hash, request.current_password)).succeeded
        ):
            raise field_error("currentPassword", "The current password is incorrect.")
        password_hash = await self._hasher.hash(request.new_password)
        async with self._db.transaction(caller.scope()) as tx:
            await tx.execute(
                "UPDATE identity.users SET password_hash = :hash, updated_at = :now WHERE id = :id",
                {"id": user_id, "hash": password_hash, "now": now},
            )
            revoked = await _revoke_sessions(tx, user_id, "password_changed", caller.session_id, now)
            await audit(tx, caller, now, AuditEntry("auth.password_changed", "user", user_id))
        await self._invalidate_sessions(revoked)

    async def list_sessions(self, caller: Caller) -> list[SessionView]:
        """List the signed-in user's active sessions, most recently used first."""
        user_id = caller.require_user_id()
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            rows = await tx.fetch(
                """
                SELECT id, created_at, last_used_at, expires_at, ip_address, user_agent
                FROM identity.sessions
                WHERE user_id = :user AND revoked_at IS NULL AND expires_at > :now AND idle_expires_at > :now
                ORDER BY last_used_at DESC
                """,
                {"user": user_id, "now": now},
            )
        return [
            SessionView.model_construct(
                id=row.id,
                created_at=row.created_at,
                last_used_at=row.last_used_at,
                expires_at=row.expires_at,
                ip_address=row.ip_address,
                user_agent=row.user_agent,
                current=caller.session_id == row.id,
            )
            for row in rows
        ]

    async def revoke_session(self, caller: Caller, session_id: UUID) -> None:
        """Revoke one of the signed-in user's sessions, for example on a lost device."""
        user_id = caller.require_user_id()
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            exists = await tx.fetchval(
                "SELECT EXISTS (SELECT 1 FROM identity.sessions WHERE id = :id AND user_id = :user)",
                {"id": session_id, "user": user_id},
            )
            if not exists:
                raise not_found("session")
            await tx.execute(
                """
                UPDATE identity.sessions SET revoked_at = :now, revoked_reason = 'revoked_by_user'
                WHERE id = :id AND revoked_at IS NULL
                """,
                {"id": session_id, "now": now},
            )
            await audit(tx, caller, now, AuditEntry("auth.session_revoked", "session", session_id))
        await self._access.invalidate_session(session_id)

    async def suspend_user(self, caller: Caller, user_id: UUID) -> None:
        """Suspend an account and end all of its sessions (platform administrators only)."""
        caller.require_platform_admin()
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            user = await _find_user_by_id(tx, user_id)
            if user is None:
                raise not_found("user")
            if caller.user_id == user.id:
                raise rule("cannot_suspend_self", "You cannot suspend your own account.")
            await tx.execute(
                "UPDATE identity.users SET status = 'suspended', updated_at = :now WHERE id = :id",
                {"id": user_id, "now": now},
            )
            revoked = await _revoke_sessions(tx, user_id, "account_suspended", None, now)
            await audit(tx, caller, now, AuditEntry("user.suspended", "user", user_id))
        await self._invalidate_sessions(revoked)

    async def reinstate_user(self, caller: Caller, user_id: UUID) -> None:
        """Restore a suspended account (platform administrators only)."""
        caller.require_platform_admin()
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            user = await _find_user_by_id(tx, user_id)
            if user is None:
                raise not_found("user")
            if user.status != UserStatus.SUSPENDED:
                raise conflict("user_not_suspended", "The account is not suspended.")
            status = UserStatus.ACTIVE if user.email_verified_at is not None else UserStatus.PENDING_VERIFICATION
            await tx.execute(
                "UPDATE identity.users SET status = :status, updated_at = :now WHERE id = :id",
                {"id": user_id, "status": status.value, "now": now},
            )
            await audit(tx, caller, now, AuditEntry("user.reinstated", "user", user_id))

    async def enroll_totp(self, caller: Caller) -> TotpEnrollmentResponse:
        """Start TOTP enrollment and return the secret to load into an authenticator app."""
        user_id = caller.require_user_id()
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            user = await _find_user_by_id(tx, user_id)
            if user is None:
                raise authentication_required()
            if user.mfa_enabled:
                raise conflict("mfa_already_enabled", "Multi-factor authentication is already enabled.")
            await tx.execute("DELETE FROM identity.mfa_factors WHERE user_id = :user", {"user": user_id})
            secret = base32_encode(secrets.token_bytes(20))
            await tx.execute(
                """
                INSERT INTO identity.mfa_factors (id, user_id, type, secret_encrypted, status, created_at)
                VALUES (:id, :user, 'totp', :secret, 'pending', :now)
                """,
                {"id": new_id(), "user": user_id, "secret": self._encryptor.encrypt(secret), "now": now},
            )
        return TotpEnrollmentResponse.model_construct(
            secret=secret, otp_auth_uri=build_otpauth_uri(self._policy.mfa_issuer_name, user.email, secret)
        )

    async def confirm_totp(self, caller: Caller, request: ConfirmTotpRequest) -> RecoveryCodesResponse:
        """Activate TOTP once the user proves the authenticator works, and return recovery codes."""
        user_id = caller.require_user_id()
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            factor = await tx.fetchrow(
                "SELECT id, secret_encrypted FROM identity.mfa_factors WHERE user_id = :user AND status = 'pending'",
                {"user": user_id},
            )
            if factor is None:
                raise conflict("mfa_enrollment_not_started", "Start enrollment before confirming a code.")
            secret = base32_decode(self._encryptor.decrypt(factor.secret_encrypted))
            step = verify_code(secret, request.code, now)
            if step is None:
                raise field_error("code", "The verification code is incorrect.")
            await tx.execute(
                """
                UPDATE identity.mfa_factors SET status = 'active', confirmed_at = :now, last_used_time_step = :step
                WHERE id = :id
                """,
                {"id": factor.id, "now": now, "step": step},
            )
            await tx.execute(
                "UPDATE identity.users SET mfa_enabled = true, updated_at = :now WHERE id = :id",
                {"id": user_id, "now": now},
            )
            codes = [generate_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
            for code in codes:
                await tx.execute(
                    """
                    INSERT INTO identity.mfa_recovery_codes (id, user_id, code_hash, created_at)
                    VALUES (:id, :user, :hash, :now)
                    """,
                    {"id": new_id(), "user": user_id, "hash": hash_token(code), "now": now},
                )
            await audit(tx, caller, now, AuditEntry("auth.mfa_enabled", "user", user_id))
        return RecoveryCodesResponse.model_construct(recovery_codes=codes)

    async def disable_totp(self, caller: Caller, request: DisableTotpRequest) -> None:
        """Turn off TOTP after re-checking the password and a current code."""
        user_id = caller.require_user_id()
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            user = await _find_user_by_id(tx, user_id)
        if user is None:
            raise authentication_required()
        if not user.mfa_enabled:
            raise conflict("mfa_not_enabled", "Multi-factor authentication is not enabled.")
        if (
            user.password_hash is None
            or not (await self._hasher.verify(user.password_hash, request.password)).succeeded
        ):
            raise field_error("password", "The password is incorrect.")
        async with self._db.transaction(caller.scope()) as tx:
            if not await self._verify_totp(tx, user_id, request.code, now):
                raise field_error("code", "The verification code is incorrect.")
            await tx.execute("DELETE FROM identity.mfa_factors WHERE user_id = :user", {"user": user_id})
            await tx.execute("DELETE FROM identity.mfa_recovery_codes WHERE user_id = :user", {"user": user_id})
            await tx.execute(
                "UPDATE identity.users SET mfa_enabled = false, updated_at = :now WHERE id = :id",
                {"id": user_id, "now": now},
            )
            await audit(tx, caller, now, AuditEntry("auth.mfa_disabled", "user", user_id))

    async def _verify_totp(self, tx: Tx, user_id: UUID, code: str | None, now: datetime) -> bool:
        """Check a TOTP code and remember its time step so the code cannot be replayed."""
        factor = await tx.fetchrow(
            """
            SELECT id, secret_encrypted, last_used_time_step FROM identity.mfa_factors
            WHERE user_id = :user AND status = 'active'
            """,
            {"user": user_id},
        )
        if factor is None:
            return False
        secret = base32_decode(self._encryptor.decrypt(factor.secret_encrypted))
        step = verify_code(secret, code, now)
        if step is None or (factor.last_used_time_step is not None and step <= factor.last_used_time_step):
            return False
        await tx.execute(
            "UPDATE identity.mfa_factors SET last_used_time_step = :step WHERE id = :id",
            {"id": factor.id, "step": step},
        )
        return True

    async def _use_recovery_code(self, tx: Tx, caller: Caller, user_id: UUID, code: str, now: datetime) -> bool:
        """Consume a recovery code when it matches an unused one."""
        if not code.strip():
            return False
        used = await tx.execute(
            """
            UPDATE identity.mfa_recovery_codes SET used_at = :now
            WHERE user_id = :user AND code_hash = :hash AND used_at IS NULL
            """,
            {"user": user_id, "hash": hash_token(code.strip().upper()), "now": now},
        )
        if used == 0:
            return False
        await audit(tx, caller, now, AuditEntry("auth.mfa_recovery_code_used", "user", user_id, actor_user_id=user_id))
        return True
