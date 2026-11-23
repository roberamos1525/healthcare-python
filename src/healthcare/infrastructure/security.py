"""Cryptography with formats shared by every backend: PBKDF2 hashes, RS256 tokens and AES-256-GCM fields."""

import base64
import hashlib
import hmac
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import anyio.to_thread
import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from healthcare.application.core.ports import AccessToken, Clock, PasswordVerification
from healthcare.application.security import base64url_decode, base64url_encode

_ALGORITHM: Final = "pbkdf2-sha256"
_SALT_BYTES: Final = 16
_HASH_BYTES: Final = 32


def derive(password: str, salt: bytes, iterations: int, length: int = _HASH_BYTES) -> bytes:
    """Run PBKDF2-HMAC-SHA256 over the UTF-8 password (OpenSSL releases the GIL while it works)."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, length)


def encode_hash(iterations: int, salt: bytes, digest: bytes) -> str:
    """Format a hash as ``pbkdf2-sha256$<iterations>$<salt>$<hash>`` with standard base64."""
    return f"{_ALGORITHM}${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_hash(encoded: str, password: str, iterations: int) -> PasswordVerification:
    """Check a password against an encoded hash in constant time and flag hashes with another cost."""
    parts = encoded.split("$")
    if len(parts) != 4 or parts[0] != _ALGORITHM or not parts[1].isdigit() or int(parts[1]) <= 0:
        return PasswordVerification(False)
    try:
        salt = base64.b64decode(parts[2], validate=True)
        expected = base64.b64decode(parts[3], validate=True)
    except ValueError:
        return PasswordVerification(False)
    if not expected:
        return PasswordVerification(False)
    stored_iterations = int(parts[1])
    matches = hmac.compare_digest(derive(password, salt, stored_iterations, len(expected)), expected)
    return PasswordVerification(matches, matches and stored_iterations != iterations)


class Pbkdf2PasswordHasher:
    """PBKDF2-HMAC-SHA256 password hashing on a worker thread, so the event loop is never blocked."""

    def __init__(self, iterations: int) -> None:
        """Create the hasher with the configured iteration count (600,000 by default)."""
        self.iterations = iterations

    def hash_sync(self, password: str) -> str:
        """Hash a password with a fresh random salt (blocking)."""
        salt = secrets.token_bytes(_SALT_BYTES)
        return encode_hash(self.iterations, salt, derive(password, salt, self.iterations))

    async def hash(self, password: str) -> str:
        """Hash a password on a worker thread."""
        return await anyio.to_thread.run_sync(self.hash_sync, password)

    async def verify(self, encoded: str, password: str) -> PasswordVerification:
        """Verify a password on a worker thread."""
        return await anyio.to_thread.run_sync(verify_hash, encoded, password, self.iterations)

    async def simulate(self, password: str) -> None:
        """Spend the time of a real verification so unknown accounts cannot be detected by timing."""
        await anyio.to_thread.run_sync(derive, password, secrets.token_bytes(_SALT_BYTES), self.iterations)


@dataclass(frozen=True, slots=True)
class SigningKey:
    """The RSA key that signs access tokens and its key identifier."""

    private_key: rsa.RSAPrivateKey
    key_id: str
    temporary: bool

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        """Return the public key as a JSON Web Key Set."""
        numbers = self.private_key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self.key_id,
                    "n": base64url_encode(_unsigned_bytes(numbers.n)),
                    "e": base64url_encode(_unsigned_bytes(numbers.e)),
                }
            ]
        }


def _unsigned_bytes(value: int) -> bytes:
    """Return the minimal big-endian bytes of a positive integer."""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def load_signing_key(pem_text: str, path: str, allow_temporary: bool) -> SigningKey:
    """Load the configured PEM key (inline or from a file) or, in development and testing, generate one."""
    if not pem_text.strip() and path.strip():
        pem_text = Path(path).read_text(encoding="utf-8")
    temporary = False
    if pem_text.strip():
        loaded = serialization.load_pem_private_key(pem_text.encode("utf-8"), password=None)
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise ValueError("The signing key is not an RSA key.")
        key = loaded
    elif allow_temporary:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        temporary = True
    else:
        raise ValueError("Auth:SigningKeyPem or Auth:SigningKeyPath must be configured.")
    spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(spki)
    return SigningKey(key, base64url_encode(digest.finalize())[:16], temporary)


@dataclass(frozen=True, slots=True)
class Principal:
    """The validated identity carried by an access token."""

    subject: str
    session_id: str
    platform_role: str | None


class JwtService:
    """Issues and validates RS256 access tokens (iss ``healthcare-platform``, aud ``healthcare-api``)."""

    def __init__(self, key: SigningKey, issuer: str, audience: str, lifetime_seconds: int, clock: Clock) -> None:
        """Create the service."""
        self._key = key
        self._public = key.private_key.public_key()
        self._issuer = issuer
        self._audience = audience
        self._lifetime = lifetime_seconds
        self._clock = clock

    def issue(self, user_id: UUID, platform_role: str | None, session_id: UUID) -> AccessToken:
        """Create a short-lived token bound to the user and session; organization roles are never embedded."""
        now = int(self._clock().timestamp())
        claims: dict[str, Any] = {
            "sub": str(user_id),
            "sid": str(session_id),
            "jti": uuid.uuid4().hex,
            "iss": self._issuer,
            "aud": self._audience,
            "iat": now,
            "nbf": now,
            "exp": now + self._lifetime,
        }
        if platform_role is not None:
            claims["platform_role"] = platform_role
        token = jwt.encode(claims, self._key.private_key, algorithm="RS256", headers={"kid": self._key.key_id})
        return AccessToken(token, self._lifetime)

    def validate(self, token: str) -> Principal | None:
        """Check signature (RS256 only), issuer, audience and lifetime with 30 s of clock skew."""
        try:
            claims = jwt.decode(
                token,
                self._public,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=30,
                options={"require": ["exp"]},
            )
        except jwt.PyJWTError:
            return None
        role = claims.get("platform_role")
        return Principal(str(claims.get("sub", "")), str(claims.get("sid", "")), str(role) if role else None)


class AesGcmFieldEncryptor:
    """Encrypts small secrets with AES-256-GCM as ``v1.<nonce>.<ciphertext>.<tag>`` (base64url)."""

    def __init__(self, key: bytes, temporary: bool = False) -> None:
        """Create the encryptor from a 256-bit key."""
        if len(key) != 32:
            raise ValueError("Auth:FieldEncryptionKey must be a base64-encoded 256-bit key.")
        self._aead = AESGCM(key)
        self.temporary = temporary

    @classmethod
    def from_setting(cls, base64_key: str, allow_temporary: bool) -> AesGcmFieldEncryptor:
        """Load the base64 key or, when allowed, generate a temporary one."""
        if base64_key.strip():
            try:
                key = base64.b64decode(base64_key.strip(), validate=True)
            except ValueError as error:
                raise ValueError("Auth:FieldEncryptionKey must be a base64-encoded 256-bit key.") from error
            return cls(key)
        if allow_temporary:
            return cls(secrets.token_bytes(32), temporary=True)
        raise ValueError("Auth:FieldEncryptionKey must be configured.")

    def encrypt(self, plaintext: str) -> str:
        """Encrypt with a random 96-bit nonce."""
        nonce = secrets.token_bytes(12)
        sealed = self._aead.encrypt(nonce, plaintext.encode("utf-8"), None)
        return ".".join(("v1", base64url_encode(nonce), base64url_encode(sealed[:-16]), base64url_encode(sealed[-16:])))

    def decrypt(self, value: str) -> str:
        """Verify the authentication tag and decrypt; tampered values raise ``ValueError``."""
        parts = value.split(".")
        if len(parts) != 4 or parts[0] != "v1":
            raise ValueError("The encrypted value has an unknown format.")
        try:
            nonce, ciphertext, tag = (base64url_decode(part) for part in parts[1:])
        except ValueError as error:
            raise ValueError("The encrypted value is malformed.") from error
        if len(nonce) != 12 or len(tag) != 16:
            raise ValueError("The encrypted value is malformed.")
        try:
            return self._aead.decrypt(nonce, ciphertext + tag, None).decode("utf-8")
        except Exception as error:
            raise ValueError("The encrypted value failed authentication.") from error


TokenValidator = Callable[[str], Principal | None]
