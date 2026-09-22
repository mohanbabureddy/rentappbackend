import unittest
from types import SimpleNamespace as N
from unittest import mock

from app.auth import generate_token


class GenerateTokenVersionTest(unittest.TestCase):
    def test_ver_claim_defaults_to_zero(self):
        import jwt
        from app.auth import JWT_ALGORITHM, JWT_SECRET
        payload = jwt.decode(generate_token("Room1", "TENANT"), JWT_SECRET, algorithms=[JWT_ALGORITHM])
        self.assertEqual(payload["ver"], 0)

    def test_ver_claim_carries_the_given_session_version(self):
        import jwt
        from app.auth import JWT_ALGORITHM, JWT_SECRET
        payload = jwt.decode(generate_token("Room1", "TENANT", session_version=3), JWT_SECRET, algorithms=[JWT_ALGORITHM])
        self.assertEqual(payload["ver"], 3)


def _authenticate_with(token, user):
    """Runs auth._authenticate() inside a request context with the given bearer
    token, and a fake UserRepository that returns `user` for any lookup."""
    from app import app as flask_app
    from app.auth import _authenticate

    repo = N(find_by_username=lambda u: user)
    with mock.patch("app.auth.UserRepository", return_value=repo), mock.patch("app.auth.get_db", return_value=object()):
        with flask_app.test_request_context(headers={"Authorization": f"Bearer {token}"}):
            return _authenticate()


class AuthenticateSessionVersionTest(unittest.TestCase):
    def test_token_matching_current_session_version_is_accepted(self):
        user = N(username="Room1", role="TENANT", session_version=2)
        token = generate_token("Room1", "TENANT", session_version=2)
        self.assertEqual(_authenticate_with(token, user), {"username": "Room1", "role": "TENANT"})

    def test_token_from_an_earlier_login_is_rejected(self):
        # Room1 logged in twice: this token is from the FIRST login (ver=1), but a
        # second login has since bumped the account to ver=2 -- the first device's
        # token must stop working the moment the second device logs in.
        user = N(username="Room1", role="TENANT", session_version=2)
        stale_token = generate_token("Room1", "TENANT", session_version=1)
        self.assertIsNone(_authenticate_with(stale_token, user))

    def test_unknown_user_is_rejected(self):
        token = generate_token("Ghost", "TENANT", session_version=0)
        self.assertIsNone(_authenticate_with(token, None))


class LoginBumpsSessionVersionTest(unittest.TestCase):
    def test_each_successful_login_issues_a_higher_version_and_invalidates_the_last(self):
        """End-to-end: log in twice against the SAME fake user store and prove the
        first login's token stops authenticating once the second login has happened
        -- this is the actual "only one device at a time" behaviour."""
        from app import app as flask_app
        from werkzeug.security import generate_password_hash

        user = N(username="Room1", password=generate_password_hash("secret"), role="TENANT",
                  registration_completed=True, session_version=0, full_name=None)
        repo = N(find_by_username=lambda u: user, save=lambda u: u)

        with mock.patch("app.routes.UserRepository", return_value=repo), mock.patch("app.routes.get_db", return_value=object()), \
             mock.patch("app.auth.UserRepository", return_value=repo), mock.patch("app.auth.get_db", return_value=object()):
            client = flask_app.test_client()

            first = client.post("/api/users/login", json={"username": "Room1", "password": "secret"})
            self.assertEqual(first.status_code, 200)
            token_a = first.get_json()["token"]

            # Device A's token works right after logging in.
            r = client.get("/api/users/me/movein-deposit", headers={"Authorization": f"Bearer {token_a}"})
            self.assertNotEqual(r.status_code, 401)

            second = client.post("/api/users/login", json={"username": "Room1", "password": "secret"})
            self.assertEqual(second.status_code, 200)
            token_b = second.get_json()["token"]
            self.assertNotEqual(token_a, token_b)

            # Device A's old token is now rejected; Device B's is accepted.
            r = client.get("/api/users/me/movein-deposit", headers={"Authorization": f"Bearer {token_a}"})
            self.assertEqual(r.status_code, 401)
            self.assertIn("another device", r.get_json()["error"])

            r = client.get("/api/users/me/movein-deposit", headers={"Authorization": f"Bearer {token_b}"})
            self.assertNotEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
