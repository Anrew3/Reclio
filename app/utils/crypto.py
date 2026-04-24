from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


_settings = get_settings()
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = _settings.fernet_key
        if not key:
            raise RuntimeError("FERNET_KEY not configured")
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(value: str) -> str:
    if value is None:
        return None
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str | None:
    if not token:
        return None
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None
