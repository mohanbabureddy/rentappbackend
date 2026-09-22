import unittest
from types import SimpleNamespace as N
from unittest import mock

from werkzeug.security import check_password_hash

from app import app as flask_app
from app.auth import generate_token


def _post(body, role="ADMIN", saved=None, existing=None):
    """Calls POST /api/users/add with a fake user repository (no real database)."""
    saved = saved if saved is not None else []

    def save(user):
        user.id = 7
        saved.append(user)
        return user

    # "someone" is the caller identified by the bearer token (checked by auth.py);
    # `existing`/`save` model the target account the route itself looks up.
    actor = N(username="someone", role=role, session_version=0)

    def find_by_username(u):
        return actor if u == "someone" else existing

    repo = N(find_by_username=find_by_username, save=save)
    with mock.patch("app.routes.UserRepository", return_value=repo), mock.patch("app.routes.get_db", return_value=object()), \
         mock.patch("app.auth.UserRepository", return_value=repo), mock.patch("app.auth.get_db", return_value=object()):
        headers = {"Authorization": "Bearer " + generate_token("someone", role)}
        return flask_app.test_client().post("/api/users/add", json=body, headers=headers)


class AddUserTest(unittest.TestCase):
    def test_username_alone_is_enough(self):
        saved = []
        r = _post({"username": "Room9"}, saved=saved)
        self.assertEqual(r.status_code, 201)
        user = saved[0]
        self.assertEqual((user.username, user.role, user.registration_completed), ("Room9", "TENANT", False))

    def test_account_has_an_unknown_password_until_the_tenant_registers(self):
        saved = []
        _post({"username": "Room9"}, saved=saved)
        stored = saved[0].password
        self.assertTrue(stored)
        for guess in ("", "password", "Room9", "123456"):
            self.assertFalse(check_password_hash(stored, guess))

    def test_two_accounts_never_share_a_placeholder_password(self):
        a, b = [], []
        _post({"username": "Room1"}, saved=a)
        _post({"username": "Room2"}, saved=b)
        self.assertNotEqual(a[0].password, b[0].password)

    def test_username_is_trimmed_and_required(self):
        saved = []
        self.assertEqual(_post({"username": "  Room3  "}, saved=saved).status_code, 201)
        self.assertEqual(saved[0].username, "Room3")
        self.assertEqual(_post({"username": "  "}).status_code, 400)
        self.assertEqual(_post({}).status_code, 400)

    def test_bad_role_rejected_and_duplicate_rejected(self):
        self.assertEqual(_post({"username": "Room4", "role": "OWNER"}).status_code, 400)
        self.assertEqual(_post({"username": "Room1"}, existing=N(username="Room1")).status_code, 409)

    def test_tenants_cannot_add_users(self):
        self.assertEqual(_post({"username": "Room5"}, role="TENANT").status_code, 403)

    def test_an_explicit_password_is_still_honoured(self):
        saved = []
        _post({"username": "Room6", "password": "Chosen#1"}, saved=saved)
        self.assertTrue(check_password_hash(saved[0].password, "Chosen#1"))


if __name__ == "__main__":
    unittest.main()
