"""Signed bearer-token user identification for multi-user support."""

import base64
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class UserAuth:
    """Simple user identification and signed token generation."""

    def __init__(self, secret_key: str = None):
        """
        Initialize user auth.

        Args:
            secret_key: Secret for JWT signing (env: AUTH_SECRET_KEY or auto-generated)
        """
        self.secret_key = secret_key or os.getenv("AUTH_SECRET_KEY", self._generate_secret())
        self.users = {}  # In-memory user store (in production: use a DB)

    @staticmethod
    def _generate_secret() -> str:
        """Generate a random secret key."""
        return hashlib.sha256(os.urandom(32)).hexdigest()

    def identify_user(self, user_id: str = None, email: str = None) -> str:
        """
        Identify user by ID or email.

        Args:
            user_id: User UUID or identifier
            email: User email (optional)

        Returns:
            Canonicalized user ID
        """
        if not user_id:
            user_id = os.getenv("USER_ID") or os.getenv("USERNAME") or "anonymous"

        # Normalize user ID
        user_id = str(user_id).lower().strip()

        # Cache user record
        if user_id not in self.users:
            self.users[user_id] = {
                "id": user_id,
                "email": email or f"{user_id}@local",
                "created_at": datetime.now().isoformat(),
                "last_active": datetime.now().isoformat(),
            }

        # Update last active
        self.users[user_id]["last_active"] = datetime.now().isoformat()

        return user_id

    def generate_token(self, user_id: str, duration_hours: int = 24) -> str:
        """
        Generate a signed bearer token.

        Args:
            user_id: User ID
            duration_hours: Token validity (hours)

        Returns:
            Token string
        """
        user_id = self.identify_user(user_id)

        # Create payload
        payload = {
            "user_id": user_id,
            "issued_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(hours=duration_hours)).isoformat(),
            "nonce": str(uuid.uuid4()),
        }

        payload_segment = self._encode_payload(payload)
        signature = self._sign(payload_segment)
        return f"{payload_segment}.{signature}"

    def verify_token(self, token: str) -> bool:
        """
        Verify token validity.

        Args:
            token: Token string

        Returns:
            True if valid
        """
        payload = self._decode_token(token)
        if not payload:
            return False
        try:
            expires_at = datetime.fromisoformat(payload["expires_at"])
        except Exception:
            return False
        return expires_at > datetime.now()

    def get_user_from_token(self, token: str) -> str:
        """Extract user ID from token."""
        payload = self._decode_token(token)
        if not payload:
            return "anonymous"
        return payload.get("user_id", "anonymous")

    def list_users(self) -> list[dict]:
        """List all known users."""
        return list(self.users.values())

    def _encode_payload(self, payload: dict) -> str:
        """Encode a token payload as a URL-safe segment."""
        payload_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return base64.urlsafe_b64encode(payload_text.encode("utf-8")).decode("ascii").rstrip("=")

    def _sign(self, payload_segment: str) -> str:
        """Sign a token payload with HMAC-SHA256."""
        return hmac.new(
            self.secret_key.encode("utf-8"),
            payload_segment.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _decode_token(self, token: str) -> dict | None:
        """Return verified token payload, or None when malformed/tampered."""
        try:
            payload_segment, separator, signature = token.rpartition(".")
            if not separator:
                return None
            expected = self._sign(payload_segment)
            if not hmac.compare_digest(signature, expected):
                return None
            padding = "=" * (-len(payload_segment) % 4)
            payload_text = base64.urlsafe_b64decode(f"{payload_segment}{padding}").decode("utf-8")
            return json.loads(payload_text)
        except Exception:
            return None
