"""Security formats: TOTP, tokens, recovery codes, PBKDF2 hashes, AES-GCM field encryption and RS256 tokens."""

import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from healthcare.application.security import (
    RECOVERY_ALPHABET,
    base32_decode,
    base32_encode,
    base64url_decode,
    base64url_encode,
    build_otpauth_uri,
    compute_code,
    escape_data_string,
    generate_recovery_code,
    generate_token,
    hash_token,
    random_int,
    time_step,
    verify_code,
)
from healthcare.infrastructure.config import AuthSettings
from healthcare.infrastructure.security import (
    AesGcmFieldEncryptor,
    JwtService,
    Pbkdf2PasswordHasher,
    SigningKey,
    derive,
    encode_hash,
    load_signing_key,
    verify_hash,
)

RFC_SECRET = b"12345678901234567890"
ISSUER = "healthcare-platform"
AUDIENCE = "healthcare-api"


def at(seconds: int) -> datetime:
    """Return the UTC instant a number of seconds after the Unix epoch."""
    return datetime.fromtimestamp(seconds, UTC)


# ---------------------------------------------------------------------------------------------------------
# TOTP (RFC 6238)
# ---------------------------------------------------------------------------------------------------------


def rfc6238_eight_digits(secret: bytes, step: int) -> str:
    """Compute the eight-digit RFC 6238 SHA-1 value independently of the application."""
    digest = hmac.new(secret, step.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 100_000_000:08d}"


@pytest.mark.parametrize(
    ("seconds", "expected", "rfc_value"),
    [
        (59, "287082", "94287082"),
        (1111111109, "081804", "07081804"),
        (1111111111, "050471", "14050471"),
        (1234567890, "005924", "89005924"),
        (2000000000, "279037", "69279037"),
    ],
)
def test_computes_the_rfc6238_reference_codes(seconds: int, expected: str, rfc_value: str) -> None:
    """Six-digit codes are the published eight-digit SHA-1 vectors truncated to their last six digits."""
    step = time_step(at(seconds))
    assert rfc6238_eight_digits(RFC_SECRET, step) == rfc_value
    assert compute_code(RFC_SECRET, step) == expected == rfc_value[-6:]


def test_time_steps_are_thirty_seconds_long() -> None:
    """Instants in the same 30-second window share a step."""
    assert time_step(at(0)) == 0
    assert time_step(at(29)) == 0
    assert time_step(at(30)) == 1
    assert time_step(at(59)) == 1


def test_accepts_codes_from_the_adjacent_time_steps_only() -> None:
    """The current step and one step either side are accepted; the matched step is returned."""
    now = at(1234567890)
    current = time_step(now)
    assert verify_code(RFC_SECRET, compute_code(RFC_SECRET, current), now) == current
    assert verify_code(RFC_SECRET, compute_code(RFC_SECRET, current - 1), now) == current - 1
    assert verify_code(RFC_SECRET, compute_code(RFC_SECRET, current + 1), now) == current + 1
    assert verify_code(RFC_SECRET, compute_code(RFC_SECRET, current + 3), now) is None
    assert verify_code(RFC_SECRET, compute_code(RFC_SECRET, current - 2), now) is None


def test_a_code_with_spaces_is_normalized() -> None:
    """Spaces typed between digit groups are ignored."""
    assert verify_code(RFC_SECRET, "287 082", at(59)) == 1


@pytest.mark.parametrize("code", [None, "", "12345", "abcdef", "1234567", "28708a", "２８７０８２"])
def test_rejects_malformed_codes(code: str | None) -> None:
    """Missing, short, long or non-digit codes never verify."""
    assert verify_code(RFC_SECRET, code, at(59)) is None


def test_a_code_for_another_secret_is_rejected() -> None:
    """A code is bound to its secret."""
    other = b"98765432109876543210"
    assert verify_code(other, "287082", at(59)) is None


def test_encodes_and_decodes_base32() -> None:
    """Base32 is unpadded on output and tolerant of case, spaces and padding on input."""
    assert base32_encode(b"foobar") == "MZXW6YTBOI"
    assert base32_decode("mzxw 6ytb oi==") == b"foobar"
    random = secrets.token_bytes(20)
    assert base32_decode(base32_encode(random)) == random
    with pytest.raises(ValueError, match="base32"):
        base32_decode("not-base32!")


def test_builds_an_otpauth_uri_for_authenticator_apps() -> None:
    """The provisioning URI escapes the issuer and account and lists the parameters."""
    uri = build_otpauth_uri("HealthCare Platform", "jane@example.test", "JBSWY3DPEHPK3PXP")
    assert uri.startswith("otpauth://totp/HealthCare%20Platform:jane%40example.test?secret=JBSWY3DPEHPK3PXP")
    assert "&issuer=HealthCare%20Platform" in uri
    assert "algorithm=SHA1" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


def test_escape_data_string_matches_the_reference_encoding() -> None:
    """Everything but RFC 3986 unreserved characters is percent-encoded with upper-case hex."""
    assert escape_data_string("a b+c@d/é~-._") == "a%20b%2Bc%40d%2F%C3%A9~-._"


# ---------------------------------------------------------------------------------------------------------
# Tokens and recovery codes
# ---------------------------------------------------------------------------------------------------------


def test_generates_unique_url_safe_tokens_and_stable_hashes() -> None:
    """Tokens are 43 URL-safe characters for 32 bytes; hashes are stable lowercase hex SHA-256."""
    first, second = generate_token(32), generate_token(32)
    assert first != second
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first)
    assert hash_token(first) == hash_token(first)
    assert re.fullmatch(r"[0-9a-f]{64}", hash_token(first))
    assert hash_token(first) != hash_token(second)
    assert hash_token("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_round_trips_base64url() -> None:
    """Unpadded base64url round-trips and padded input is accepted."""
    data = secrets.token_bytes(33)
    assert base64url_decode(base64url_encode(data)) == data
    assert base64url_encode(b"\xfb\xff") == "-_8"
    assert base64url_decode("-_8") == b"\xfb\xff"
    assert base64url_decode("-_8=") == b"\xfb\xff"


@pytest.mark.parametrize("text", ["***", "a", "ab$c"])
def test_rejects_invalid_base64url_input(text: str) -> None:
    """Text with characters outside the alphabet or an impossible length is rejected, as in the reference."""
    with pytest.raises(ValueError):
        base64url_decode(text)


def test_recovery_codes_use_the_expected_format() -> None:
    """Recovery codes are XXXXX-XXXXX without look-alike characters."""
    pattern = re.compile(r"[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}")
    assert set(RECOVERY_ALPHABET).isdisjoint("01IO")
    for _ in range(50):
        assert pattern.fullmatch(generate_recovery_code())


def test_random_integers_stay_in_range() -> None:
    """random_int returns values in [low, high)."""
    values = {random_int(10, 20) for _ in range(1000)}
    assert min(values) >= 10
    assert max(values) < 20


# ---------------------------------------------------------------------------------------------------------
# PBKDF2 password hashing
# ---------------------------------------------------------------------------------------------------------


def test_pbkdf2_matches_the_rfc_7914_test_vector() -> None:
    """The derivation is standard PBKDF2-HMAC-SHA256."""
    expected = (
        "55ac046e56e3089fec1691c22544b605f94185216dde0465e68b9d57c20dacbc"
        "49ca9cccf179b645991664b39d77ef317c71b845b1e30bd509112041d3a19783"
    )
    assert derive("passwd", b"salt", 1, 64).hex() == expected


def test_the_default_hash_uses_the_reference_format_and_cost() -> None:
    """Hashes are pbkdf2-sha256$600000$<16-byte salt>$<32-byte hash> in standard base64."""
    iterations = AuthSettings().password_hash_iterations
    assert iterations == 600_000
    hasher = Pbkdf2PasswordHasher(iterations)
    encoded = hasher.hash_sync("correct horse battery staple")
    algorithm, cost, salt, digest = encoded.split("$")
    assert (algorithm, cost) == ("pbkdf2-sha256", "600000")
    assert len(base64.b64decode(salt, validate=True)) == 16
    assert len(base64.b64decode(digest, validate=True)) == 32
    result = verify_hash(encoded, "correct horse battery staple", iterations)
    assert result.succeeded
    assert not result.needs_rehash


def test_verifies_the_correct_password_and_rejects_others() -> None:
    """Only the original password verifies and every hash uses a fresh salt."""
    hasher = Pbkdf2PasswordHasher(1000)
    encoded = hasher.hash_sync("correct horse battery staple")
    assert encoded.startswith("pbkdf2-sha256$1000$")
    assert verify_hash(encoded, "correct horse battery staple", 1000).succeeded
    assert not verify_hash(encoded, "correct horse battery stapler", 1000).succeeded
    assert not verify_hash(encoded, "Correct horse battery staple", 1000).succeeded
    assert encoded != hasher.hash_sync("correct horse battery staple")


async def test_hashes_and_verifies_on_a_worker_thread() -> None:
    """The asynchronous API produces and checks the same format."""
    hasher = Pbkdf2PasswordHasher(1000)
    encoded = await hasher.hash("a long enough password")
    assert (await hasher.verify(encoded, "a long enough password")).succeeded
    assert not (await hasher.verify(encoded, "another password")).succeeded
    await hasher.simulate("anything at all")


def test_flags_hashes_created_with_a_different_cost_for_rehashing() -> None:
    """A correct password under another cost succeeds and asks for an upgrade; a wrong one does not."""
    old = Pbkdf2PasswordHasher(1000).hash_sync("a long enough password")
    result = verify_hash(old, "a long enough password", 2000)
    assert result.succeeded
    assert result.needs_rehash
    current = Pbkdf2PasswordHasher(2000).hash_sync("a long enough password")
    assert not verify_hash(current, "a long enough password", 2000).needs_rehash
    wrong = verify_hash(old, "wrong password", 2000)
    assert not wrong.succeeded
    assert not wrong.needs_rehash


def test_verifies_hashes_produced_by_the_reference_format() -> None:
    """A hash assembled from a known salt in the shared format verifies."""
    salt = b"0123456789abcdef"
    digest = hashlib.pbkdf2_hmac("sha256", b"Synthetic-Password-1", salt, 1000, 32)
    encoded = encode_hash(1000, salt, digest)
    assert encoded == (
        "pbkdf2-sha256$1000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()
    )
    assert verify_hash(encoded, "Synthetic-Password-1", 1000).succeeded


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "plaintext",
        "pbkdf2-sha256$abc$AAAA$AAAA",
        "pbkdf2-sha256$1000$not-base64$AAAA",
        "bcrypt$1000$AAAA$AAAA",
        "pbkdf2-sha256$0$AAAA$AAAA",
        "pbkdf2-sha256$1000$AAAA$",
        "pbkdf2-sha256$1000$AAAA$AAAA$AAAA",
        "pbkdf2-sha256$-5$AAAA$AAAA",
    ],
)
def test_treats_malformed_hashes_as_a_failed_verification(encoded: str) -> None:
    """Malformed hashes never verify and never raise."""
    result = verify_hash(encoded, "anything", 1000)
    assert not result.succeeded
    assert not result.needs_rehash


# ---------------------------------------------------------------------------------------------------------
# AES-256-GCM field encryption
# ---------------------------------------------------------------------------------------------------------


def new_key() -> str:
    """Return a random base64 field encryption key."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def test_encrypts_and_decrypts_field_values_with_a_random_nonce() -> None:
    """Each encryption uses a fresh nonce and the plaintext never appears in the ciphertext."""
    encryptor = AesGcmFieldEncryptor.from_setting(new_key(), False)
    assert not encryptor.temporary
    first, second = encryptor.encrypt("JBSWY3DPEHPK3PXP"), encryptor.encrypt("JBSWY3DPEHPK3PXP")
    assert first != second
    assert "JBSWY3DPEHPK3PXP" not in first
    assert first.startswith("v1.")
    version, nonce, ciphertext, tag = first.split(".")
    assert version == "v1"
    assert len(base64url_decode(nonce)) == 12
    assert len(base64url_decode(ciphertext)) == len("JBSWY3DPEHPK3PXP")
    assert len(base64url_decode(tag)) == 16
    assert encryptor.decrypt(first) == "JBSWY3DPEHPK3PXP"
    assert encryptor.decrypt(second) == "JBSWY3DPEHPK3PXP"
    assert encryptor.decrypt(encryptor.encrypt("")) == ""
    assert encryptor.decrypt(encryptor.encrypt("café 😀")) == "café 😀"


def flip_byte(part: str) -> str:
    """Flip the bits of the first byte of a base64url segment."""
    data = bytearray(base64url_decode(part))
    data[0] ^= 0xFF
    return base64url_encode(bytes(data))


def test_rejects_tampered_or_foreign_ciphertext() -> None:
    """Changing any segment, or decrypting with another key, fails authentication."""
    first = AesGcmFieldEncryptor.from_setting(new_key(), False)
    second = AesGcmFieldEncryptor.from_setting(new_key(), False)
    ciphertext = first.encrypt("secret")
    parts = ciphertext.split(".")
    replacement = "B" if parts[2][-1] == "A" else "A"
    tampered = [
        ".".join([parts[0], parts[1], parts[2][:-1] + replacement, parts[3]]),
        ".".join([parts[0], flip_byte(parts[1]), parts[2], parts[3]]),
        ".".join([parts[0], parts[1], flip_byte(parts[2]), parts[3]]),
        ".".join([parts[0], parts[1], parts[2], flip_byte(parts[3])]),
        ".".join([parts[0], parts[1], parts[2], parts[3][:-2]]),
        ".".join(["v2", *parts[1:]]),
        "not-a-ciphertext",
        "",
    ]
    for value in tampered:
        with pytest.raises(ValueError):
            first.decrypt(value)
    with pytest.raises(ValueError, match="authentication"):
        second.decrypt(ciphertext)
    assert first.decrypt(ciphertext) == "secret"


def test_requires_configured_keys_outside_development() -> None:
    """Keys are mandatory unless temporary keys are allowed, and must be 256 bits."""
    with pytest.raises(ValueError, match="must be configured"):
        AesGcmFieldEncryptor.from_setting("", False)
    with pytest.raises(ValueError, match="256-bit"):
        AesGcmFieldEncryptor.from_setting(base64.b64encode(bytes(16)).decode(), False)
    with pytest.raises(ValueError, match="256-bit"):
        AesGcmFieldEncryptor.from_setting("not base64!", False)
    with pytest.raises(ValueError, match="must be configured"):
        load_signing_key("", "", False)
    assert AesGcmFieldEncryptor.from_setting("", True).temporary
    assert load_signing_key("", "", True).temporary


# ---------------------------------------------------------------------------------------------------------
# RS256 access tokens and the key set
# ---------------------------------------------------------------------------------------------------------


def pem_of(key: rsa.RSAPrivateKey) -> str:
    """Return the PKCS#8 PEM text of a private key."""
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    """Generate one RSA key for the token tests."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def signing_key(private_key: rsa.RSAPrivateKey) -> SigningKey:
    """Load the generated key from PEM text like a configured key."""
    key = load_signing_key(pem_of(private_key), "", False)
    assert not key.temporary
    return key


@pytest.fixture(scope="module")
def other_signing_key() -> SigningKey:
    """Provide a second, unrelated signing key."""
    return load_signing_key("", "", True)


def service(key: SigningKey, instant: datetime, audience: str = AUDIENCE, issuer: str = ISSUER) -> JwtService:
    """Create a token service whose clock is fixed at an instant."""
    return JwtService(key, issuer, audience, 900, lambda: instant)


def segment(token: str, index: int) -> dict[str, Any]:
    """Decode one JSON segment of a compact token."""
    data: dict[str, Any] = json.loads(base64url_decode(token.split(".")[index]))
    return data


def test_issues_access_tokens_with_the_reference_header_and_claims(signing_key: SigningKey) -> None:
    """RS256 with the key id; subject, session, role, issuer, audience and a 32-character token id."""
    now = datetime.now(UTC)
    user, session = uuid.uuid4(), uuid.uuid4()
    token = service(signing_key, now).issue(user, "platform_admin", session)
    assert token.expires_in == 900
    header, claims = segment(token.value, 0), segment(token.value, 1)
    assert header["alg"] == "RS256"
    assert header["kid"] == signing_key.key_id
    assert len(signing_key.key_id) == 16
    assert claims["aud"] == AUDIENCE
    assert claims["iss"] == ISSUER
    assert claims["sub"] == str(user)
    assert claims["sid"] == str(session)
    assert claims["platform_role"] == "platform_admin"
    assert re.fullmatch(r"[0-9a-f]{32}", claims["jti"])
    assert claims["exp"] - claims["iat"] == 900
    assert claims["nbf"] == claims["iat"] == int(now.timestamp())


def test_validates_its_own_tokens(signing_key: SigningKey) -> None:
    """A fresh token yields the principal; a token without a platform role has none."""
    now = datetime.now(UTC)
    user, session = uuid.uuid4(), uuid.uuid4()
    jwt_service = service(signing_key, now)
    principal = jwt_service.validate(jwt_service.issue(user, "platform_admin", session).value)
    assert principal is not None
    assert (principal.subject, principal.session_id, principal.platform_role) == (
        str(user),
        str(session),
        "platform_admin",
    )
    plain = jwt_service.validate(jwt_service.issue(user, None, session).value)
    assert plain is not None
    assert plain.platform_role is None
    assert "platform_role" not in segment(jwt_service.issue(user, None, session).value, 1)


def test_tolerates_thirty_seconds_of_clock_skew_and_rejects_expired_tokens(signing_key: SigningKey) -> None:
    """A token that expired 20 seconds ago is accepted, one that expired 40 seconds ago is not."""
    now = datetime.now(UTC)
    validator = service(signing_key, now)
    recent = service(signing_key, now - timedelta(minutes=15, seconds=20)).issue(uuid.uuid4(), None, uuid.uuid4())
    assert validator.validate(recent.value) is not None
    expired = service(signing_key, now - timedelta(minutes=15, seconds=40)).issue(uuid.uuid4(), None, uuid.uuid4())
    assert validator.validate(expired.value) is None


def test_rejects_tokens_for_another_audience_or_issuer(signing_key: SigningKey) -> None:
    """Audience and issuer must match the configuration."""
    now = datetime.now(UTC)
    token = service(signing_key, now).issue(uuid.uuid4(), None, uuid.uuid4()).value
    assert service(signing_key, now, audience="another-api").validate(token) is None
    assert service(signing_key, now, issuer="another-issuer").validate(token) is None


def test_rejects_tokens_signed_with_another_key(signing_key: SigningKey, other_signing_key: SigningKey) -> None:
    """The signature must come from the configured key."""
    now = datetime.now(UTC)
    token = service(signing_key, now).issue(uuid.uuid4(), None, uuid.uuid4()).value
    assert service(other_signing_key, now).validate(token) is None


def unsigned(header: dict[str, Any], claims: dict[str, Any], signature: bytes = b"") -> str:
    """Assemble a compact token from a header, claims and raw signature bytes."""

    def encode(value: dict[str, Any]) -> str:
        """Encode one JSON segment."""
        return base64url_encode(json.dumps(value).encode())

    return f"{encode(header)}.{encode(claims)}.{base64url_encode(signature)}"


def valid_claims() -> dict[str, Any]:
    """Return claims that would be accepted if signed correctly."""
    now = int(datetime.now(UTC).timestamp())
    return {
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 600,
    }


def test_rejects_the_none_algorithm(signing_key: SigningKey) -> None:
    """An unsigned token with alg none is never accepted."""
    validator = service(signing_key, datetime.now(UTC))
    assert validator.validate(unsigned({"alg": "none", "typ": "JWT"}, valid_claims())) is None
    assert validator.validate(unsigned({"alg": "None", "typ": "JWT"}, valid_claims())) is None


def test_rejects_hmac_tokens_keyed_with_the_public_key(signing_key: SigningKey) -> None:
    """Algorithm confusion (HS256 with the public key as the secret) is rejected."""
    public_pem = signing_key.private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header, claims = {"alg": "HS256", "typ": "JWT"}, valid_claims()
    signing_input = unsigned(header, claims).rsplit(".", 1)[0]
    signature = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    assert service(signing_key, datetime.now(UTC)).validate(f"{signing_input}.{base64url_encode(signature)}") is None


def test_rejects_tampered_and_malformed_tokens(signing_key: SigningKey) -> None:
    """Changing the payload breaks the signature; garbage is rejected without raising."""
    jwt_service = service(signing_key, datetime.now(UTC))
    token = jwt_service.issue(uuid.uuid4(), None, uuid.uuid4()).value
    header, _, signature = token.split(".")
    claims = segment(token, 1)
    claims["platform_role"] = "platform_admin"
    forged = f"{header}.{base64url_encode(json.dumps(claims).encode())}.{signature}"
    assert jwt_service.validate(forged) is None
    for garbage in ("", "abc", "a.b.c", token + "x"):
        assert jwt_service.validate(garbage) is None


def test_requires_an_expiry(signing_key: SigningKey) -> None:
    """A correctly signed token without exp is rejected."""
    claims = valid_claims()
    del claims["exp"]
    token = jwt.encode(claims, signing_key.private_key, algorithm="RS256", headers={"kid": signing_key.key_id})
    assert service(signing_key, datetime.now(UTC)).validate(token) is None


def test_publishes_the_public_key_as_a_json_web_key_set(signing_key: SigningKey) -> None:
    """The key set has one RS256 signing key with the reference member order."""
    key_set = signing_key.jwks()
    assert len(key_set["keys"]) == 1
    jwk = key_set["keys"][0]
    assert (jwk["kty"], jwk["use"], jwk["alg"], jwk["kid"], jwk["e"]) == (
        "RSA",
        "sig",
        "RS256",
        signing_key.key_id,
        "AQAB",
    )
    numbers = signing_key.private_key.public_key().public_numbers()
    assert int.from_bytes(base64url_decode(jwk["n"]), "big") == numbers.n
    assert json.dumps(key_set, separators=(",", ":")).startswith(
        '{"keys":[{"kty":"RSA","use":"sig","alg":"RS256","kid":'
    )


def test_the_key_id_is_derived_from_the_public_key(signing_key: SigningKey, private_key: rsa.RSAPrivateKey) -> None:
    """The key id is the first 16 characters of the base64url SHA-256 of the SubjectPublicKeyInfo."""
    spki = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert signing_key.key_id == base64url_encode(hashlib.sha256(spki).digest())[:16]
    assert load_signing_key(pem_of(private_key), "", False).key_id == signing_key.key_id


def test_tokens_validate_with_a_standard_library_and_the_published_key(signing_key: SigningKey) -> None:
    """Other backends can verify the token with the key set alone."""
    token = service(signing_key, datetime.now(UTC)).issue(uuid.uuid4(), None, uuid.uuid4()).value
    public = jwt.PyJWK(signing_key.jwks()["keys"][0]).key
    claims = jwt.decode(token, public, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER)
    assert claims["iss"] == ISSUER
