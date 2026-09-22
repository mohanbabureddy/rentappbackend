import json
import unittest
from types import SimpleNamespace as N
from unittest import mock

from app import app as flask_app
from app.auth import generate_token


def _admin_client(patches):
    actor = N(username="mohan", role="ADMIN", session_version=0)
    user_repo = N(find_by_username=lambda u: actor)
    with mock.patch("app.auth.UserRepository", return_value=user_repo), \
         mock.patch("app.auth.get_db", return_value=object()), \
         mock.patch("app.routes.get_db", return_value=object()), \
         patches:
        headers = {"Authorization": "Bearer " + generate_token("mohan", "ADMIN", 0)}
        yield flask_app.test_client(), headers


class ArchivedTenantsRouteTest(unittest.TestCase):
    def test_lists_archived_tenants_with_parsed_snapshot(self):
        record = N(
            id=1, username="Room1", archived_by="mohan",
            archived_date=None,
            data=json.dumps({"fullName": "Old Tenant", "bills": []}),
        )
        repo = N(find_all=lambda: [record])
        for client, headers in _admin_client(mock.patch("app.routes.ArchivedTenantRepository", return_value=repo)):
            r = client.get("/api/admin/archived-tenants", headers=headers)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["username"], "Room1")
        self.assertEqual(body[0]["archivedBy"], "mohan")
        self.assertEqual(body[0]["data"]["fullName"], "Old Tenant")

    def test_rejects_a_tenant(self):
        actor = N(username="Room1", role="TENANT", session_version=0)
        user_repo = N(find_by_username=lambda u: actor)
        with mock.patch("app.auth.UserRepository", return_value=user_repo), \
             mock.patch("app.auth.get_db", return_value=object()):
            headers = {"Authorization": "Bearer " + generate_token("Room1", "TENANT", 0)}
            r = flask_app.test_client().get("/api/admin/archived-tenants", headers=headers)
        self.assertEqual(r.status_code, 403)


class UsersListRegistrationKeyTest(unittest.TestCase):
    def test_registration_key_shown_only_when_not_registered(self):
        pending = N(id=1, username="Room2", phone=None, full_name=None, mail=None, role="TENANT",
                     registration_completed=False, move_in_date=None, demanded_deposit=None, registration_code="ABCD-1234")
        registered = N(id=2, username="Room1", phone=None, full_name="Ravi", mail=None, role="TENANT",
                        registration_completed=True, move_in_date=None, demanded_deposit=None, registration_code="OLD1-2345")
        user_repo = N(find_all=lambda: [pending, registered])
        deposit_repo = N(totals_by_tenant=lambda: {})
        for client, headers in _admin_client(mock.patch("app.routes.UserRepository", return_value=user_repo)):
            with mock.patch("app.routes.DepositRepository", return_value=deposit_repo):
                r = client.get("/api/users/all", headers=headers)
        body = {u["username"]: u for u in r.get_json()}
        self.assertEqual(body["Room2"]["registrationKey"], "ABCD-1234")
        self.assertIsNone(body["Room1"]["registrationKey"])


if __name__ == "__main__":
    unittest.main()
