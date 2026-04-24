"""Signed session tokens for portal authentication.

Uses itsdangerous to sign the authenticated `account_id` in a cookie so
clients cannot spoof another account by setting the cookie manually. The
cookie stores the **Account**, not the **User** — a single login can
manage multiple Trakt-connected Users (family mode).
"""

from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from app.config import get_settings

SESSION_COOKIE = "reclio_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

# Separate cookie (signed) that remembers which Member (User row) the
# signed-in Account is currently "viewing as". Falls back to the Account's
# primary_user_id when absent. Short max-age so switching is ephemeral.
ACTIVE_MEMBER_COOKIE = "reclio_active_member"
ACTIVE_MEMBER_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _signer(salt: str = "reclio-session") -> TimestampSigner:
    return TimestampSigner(get_settings().secret_key, salt=salt)


def create_session_token(account_id: str) -> str:
    """Sign an Account.id for the primary login cookie."""
    return _signer().sign(account_id.encode()).decode()


def read_session_token(token: str | None, max_age: int = SESSION_MAX_AGE) -> str | None:
    """Verify and return the signed Account.id, or None if invalid/expired."""
    if not token:
        return None
    try:
        raw = _signer().unsign(token.encode(), max_age=max_age)
        return raw.decode()
    except (BadSignature, SignatureExpired):
        return None


def create_active_member_token(user_id: str) -> str:
    """Sign a User.id for the 'currently viewing as' member cookie."""
    return _signer(salt="reclio-active-member").sign(user_id.encode()).decode()


def read_active_member_token(
    token: str | None, max_age: int = ACTIVE_MEMBER_MAX_AGE
) -> str | None:
    if not token:
        return None
    try:
        raw = _signer(salt="reclio-active-member").unsign(token.encode(), max_age=max_age)
        return raw.decode()
    except (BadSignature, SignatureExpired):
        return None
