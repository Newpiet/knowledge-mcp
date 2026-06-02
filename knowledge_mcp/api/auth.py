"""Authentication utilities: password hashing and JWT tokens."""

import hashlib
import hmac
import secrets
from typing import Optional

import bcrypt
import jwt


_jwt_secret: str = ""


def init_auth(secret: Optional[str] = None) -> None:
    """Initialize auth with a JWT secret. Generates one if not provided."""
    global _jwt_secret
    _jwt_secret = secret or secrets.token_hex(32)


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_legacy_password(password: str, stored_hash: str) -> bool:
    """Verify against the old SHA-256 format (salt:hash)."""
    try:
        salt, pw_hash = stored_hash.split(":")
        return hmac.compare_digest(
            hashlib.sha256(f"{salt}{password}".encode()).hexdigest(),
            pw_hash,
        )
    except (ValueError, AttributeError):
        return False


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password. Handles both bcrypt and legacy SHA-256 hashes."""
    if stored_hash.startswith("$2"):
        return bcrypt.checkpw(password.encode(), stored_hash.encode())
    return _verify_legacy_password(password, stored_hash)


def needs_rehash(stored_hash: str) -> bool:
    """Return True if the stored hash is the legacy format and should be upgraded."""
    return not stored_hash.startswith("$2")


def create_token(user_id: int, username: str, kb_name: str, expires_hours: int = 72) -> str:
    """Create a signed JWT token."""
    import time
    payload = {
        "user_id": user_id,
        "username": username,
        "kb_name": kb_name,
        "exp": int(time.time()) + expires_hours * 3600,
    }
    return jwt.encode(payload, _jwt_secret, algorithm="HS256")


def decode_token(token: str) -> Optional[dict]:
    """Decode and verify a JWT token. Returns payload dict or None if invalid/expired."""
    try:
        return jwt.decode(token, _jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
