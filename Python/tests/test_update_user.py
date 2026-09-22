import unittest
from types import SimpleNamespace as N
from unittest import mock

from app import app as flask_app
from app.auth import generate_token


def _put(body, user, role="ADMIN"):
    """Calls PUT /api/users/<id> with a fake user repository (no real database)."""
    # "someone" is the caller identified by the bearer token (checked by auth.py);
    # `user` models the target account the route itself looks up by id.
    actor = N(username="someone", role=role, session_version=0)
    repo = N(find_by_id=lambda i: user, find_by_username=lambda u: actor, save=lambda u: u)
    with mock.patch("app.routes.UserRepository", return_value=repo), mock.patch("app.routes.get_db", return_value=object()), \
         mock.patch("app.auth.UserRepository", return_value=repo), mock.patch("app.auth.get_db", return_value=object()):
        headers = {"Authorization": "Bearer " + generate_token("someone", role)}
        return flask_app.test_client().put("/api/users/update/5", json=body, headers=headers)


def _user():
    return N(id=5, username="Room1", role="TENANT", password="x", mail="a@b.c", phone="9876543210", full_name="Ravindra", registration_completed=True)


class UpdateUserClearTest(unittest.TestCase):
    def test_empty_name_and_phone_are_cleared(self):
        user = _user()
        r = _put({"username": "Room1", "role": "TENANT", "fullName": "", "phone": ""}, user)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(user.full_name)
        self.assertIsNone(user.phone)

    def test_whitespace_only_also_clears(self):
        user = _user()
        _put({"fullName": "   ", "phone": "  "}, user)
        self.assertIsNone(user.full_name)
        self.assertIsNone(user.phone)

    def test_absent_fields_are_left_alone(self):
        user = _user()
        _put({"username": "Room1", "role": "TENANT"}, user)
        self.assertEqual((user.full_name, user.phone), ("Ravindra", "9876543210"))

    def test_new_values_are_saved_and_phone_is_validated(self):
        user = _user()
        _put({"fullName": "  New   Name ", "phone": "+91 98765-00000"}, user)
        self.assertEqual((user.full_name, user.phone), ("New Name", "9876500000"))
        self.assertEqual(_put({"phone": "12345"}, _user()).status_code, 400)

    def test_registration_status_can_be_reset_and_set(self):
        user = _user()
        self.assertEqual(_put({"registrationCompleted": False}, user).status_code, 200)
        self.assertFalse(user.registration_completed)
        self.assertEqual(_put({"registrationCompleted": True}, user).status_code, 200)
        self.assertTrue(user.registration_completed)

    def test_status_left_alone_when_not_sent(self):
        user = _user()
        _put({"phone": "9876543210"}, user)
        self.assertTrue(user.registration_completed)

    def test_an_admin_cannot_be_made_unregistered(self):
        admin = _user()
        admin.role = "ADMIN"
        self.assertEqual(_put({"registrationCompleted": False}, admin).status_code, 400)
        self.assertTrue(admin.registration_completed)

    def test_only_admins_can_update(self):
        self.assertEqual(_put({"phone": ""}, _user(), role="TENANT").status_code, 403)


if __name__ == "__main__":
    unittest.main()
