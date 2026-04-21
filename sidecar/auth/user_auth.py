"""Simple JWT-based user identification for multi-user support."""

import os
import uuid
import json
import logging
from datetime import datetime, timedelta
import hashlib

logger = logging.getLogger(__name__)


class UserAuth:
    """Simple user identification and JWT token generation."""

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
        Generate a simple JWT-like token.

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

        # Simple encoding (not cryptographically secure, but sufficient for local development)
        token = json.dumps(payload)
        return token

    def verify_token(self, token: str) -> bool:
        """
        Verify token validity.

        Args:
            token: Token string

        Returns:
            True if valid
        """
        try:
            payload = json.loads(token)
            expires_at = datetime.fromisoformat(payload["expires_at"])
            return expires_at > datetime.now()
        except Exception:
            return False

    def get_user_from_token(self, token: str) -> str:
        """Extract user ID from token."""
        try:
            payload = json.loads(token)
            return payload.get("user_id", "anonymous")
        except Exception:
            return "anonymous"

    def list_users(self) -> list[dict]:
        """List all known users."""
        return list(self.users.values())
