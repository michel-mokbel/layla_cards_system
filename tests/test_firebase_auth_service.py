from __future__ import annotations

import unittest

from firebase_auth_service import (
    FirebaseAuthSession,
    auth_session_expiring,
    firebase_auth_error_message,
    refresh_id_token,
    sign_in_with_email_password,
)


class FirebaseAuthServiceTests(unittest.TestCase):
    def test_sign_in_with_email_password_builds_session(self) -> None:
        session = sign_in_with_email_password(
            "api-key",
            "ops@example.com",
            "secret123",
            post_json=lambda url, payload: {
                "localId": "uid-1",
                "email": payload["email"],
                "idToken": "id-token",
                "refreshToken": "refresh-token",
                "expiresIn": "3600",
            },
        )

        self.assertIsInstance(session, FirebaseAuthSession)
        self.assertEqual(session.uid, "uid-1")
        self.assertEqual(session.email, "ops@example.com")
        self.assertEqual(session.id_token, "id-token")
        self.assertEqual(session.refresh_token, "refresh-token")

    def test_refresh_id_token_preserves_email(self) -> None:
        session = refresh_id_token(
            "api-key",
            "refresh-token",
            email="ops@example.com",
            post_form=lambda _url, payload: {
                "user_id": "uid-1",
                "id_token": "new-id-token",
                "refresh_token": payload["refresh_token"],
                "expires_in": "3600",
            },
        )

        self.assertEqual(session.uid, "uid-1")
        self.assertEqual(session.email, "ops@example.com")
        self.assertEqual(session.id_token, "new-id-token")

    def test_auth_session_expiring_detects_far_future_false(self) -> None:
        session = FirebaseAuthSession(
            uid="uid",
            email="ops@example.com",
            id_token="token",
            refresh_token="refresh",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        self.assertFalse(auth_session_expiring(session))

    def test_firebase_auth_error_message_maps_codes(self) -> None:
        self.assertEqual(
            firebase_auth_error_message(RuntimeError("INVALID_PASSWORD")),
            "The password is incorrect.",
        )
        self.assertEqual(
            firebase_auth_error_message(RuntimeError("EMAIL_NOT_FOUND")),
            "No Firebase user exists for that email.",
        )


if __name__ == "__main__":
    unittest.main()
