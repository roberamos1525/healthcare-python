"""Request and response models of the account endpoints."""

from datetime import date
from typing import Final
from uuid import UUID

from healthcare.application.core.models import ApiModel, RequestModel, UtcDateTime
from healthcare.application.core.validation import Validator
from healthcare.domain.enums import UserStatus

MIN_PASSWORD_LENGTH: Final = 12
MAX_PASSWORD_LENGTH: Final = 128


def _password_rules(v: Validator, field: str, value: str) -> None:
    """Apply the password policy (length over composition, NIST SP 800-63B)."""
    v.text(field, value).not_empty().min_length(
        MIN_PASSWORD_LENGTH, "The password must be at least 12 characters long."
    ).max_length(MAX_PASSWORD_LENGTH)


def _name_rules(v: Validator, field: str, value: str) -> None:
    """Require a non-empty person name within the column limit."""
    v.text(field, value).not_empty().max_length(100)


class RegisterRequest(RequestModel):
    """Body of POST /auth/register."""

    email: str
    password: str
    first_name: str
    last_name: str
    phone: str | None = None

    def check(self, v: Validator, today: date) -> None:
        """Require a valid email, a long password that differs from the email, and names."""
        v.text("email", self.email).not_empty().max_length(254).email()
        _password_rules(v, "password", self.password)
        v.ensure(
            "password",
            self.password.casefold() != self.email.casefold(),
            "The password must not be the same as the email address.",
        )
        _name_rules(v, "firstName", self.first_name)
        _name_rules(v, "lastName", self.last_name)
        v.text("phone", self.phone).phone()


class VerifyEmailRequest(RequestModel):
    """Body of POST /auth/verify-email."""

    token: str

    def check(self, v: Validator, today: date) -> None:
        """Require the token."""
        v.text("token", self.token).not_empty().max_length(200)


class EmailRequest(RequestModel):
    """Body carrying only an email address (resend verification, forgotten password)."""

    email: str

    def check(self, v: Validator, today: date) -> None:
        """Require a valid email."""
        v.text("email", self.email).not_empty().max_length(254).email()


class LoginRequest(RequestModel):
    """Body of POST /auth/login."""

    email: str
    password: str

    def check(self, v: Validator, today: date) -> None:
        """Require both values."""
        v.text("email", self.email).not_empty().max_length(254)
        v.text("password", self.password).not_empty().max_length(MAX_PASSWORD_LENGTH)


class MfaChallengeRequest(RequestModel):
    """Body of POST /auth/mfa/challenge."""

    mfa_token: str
    code: str | None = None
    recovery_code: str | None = None

    def check(self, v: Validator, today: date) -> None:
        """Require the challenge token and exactly one of code or recovery code."""
        v.text("mfaToken", self.mfa_token).not_empty().max_length(200)
        has_code = self.code is not None and bool(self.code.strip())
        has_recovery = self.recovery_code is not None and bool(self.recovery_code.strip())
        v.ensure("code", has_code != has_recovery, "Provide either a verification code or a recovery code.")


class RefreshRequest(RequestModel):
    """Body of POST /auth/refresh."""

    refresh_token: str

    def check(self, v: Validator, today: date) -> None:
        """Require the token."""
        v.text("refreshToken", self.refresh_token).not_empty().max_length(200)


class ResetPasswordRequest(RequestModel):
    """Body of POST /auth/password/reset."""

    token: str
    new_password: str

    def check(self, v: Validator, today: date) -> None:
        """Require the token and a valid new password."""
        v.text("token", self.token).not_empty().max_length(200)
        _password_rules(v, "newPassword", self.new_password)


class ChangePasswordRequest(RequestModel):
    """Body of POST /auth/password/change."""

    current_password: str
    new_password: str

    def check(self, v: Validator, today: date) -> None:
        """Require the current password and a different, valid new password."""
        v.text("currentPassword", self.current_password).not_empty().max_length(MAX_PASSWORD_LENGTH)
        _password_rules(v, "newPassword", self.new_password)
        v.ensure(
            "newPassword",
            self.new_password != self.current_password,
            "The new password must be different from the current password.",
        )


class UpdateProfileRequest(RequestModel):
    """Body of PUT /auth/me."""

    first_name: str
    last_name: str
    phone: str | None = None

    def check(self, v: Validator, today: date) -> None:
        """Require names and a valid optional phone number."""
        _name_rules(v, "firstName", self.first_name)
        _name_rules(v, "lastName", self.last_name)
        v.text("phone", self.phone).phone()


class ConfirmTotpRequest(RequestModel):
    """Body of POST /auth/mfa/totp/confirm."""

    code: str

    def check(self, v: Validator, today: date) -> None:
        """Require the code."""
        v.text("code", self.code).not_empty().max_length(10)


class DisableTotpRequest(RequestModel):
    """Body of POST /auth/mfa/totp/disable."""

    password: str
    code: str

    def check(self, v: Validator, today: date) -> None:
        """Require the password and a current code."""
        v.text("password", self.password).not_empty().max_length(MAX_PASSWORD_LENGTH)
        v.text("code", self.code).not_empty().max_length(10)


class MessageResponse(ApiModel):
    """A short confirmation text."""

    message: str


class UserProfile(ApiModel):
    """Public profile of a user."""

    id: UUID
    email: str
    first_name: str
    last_name: str
    phone: str | None = None
    status: UserStatus
    email_verified: bool
    mfa_enabled: bool
    platform_role: str | None = None
    created_at: UtcDateTime


class LoginResponse(ApiModel):
    """Result of a sign-in step: tokens, or a challenge to complete with a second factor."""

    mfa_required: bool
    mfa_token: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    expires_in: int | None = None
    user: UserProfile | None = None


class TokenResponse(ApiModel):
    """A new token pair."""

    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int


class MembershipSummary(ApiModel):
    """One membership of the signed-in user with its roles and permissions."""

    membership_id: UUID
    organization_id: UUID
    organization_name: str
    roles: list[str]
    permissions: list[str]


class PatientProfileSummary(ApiModel):
    """A patient record linked to the signed-in user."""

    patient_id: UUID
    organization_id: UUID
    organization_name: str


class CurrentUserResponse(ApiModel):
    """Body of GET /auth/me."""

    user: UserProfile
    memberships: list[MembershipSummary]
    patient_profiles: list[PatientProfileSummary]


class SessionView(ApiModel):
    """One active session."""

    id: UUID
    created_at: UtcDateTime
    last_used_at: UtcDateTime
    expires_at: UtcDateTime
    ip_address: str | None = None
    user_agent: str | None = None
    current: bool


class TotpEnrollmentResponse(ApiModel):
    """The secret to load into an authenticator app."""

    secret: str
    otp_auth_uri: str


class RecoveryCodesResponse(ApiModel):
    """The one-time recovery codes, shown once."""

    recovery_codes: list[str]
