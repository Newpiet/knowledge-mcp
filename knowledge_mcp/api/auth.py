"""Authentication utilities: password hashing and JWT tokens."""

import hashlib
import hmac
import secrets
import json
import base64
import time
from typing import Optional


# JWT secret - in production, load from environment variable
_jwt_secret: str = ""


def init_auth(secret: Optional[str] = None) -> None:
    """Initialize auth with a JWT secret. Generates one if not provided."""
    global _jwt_secret
    _jwt_secret = secret or secrets.token_hex(32)


def hash_password(password: str) -> str:
    """Hash a password with a random salt using SHA-256."""
    salt = secrets.token_hex(16)
    pw_hash = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{pw_hash}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored hash."""
    try:
        salt, pw_hash = stored_hash.split(":")
        return hmac.compare_digest(
            hashlib.sha256(f"{salt}{password}".encode()).hexdigest(),
            pw_hash
        )
    except (ValueError, AttributeError):
        return False


def create_token(user_id: int, username: str, kb_name: str, expires_hours: int = 72) -> str:
    """Create a simple JWT-like token."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload_data = {
        "user_id": user_id,
        "username": username,
        "kb_name": kb_name,
        "exp": int(time.time()) + expires_hours * 3600,
    }
    payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).decode().rstrip("=")
    signature = hmac.new(_jwt_secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).hexdigest()
    return f"{header}.{payload}.{signature}"


def decode_token(token: str) -> Optional[dict]:
    """Decode and verify a token. Returns payload dict or None if invalid."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, payload, signature = parts
        # Verify signature
        expected_sig = hmac.new(_jwt_secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return None
        # Decode payload (add padding back)
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        payload_data = json.loads(base64.urlsafe_b64decode(payload))
        # Check expiration
        if payload_data.get("exp", 0) < time.time():
            return None
        return payload_data
    except Exception:
        return None
