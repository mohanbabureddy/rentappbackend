import unittest
from types import SimpleNamespace as N
from unittest import mock

from app import app as flask_app
from app.services import generate_registration_key


def _user(**overrides):
    base = dict(
        id=5, username="Room1", role="TENANT", mail=None, phone=None, full_name=None,
        registration_completed=False, registration_code="ABCD-1234",
    )
    base.update(overrides)
    return N(**base)


def _start(body, user):
    repo = N(find_by_username=lambda u: user, save=lambda u: u)
    with mock.patch("app.routes.UserRepository", return_value=repo), \
         mock.patch("app.routes.get_db", return_value=object()), \
         mock.patch("app.services.OTPService.generate_otp", return_value="1234"):
        return flask_app.test_client().post("/api/users/registration/start", json=body)


VALID_BODY = {"username": "Room1", "email": "new@tenant.com", "phone": "9876543210", "fullName": "New Tenant"}


class RegistrationKeyTest(unittest.TestCase):
    def test_correct_key_succeeds(self):
        user = _user()
        r = _start({**VALID_BODY, "registrationKey": "ABCD-1234"}, user)
        self.assertEqual(r.status_code, 200)

    def test_wrong_key_is_rejected(self):
        user = _user()
        r = _start({**VALID_BODY, "registrationKey": "WRONG-KEY"}, user)
        self.assertEqual(r.status_code, 403)
        self.assertIn("Invalid registration key", r.get_json()["error"])
        # Must not have mutated the account's contact info on a rejected attempt.
        self.assertIsNone(user.mail)

    def test_missing_key_is_rejected(self):
        user = _user()
        r = _start(VALID_BODY, user)
        self.assertEqual(r.status_code, 403)

    def test_key_check_is_case_insensitive_and_trims_whitespace(self):
        user = _user()
        r = _start({**VALID_BODY, "registrationKey": "  abcd-1234  "}, user)
        self.assertEqual(r.status_code, 200)

    def test_legacy_account_with_no_key_set_skips_the_check(self):
        user = _user(registration_code=None)
        r = _start(VALID_BODY, user)
        self.assertEqual(r.status_code, 200)

    def test_key_generator_shape(self):
        key = generate_registration_key()
        self.assertRegex(key, r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}-[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}$")

    def test_key_generator_is_not_trivially_repeating(self):
        keys = {generate_registration_key() for _ in range(20)}
        self.assertEqual(len(keys), 20)


if __name__ == "__main__":
    unittest.main()
