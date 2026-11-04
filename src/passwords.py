"""
Password hashing, username canonicalisation and password policy (ADR 0011).

Why stdlib scrypt: `hashlib.scrypt` is memory-hard, ships with Python and needs no new dependency. Stored form is
`scrypt$n=<N>,r=<R>,p=<P>$<salt hex>$<key hex>`, so the parameters travel with the hash and can be raised later
(`needs_rehash`). Settings are read from `config` at CALL time. A bounded semaphore (`kdf_slot`) caps how many
scrypt runs happen at once, so a flood of logins cannot exhaust memory: callers fail fast with `KdfBusy`.
"""

import hashlib
import hmac
import re
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from src import config

_SALT_BYTES = 16
_KEY_BYTES = 32
_USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")
_kdf_slots = threading.BoundedSemaphore(config.KDF_MAX_CONCURRENT)


class PasswordPolicyError(ValueError):
    """The chosen password breaks the policy; the message is safe to show to the user."""


class KdfBusy(Exception):
    """Every key-derivation slot is in use; the caller should answer 'try again later' without hashing."""


@contextmanager
def kdf_slot(timeout: float) -> Iterator[None]:
    """Hold one of KDF_MAX_CONCURRENT slots for the duration of a scrypt run; raise KdfBusy after `timeout` s."""
    if not _kdf_slots.acquire(timeout=timeout):
        raise KdfBusy("The server is busy. Please try again shortly.")
    try:
        yield
    finally:
        _kdf_slots.release()


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=config.SCRYPT_MAXMEM, dklen=_KEY_BYTES
    )


def _encode(salt: bytes, key: bytes, n: int, r: int, p: int) -> str:
    return f"scrypt$n={n},r={r},p={p}${salt.hex()}${key.hex()}"


def hash_password(password: str) -> str:
    """Salted scrypt hash with the currently configured parameters."""
    n, r, p = config.SCRYPT_N, config.SCRYPT_R, config.SCRYPT_P
    salt = secrets.token_bytes(_SALT_BYTES)
    return _encode(salt, _derive(password, salt, n, r, p), n, r, p)


def _parse(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    """Split a stored hash; None for anything malformed (never raises)."""
    try:
        kind, params, salt_hex, key_hex = encoded.split("$")
        values = dict(item.split("=") for item in params.split(","))
        if kind != "scrypt" or set(values) != {"n", "r", "p"}:
            return None
        n, r, p = int(values["n"]), int(values["r"]), int(values["p"])
        return n, r, p, bytes.fromhex(salt_hex), bytes.fromhex(key_hex)
    except (ValueError, AttributeError):
        return None


def verify_password(password: str, encoded: str) -> bool:
    """True if `password` matches `encoded`. One scrypt run, constant-time compare; bad stored data is just False."""
    parsed = _parse(encoded) if isinstance(encoded, str) else None
    if parsed is None:
        return False
    n, r, p, salt, key = parsed
    try:
        candidate = _derive(password, salt, n, r, p)
    except (ValueError, MemoryError):  # parameters scrypt refuses (not a power of two, over maxmem, ...)
        return False
    return hmac.compare_digest(candidate, key)


def needs_rehash(encoded: str) -> bool:
    """True when the stored hash uses parameters other than the current configuration."""
    parsed = _parse(encoded)
    return parsed is None or parsed[:3] != (config.SCRYPT_N, config.SCRYPT_R, config.SCRYPT_P)


# Verified for unknown users so they cost the same KDF time as real ones (no user enumeration by timing).
DUMMY_HASH = hash_password("trustrag-dummy-password-never-valid")


def canonical_username(raw: object) -> str | None:
    """The one canonical form of a username, or None. `isascii` runs BEFORE `lower` (e.g. Kelvin sign K -> k)."""
    if not isinstance(raw, str) or not raw.isascii():
        return None
    name = raw.lower()
    return name if _USERNAME.fullmatch(name) else None


def check_password_policy(password: str, username: str) -> None:
    """Raise PasswordPolicyError unless the password is PASSWORD_MIN_CHARS..PASSWORD_MAX_CHARS and not the username."""
    low, high = config.PASSWORD_MIN_CHARS, config.PASSWORD_MAX_CHARS
    if not low <= len(password) <= high:
        raise PasswordPolicyError(f"The password must be between {low} and {high} characters long.")
    if password.lower() == username.lower():
        raise PasswordPolicyError("The password must not be the same as the username.")
