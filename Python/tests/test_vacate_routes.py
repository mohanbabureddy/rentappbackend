import unittest
from types import SimpleNamespace as N
from unittest import mock

from app import app as flask_app
from app.auth import generate_token


def _client(repo, deposit_repo=None, username="Room1", role="TENANT"):
    actor = N(username=username, role=role, session_version=0)
    user_repo = N(find_by_username=lambda u: actor)
    empty_complaint_repo = N(find_by_tenant_name_order_by_created_desc=lambda u: [])
    empty_occupant_repo = N(find_by_tenant_username_order_by_uploaded_desc=lambda u: [])
    empty_bill_repo = N(find_by_tenant_name_order_by_month_desc=lambda u: [])
    with mock.patch("app.routes.VacateRequestRepository", return_value=repo), \
         mock.patch("app.routes.DepositRepository", return_value=deposit_repo or _FakeDepositRepo()), \
         mock.patch("app.routes.ComplaintRepository", return_value=empty_complaint_repo), \
         mock.patch("app.routes.OccupantRepository", return_value=empty_occupant_repo), \
         mock.patch("app.routes.TenantBillRepository", return_value=empty_bill_repo), \
         mock.patch("app.routes.get_db", return_value=object()), \
         mock.patch("app.auth.UserRepository", return_value=user_repo), \
         mock.patch("app.auth.get_db", return_value=object()):
        headers = {"Authorization": "Bearer " + generate_token(username, role, 0)}
        yield flask_app.test_client(), headers


class _FakeRepo:
    OPEN_STATUSES = ("PENDING", "APPROVED")

    def __init__(self, existing=None):
        self.by_id = {}
        for r in existing or []:
            r.id = len(self.by_id) + 1
            self.by_id[r.id] = r

    def find_open_by_tenant(self, username):
        matches = [r for r in self.by_id.values() if r.tenant_username == username and r.status in self.OPEN_STATUSES]
        return matches[-1] if matches else None

    def find_latest_by_tenant(self, username):
        matches = [r for r in self.by_id.values() if r.tenant_username == username]
        return matches[-1] if matches else None

    def find_all_open(self):
        return [r for r in self.by_id.values() if r.status in self.OPEN_STATUSES]

    def find_all_settled(self):
        return [r for r in self.by_id.values() if r.status == "SETTLED"]

    def find_by_id(self, request_id):
        return self.by_id.get(request_id)

    def save(self, request):
        if not getattr(request, "id", None):
            request.id = len(self.by_id) + 1
        self.by_id[request.id] = request
        return request


class _FakeDepositRepo:
    def __init__(self, totals=None):
        self.totals = totals or {}

    def total_for_tenant(self, username):
        return self.totals.get(username, 0.0)


class VacateRoutesTest(unittest.TestCase):
    def test_preview_needs_no_existing_request(self):
        for client, headers in _client(_FakeRepo()):
            r = client.get("/api/tenants/vacate/preview", headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertIn("vacateDate", r.get_json())

    def test_status_with_nothing_open_returns_null(self):
        for client, headers in _client(_FakeRepo()):
            r = client.get("/api/tenants/vacate/status", headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertIsNone(r.get_json())

    def test_request_then_status_then_cancel(self):
        repo = _FakeRepo()
        for client, headers in _client(repo):
            r = client.post("/api/tenants/vacate/request", headers=headers)
            self.assertEqual(r.status_code, 201)
            body = r.get_json()
            self.assertEqual(body["status"], "PENDING")

            r = client.get("/api/tenants/vacate/status", headers=headers)
            self.assertEqual(r.get_json()["tenantUsername"], "Room1")

            r = client.post("/api/tenants/vacate/request", headers=headers)
            self.assertEqual(r.status_code, 409)  # already open

            r = client.put("/api/tenants/vacate/cancel", headers=headers)
            self.assertEqual(r.status_code, 200)

            r = client.get("/api/tenants/vacate/status", headers=headers)
            self.assertIsNone(r.get_json())

    def test_cancel_with_nothing_open_is_404(self):
        for client, headers in _client(_FakeRepo()):
            r = client.put("/api/tenants/vacate/cancel", headers=headers)
            self.assertEqual(r.status_code, 404)

    def test_acknowledge_requires_a_settled_request(self):
        for client, headers in _client(_FakeRepo()):
            r = client.put("/api/tenants/vacate/acknowledge", headers=headers, json={"feedback": "n/a"})
            self.assertEqual(r.status_code, 409)

    def test_acknowledge_after_settlement(self):
        repo = _FakeRepo()
        deposit_repo = _FakeDepositRepo({"Room1": 5000.0})
        for client, headers in _client(repo, deposit_repo, username="Room1", role="TENANT"):
            r = client.post("/api/tenants/vacate/request", headers=headers)
            request_id = r.get_json()["id"]
        for client, headers in _client(repo, deposit_repo, username="mohan", role="ADMIN"):
            client.put(f"/api/admin/vacate/{request_id}/approve", headers=headers)
            client.put(f"/api/admin/vacate/{request_id}/settle", headers=headers, json={"deduction": 0, "refundMethod": "CASH"})
        for client, headers in _client(repo, deposit_repo, username="Room1", role="TENANT"):
            r = client.put("/api/tenants/vacate/acknowledge", headers=headers, json={"feedback": "Received, thanks."})
            self.assertEqual(r.status_code, 200)
            body = r.get_json()
            self.assertTrue(body["settlement"]["tenantAcknowledged"])
            self.assertEqual(body["settlement"]["tenantFeedback"], "Received, thanks.")

            r = client.put("/api/tenants/vacate/acknowledge", headers=headers, json={"feedback": "again"})
            self.assertEqual(r.status_code, 409)

    def test_admin_only_listing_rejects_a_tenant(self):
        for client, headers in _client(_FakeRepo(), role="TENANT"):
            r = client.get("/api/admin/vacate/all", headers=headers)
            self.assertEqual(r.status_code, 403)

    def test_admin_can_list_all_open_requests(self):
        for client, headers in _client(_FakeRepo(), username="mohan", role="ADMIN"):
            r = client.get("/api/admin/vacate/all", headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json(), [])

    def test_full_approve_and_settle_flow_as_admin(self):
        repo = _FakeRepo()
        deposit_repo = _FakeDepositRepo({"Room1": 10000.0})

        for client, headers in _client(repo, deposit_repo, username="Room1", role="TENANT"):
            r = client.post("/api/tenants/vacate/request", headers=headers)
            request_id = r.get_json()["id"]

        for client, headers in _client(repo, deposit_repo, username="mohan", role="ADMIN"):
            # A tenant can't approve their own request.
            pass

        for client, headers in _client(repo, deposit_repo, username="mohan", role="ADMIN"):
            r = client.put(f"/api/admin/vacate/{request_id}/approve", headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["status"], "APPROVED")
            # Regression: the approve route used to build VacateService without a
            # DepositRepository, so its response always had depositTotal: null even
            # though the request-list endpoint returned the real figure -- the admin
            # page would overwrite the correct number with null right after approving,
            # only fixing itself on the next full reload.
            self.assertEqual(r.get_json()["depositTotal"], 10000.0)

            r = client.put(
                f"/api/admin/vacate/{request_id}/settle",
                headers=headers,
                json={"deduction": 1500, "refundMethod": "upi", "note": "Wall scuff"},
            )
            self.assertEqual(r.status_code, 200)
            body = r.get_json()
            self.assertEqual(body["status"], "SETTLED")
            self.assertEqual(body["settlement"]["refundAmount"], 8500.0)
            self.assertEqual(body["settlement"]["refundMethod"], "UPI")

        for client, headers in _client(repo, deposit_repo, username="Room1", role="TENANT"):
            r = client.get("/api/tenants/vacate/status", headers=headers)
            body = r.get_json()
            self.assertEqual(body["status"], "SETTLED")
            self.assertEqual(body["settlement"]["refundAmount"], 8500.0)

    def test_cannot_cancel_after_owner_has_approved(self):
        repo = _FakeRepo()
        for client, headers in _client(repo, username="Room1", role="TENANT"):
            r = client.post("/api/tenants/vacate/request", headers=headers)
            request_id = r.get_json()["id"]
        for client, headers in _client(repo, username="mohan", role="ADMIN"):
            client.put(f"/api/admin/vacate/{request_id}/approve", headers=headers)
        for client, headers in _client(repo, username="Room1", role="TENANT"):
            r = client.put("/api/tenants/vacate/cancel", headers=headers)
            self.assertEqual(r.status_code, 409)
            r = client.get("/api/tenants/vacate/status", headers=headers)
            self.assertEqual(r.get_json()["status"], "APPROVED")

    def test_settled_listing_includes_open_complaint_and_unverified_occupant_counts(self):
        repo = _FakeRepo()
        deposit_repo = _FakeDepositRepo({"Room1": 5000.0})
        for client, headers in _client(repo, deposit_repo, username="Room1", role="TENANT"):
            r = client.post("/api/tenants/vacate/request", headers=headers)
            request_id = r.get_json()["id"]
        for client, headers in _client(repo, deposit_repo, username="mohan", role="ADMIN"):
            client.put(f"/api/admin/vacate/{request_id}/approve", headers=headers)
            client.put(f"/api/admin/vacate/{request_id}/settle", headers=headers, json={"deduction": 0, "refundMethod": "CASH"})

            complaint_repo = N(find_by_tenant_name_order_by_created_desc=lambda u: [N(status="OPEN"), N(status="CLOSED")])
            occupant_repo = N(find_by_tenant_username_order_by_uploaded_desc=lambda u: [N(verified=False)])
            bill_repo = N(find_by_tenant_name_order_by_month_desc=lambda u: [N(paid=False), N(paid=True)])
            with mock.patch("app.routes.ComplaintRepository", return_value=complaint_repo), \
                 mock.patch("app.routes.OccupantRepository", return_value=occupant_repo), \
                 mock.patch("app.routes.TenantBillRepository", return_value=bill_repo):
                r = client.get("/api/admin/vacate/settled", headers=headers)
            body = r.get_json()[0]
            self.assertEqual(body["openComplaints"], 1)
            self.assertEqual(body["unverifiedOccupants"], 1)
            self.assertEqual(body["unpaidBills"], 1)

    def test_admin_listing_includes_deposit_total(self):
        repo = _FakeRepo()
        deposit_repo = _FakeDepositRepo({"Room1": 15000.0})
        for client, headers in _client(repo, deposit_repo, username="Room1", role="TENANT"):
            client.post("/api/tenants/vacate/request", headers=headers)
        for client, headers in _client(repo, deposit_repo, username="mohan", role="ADMIN"):
            r = client.get("/api/admin/vacate/all", headers=headers)
            self.assertEqual(r.get_json()[0]["depositTotal"], 15000.0)

    def test_tenant_cannot_approve_a_request(self):
        repo = _FakeRepo()
        for client, headers in _client(repo, username="Room1", role="TENANT"):
            r = client.post("/api/tenants/vacate/request", headers=headers)
            request_id = r.get_json()["id"]
            r = client.put(f"/api/admin/vacate/{request_id}/approve", headers=headers)
            self.assertEqual(r.status_code, 403)

    def test_approve_unknown_id_is_404(self):
        for client, headers in _client(_FakeRepo(), username="mohan", role="ADMIN"):
            r = client.put("/api/admin/vacate/999/approve", headers=headers)
            self.assertEqual(r.status_code, 404)

    def test_admin_can_list_settled_requests_awaiting_finalization(self):
        repo = _FakeRepo()
        deposit_repo = _FakeDepositRepo({"Room1": 10000.0})
        for client, headers in _client(repo, deposit_repo, username="Room1", role="TENANT"):
            r = client.post("/api/tenants/vacate/request", headers=headers)
            request_id = r.get_json()["id"]
        for client, headers in _client(repo, deposit_repo, username="mohan", role="ADMIN"):
            client.put(f"/api/admin/vacate/{request_id}/approve", headers=headers)
            client.put(f"/api/admin/vacate/{request_id}/settle", headers=headers, json={"deduction": 0, "refundMethod": "CASH"})
            r = client.get("/api/admin/vacate/settled", headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(r.get_json()), 1)
            self.assertEqual(r.get_json()[0]["status"], "SETTLED")
            # Settled requests are no longer in the "open" listing.
            r2 = client.get("/api/admin/vacate/all", headers=headers)
            self.assertEqual(r2.get_json(), [])

    def test_finalize_calls_the_offboard_service_and_returns_its_result(self):
        stub_result = {"username": "Room1", "registrationKey": "WXYZ-9876"}
        stub_service = mock.Mock(finalize_move_out=mock.Mock(return_value=stub_result))
        for client, headers in _client(_FakeRepo(), username="mohan", role="ADMIN"):
            with mock.patch("app.routes.TenantOffboardService", return_value=stub_service):
                r = client.put("/api/admin/vacate/1/finalize", headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), stub_result)
        stub_service.finalize_move_out.assert_called_once_with(1, "mohan")

    def test_finalize_unknown_request_is_404(self):
        stub_service = mock.Mock(finalize_move_out=mock.Mock(side_effect=ValueError("No settled vacate request with that id.")))
        for client, headers in _client(_FakeRepo(), username="mohan", role="ADMIN"):
            with mock.patch("app.routes.TenantOffboardService", return_value=stub_service):
                r = client.put("/api/admin/vacate/999/finalize", headers=headers)
        self.assertEqual(r.status_code, 404)

    def test_finalize_blocked_without_tenant_acknowledgment_is_409(self):
        stub_service = mock.Mock(finalize_move_out=mock.Mock(side_effect=PermissionError("The tenant hasn't confirmed receiving their refund yet.")))
        for client, headers in _client(_FakeRepo(), username="mohan", role="ADMIN"):
            with mock.patch("app.routes.TenantOffboardService", return_value=stub_service):
                r = client.put("/api/admin/vacate/1/finalize", headers=headers)
        self.assertEqual(r.status_code, 409)

    def test_finalize_rejects_a_tenant(self):
        for client, headers in _client(_FakeRepo(), username="Room1", role="TENANT"):
            r = client.put("/api/admin/vacate/1/finalize", headers=headers)
            self.assertEqual(r.status_code, 403)

    def test_settle_rejects_invalid_refund_method(self):
        repo = _FakeRepo()
        deposit_repo = _FakeDepositRepo({"Room1": 5000.0})
        for client, headers in _client(repo, deposit_repo, username="Room1", role="TENANT"):
            r = client.post("/api/tenants/vacate/request", headers=headers)
            request_id = r.get_json()["id"]
        for client, headers in _client(repo, deposit_repo, username="mohan", role="ADMIN"):
            client.put(f"/api/admin/vacate/{request_id}/approve", headers=headers)
            r = client.put(
                f"/api/admin/vacate/{request_id}/settle",
                headers=headers,
                json={"deduction": 100, "refundMethod": "VENMO"},
            )
            self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
