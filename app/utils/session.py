"""Signed session tokens for portal authentication.

Uses itsdangerous to sign the authenticated user_id in a cookie so clients
cannot spoof another user by setting the cookie manually.
"""

from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from app.config import get_settings

SESSION_COOKIE = "reclio_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


def _signer() -> TimestampSigner:
    return TimestampSigner(get_settings().secret_key, salt="reclio-session")


def create_session_token(user_id: str) -> str:
    return _signer().sign(user_id.encode()).decode()


def read_session_token(token: str | None, max_age: int = SESSION_MAX_AGE) -> str | None:
    if not token:
        return None
    try:
        raw = _signer().unsign(token.encode(), max_age=max_age)
        return raw.decode()
    except (BadSignature, SignatureExpired):
        return None
