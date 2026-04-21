"""Unit tests for user authentication."""

from sidecar.auth import UserAuth


class TestUserAuth:
    """Test user authentication and JWT."""

    def test_identify_user_default(self):
        """Identify user with default."""
        auth = UserAuth()
        user_id = auth.identify_user()
        assert user_id is not None
        assert isinstance(user_id, str)

    def test_identify_user_by_id(self):
        """Identify user by ID."""
        auth = UserAuth()
        user_id = auth.identify_user("alice")
        assert user_id == "alice"

    def test_identify_user_case_insensitive(self):
        """User ID normalization is case-insensitive."""
        auth = UserAuth()
        user_id = auth.identify_user("ALICE")
        assert user_id == "alice"

    def test_identify_user_cached(self):
        """User record is cached after identification."""
        auth = UserAuth()
        auth.identify_user("bob")
        assert "bob" in auth.users

    def test_identify_user_with_email(self):
        """Identify user with optional email."""
        auth = UserAuth()
        user_id = auth.identify_user("charlie", email="charlie@example.com")
        assert user_id == "charlie"
        assert auth.users["charlie"]["email"] == "charlie@example.com"

    def test_generate_token(self):
        """Generate JWT token."""
        auth = UserAuth()
        token = auth.generate_token("alice")
        assert token is not None
        assert auth.get_user_from_token(token) == "alice"

    def test_verify_token_valid(self):
        """Verify valid token."""
        auth = UserAuth()
        token = auth.generate_token("alice")
        assert auth.verify_token(token) is True

    def test_verify_token_rejects_tampering(self):
        """Signed tokens reject payload edits."""
        auth = UserAuth(secret_key="test-secret")
        token = auth.generate_token("alice")
        payload, _, signature = token.rpartition(".")
        tampered = f"{payload[:-1]}A" if payload[-1] != "A" else f"{payload[:-1]}B"
        assert auth.verify_token(f"{tampered}.{signature}") is False

    def test_verify_token_invalid(self):
        """Verify invalid token."""
        auth = UserAuth()
        assert auth.verify_token("invalid-token") is False

    def test_verify_token_expired(self):
        """Verify expired token."""
        import json
        from datetime import datetime, timedelta

        auth = UserAuth()
        # Create an expired token
        payload = {
            "user_id": "alice",
            "issued_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            "nonce": "test",
        }
        token = json.dumps(payload)
        assert auth.verify_token(token) is False

    def test_get_user_from_token(self):
        """Extract user from token."""
        auth = UserAuth()
        token = auth.generate_token("alice")
        user_id = auth.get_user_from_token(token)
        assert user_id == "alice"

    def test_get_user_from_invalid_token(self):
        """Get user from invalid token returns anonymous."""
        auth = UserAuth()
        user_id = auth.get_user_from_token("invalid")
        assert user_id == "anonymous"

    def test_list_users(self):
        """List all known users."""
        auth = UserAuth()
        auth.identify_user("alice")
        auth.identify_user("bob")
        users = auth.list_users()
        assert len(users) >= 2
        user_ids = [u["id"] for u in users]
        assert "alice" in user_ids
        assert "bob" in user_ids
