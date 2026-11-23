"""Token formats shared with the other backends: random tokens, SHA-256 token hashes, recovery codes and TOTP."""

import base64
import hashlib
import hmac
import re
import secrets
import struct
from datetime import datetime
from typing import Final

RECOVERY_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TOTP_STEP: Final = 30
TOTP_DIGITS: Final = 6
_BASE32: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_UNRESERVED: Final = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def base64url_encode(data: bytes) -> str:
    """Encode bytes as unpadded base64url."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


_BASE64URL: Final = re.compile(r"[A-Za-z0-9_-]*")


def base64url_decode(text: str) -> bytes:
    """Decode base64url with or without padding; any character outside the alphabet is rejected."""
    stripped = text.rstrip("=")
    if _BASE64URL.fullmatch(stripped) is None or len(stripped) % 4 == 1:
        raise ValueError("The value is not valid base64url.")
    return base64.urlsafe_b64decode(stripped + "=" * (-len(stripped) % 4))


def generate_token(byte_length: int = 32) -> str:
    """Return a URL-safe random token made of the given number of random bytes (32 bytes = 256 bits)."""
    return base64url_encode(secrets.token_bytes(byte_length))


def hash_token(token: str) -> str:
    """Return the lowercase hex SHA-256 of a token; a fast hash suits long random values."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_recovery_code() -> str:
    """Return a recovery code of the form ``XXXXX-XXXXX`` from an alphabet without look-alike characters."""
    first = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(5))
    second = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(5))
    return f"{first}-{second}"


def random_int(low: int, high: int) -> int:
    """Return a uniformly distributed random integer in ``[low, high)``."""
    return low + secrets.randbelow(high - low)


def time_step(instant: datetime) -> int:
    """Return the TOTP time step that contains the instant."""
    return int(instant.timestamp()) // TOTP_STEP


def compute_code(secret: bytes, step: int) -> str:
    """Compute the six-digit RFC 6238 code (HMAC-SHA1) for a secret and time step."""
    digest = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def verify_code(secret: bytes, code: str | None, now: datetime) -> int | None:
    """Check a code against the current step and one step either side; return the matched step or None."""
    if code is None:
        return None
    normalized = code.replace(" ", "")
    if len(normalized) != TOTP_DIGITS or not all("0" <= ch <= "9" for ch in normalized):
        return None
    current = time_step(now)
    for step in (current - 1, current, current + 1):
        if hmac.compare_digest(compute_code(secret, step), normalized):
            return step
    return None


def escape_data_string(text: str) -> str:
    """Percent-encode everything except RFC 3986 unreserved characters, with upper-case hex digits."""
    return "".join(chr(b) if b in _UNRESERVED else f"%{b:02X}" for b in text.encode("utf-8"))


def build_otpauth_uri(issuer: str, account: str, secret: str) -> str:
    """Build the otpauth URI that authenticator apps read from a QR code."""
    escaped_issuer = escape_data_string(issuer)
    return (
        f"otpauth://totp/{escaped_issuer}:{escape_data_string(account)}?secret={secret}&issuer={escaped_issuer}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP}"
    )


def base32_encode(data: bytes) -> str:
    """Encode bytes as unpadded RFC 4648 base32, the format authenticator apps use."""
    return base64.b32encode(data).decode("ascii").rstrip("=")


def base32_decode(text: str) -> bytes:
    """Decode base32 text, ignoring case, spaces and padding."""
    buffer = 0
    bits = 0
    out = bytearray()
    for ch in text.upper():
        if ch in "= ":
            continue
        index = _BASE32.find(ch)
        if index < 0:
            raise ValueError("The value is not valid base32.")
        buffer = ((buffer << 5) | index) & 0xFFFFFFFF
        bits += 5
        if bits >= 8:
            out.append((buffer >> (bits - 8)) & 0xFF)
            bits -= 8
    return bytes(out)
