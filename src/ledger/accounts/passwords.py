"""Password hashing and verification.

Argon2id, which is the current recommendation and what `argon2-cffi` gives you
by default. Not bcrypt, which silently truncates at 72 bytes, and emphatically
not a general-purpose hash: the entire point is to be slow.
"""

from __future__ import annotations

import contextlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

#: Deliberately not tuned down for tests. A hashing cost that differs between
#: test and production is a cost that was never actually measured.
_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200

#: Verified against a real hash when no account exists, so a missing account
#: costs the same wall-clock time as a wrong password. Without this, response
#: timing turns the login form into an account-enumeration oracle.
_DUMMY_HASH = _hasher.hash("a-password-nobody-has-" + secrets.token_urlsafe(16))


class PasswordError(ValueError):
    """The password does not meet the policy."""


def validate(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        # Long inputs are a denial-of-service vector against a deliberately
        # slow hash, not a security benefit.
        raise PasswordError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
    if password.strip() != password:
        raise PasswordError("Password must not begin or end with whitespace.")


def hash_password(password: str) -> str:
    validate(password)
    return _hasher.hash(password)


def verify(password: str, password_hash: str | None) -> bool:
    """Check a password, taking the same time whether or not the account exists."""
    if password_hash is None:
        # Still do the work: an account with no password (OAuth-only) must not
        # answer faster than one with a wrong password.
        _burn()
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _burn() -> None:
    with contextlib.suppress(VerifyMismatchError, VerificationError, InvalidHashError):
        _hasher.verify(_DUMMY_HASH, "not-the-password")


def needs_rehash(password_hash: str) -> bool:
    """Whether a stored hash predates the current cost parameters."""
    try:
        return bool(_hasher.check_needs_rehash(password_hash))
    except InvalidHashError:
        return True


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)
