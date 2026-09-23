import json
import unittest
from datetime import date
from types import SimpleNamespace as N

from app.services import TenantOffboardService


class _FakeVacateRepo:
    def __init__(self, requests):
        self.by_id = {r.id: r for r in requests}
        self.by_tenant = {}
        for r in requests:
            self.by_tenant.setdefault(r.tenant_username, []).append(r)
        self.deleted_for = []

    def find_by_id(self, request_id):
        return self.by_id.get(request_id)

    def find_all_by_tenant(self, username):
        return self.by_tenant.get(username, [])

    def delete_all_for_tenant(self, username):
        self.deleted_for.append(username)
        self.by_tenant.pop(username, None)


class _FakeListRepo:
    """Stands in for TenantBillRepository/ComplaintRepository/OccupantRepository/
    DepositRepository -- all four have the same shape for what this service needs."""

    def __init__(self, find_method_name, items):
        setattr(self, find_method_name, lambda username: [i for i in items if self._owner(i) == username])
        self.deleted_for = []
        self.saved = []
        self._items = items

    @staticmethod
    def _owner(item):
        return getattr(item, "tenant_name", None) or getattr(item, "tenant_username", None)

    def delete_all_for_tenant(self, username):
        self.deleted_for.append(username)

    def save(self, item):
        self.saved.append(item)
        return item


class _FakeArchiveRepo:
    def __init__(self):
        self.saved = []

    def save(self, record):
        record.id = len(self.saved) + 1
        self.saved.append(record)
        return record


class _FakeUserRepo:
    def __init__(self, user):
        self.user = user
        self.saved = []

    def find_by_username(self, username):
        return self.user if self.user.username == username else None

    def save(self, user):
        self.saved.append(user)
        return user


def _settled_request(acknowledged=True):
    return N(
        id=1, tenant_username="Room1", requested_date=date(2026, 9, 22), vacate_date=date(2026, 11, 8),
        status="SETTLED", approved_date=None, cancelled_date=None,
        settlement_deduction=1000.0, settlement_refund_amount=9000.0, settlement_refund_method="CASH",
        settlement_note="Scuffed wall", settled_date=None,
        tenant_acknowledged=acknowledged, tenant_feedback="Received, thank you." if acknowledged else None,
        acknowledged_date=None,
    )


def _bill(paid=True):
    return N(tenant_name="Room1", month_year="2026-09", bill_type="RENT", rent=6000, water=200, electricity=None,
              miscellaneous=None, paid=paid, paid_date=None, created_date=date(2026, 9, 1))


def _complaint(status="CLOSED"):
    return N(tenant_name="Room1", description="Leaking tap", status=status, created_date=date(2026, 8, 20),
              resolution_comment="Fixed" if status == "CLOSED" else None, closed_date=None)


def _occupant(verified=True):
    return N(tenant_username="Room1", name="Ravindra Kumar", aadhar_file_name="a.pdf", aadhar_storage_path="aadhaar/Room1/a.pdf",
              uploaded_at=None, verified=verified, verified_by="OWNER" if verified else None, verified_at=None)


def _user():
    return N(
        username="Room1", role="TENANT", mail="old@tenant.com", phone="9876543210", full_name="Old Tenant",
        move_in_date=date(2025, 1, 1), demanded_deposit=10000.0, registration_completed=True,
        registration_code=None, session_version=3, password="oldhash",
    )


def _build_service(vacate_requests, bills=None, user=None, complaints=None, occupants=None):
    vacate_repo = _FakeVacateRepo(vacate_requests)
    bill_repo = _FakeListRepo("find_by_tenant_name_order_by_month_desc", bills or [])
    complaint_repo = _FakeListRepo("find_by_tenant_name_order_by_created_desc", complaints or [])
    occupant_repo = _FakeListRepo("find_by_tenant_username_order_by_uploaded_desc", occupants or [])
    deposit_repo = _FakeListRepo("find_by_tenant_order_by_date_desc", [])
    archive_repo = _FakeArchiveRepo()
    user_repo = _FakeUserRepo(user or _user())
    service = TenantOffboardService(vacate_repo, bill_repo, complaint_repo, occupant_repo, deposit_repo, archive_repo, user_repo)
    return service, vacate_repo, bill_repo, complaint_repo, occupant_repo, deposit_repo, archive_repo, user_repo


class TenantOffboardServiceTest(unittest.TestCase):
    def test_finalize_requires_a_settled_request(self):
        service, *_ = _build_service([N(id=1, tenant_username="Room1", status="APPROVED")])
        with self.assertRaises(ValueError):
            service.finalize_move_out(1, "mohan")

    def test_finalize_unknown_id_raises(self):
        service, *_ = _build_service([])
        with self.assertRaises(ValueError):
            service.finalize_move_out(999, "mohan")

    def test_finalize_blocked_until_tenant_acknowledges(self):
        service, *_ = _build_service([_settled_request(acknowledged=False)])
        with self.assertRaises(PermissionError):
            service.finalize_move_out(1, "mohan")

    def test_finalize_blocked_by_an_open_complaint(self):
        service, *_ = _build_service([_settled_request()], complaints=[_complaint(status="OPEN")])
        with self.assertRaises(PermissionError):
            service.finalize_move_out(1, "mohan")

    def test_finalize_allowed_when_all_complaints_closed(self):
        service, *_ = _build_service([_settled_request()], complaints=[_complaint(status="CLOSED"), _complaint(status="CLOSED")])
        service.finalize_move_out(1, "mohan")  # must not raise

    def test_finalize_blocked_by_an_unverified_occupant(self):
        service, *_ = _build_service([_settled_request()], occupants=[_occupant(verified=False)])
        with self.assertRaises(PermissionError):
            service.finalize_move_out(1, "mohan")

    def test_finalize_allowed_when_all_occupants_verified(self):
        service, *_ = _build_service([_settled_request()], occupants=[_occupant(verified=True), _occupant(verified=True)])
        service.finalize_move_out(1, "mohan")  # must not raise

    def test_finalize_blocked_by_an_unpaid_bill(self):
        service, *_ = _build_service([_settled_request()], bills=[_bill(paid=False)])
        with self.assertRaises(PermissionError):
            service.finalize_move_out(1, "mohan")

    def test_finalize_allowed_when_all_bills_paid(self):
        service, *_ = _build_service([_settled_request()], bills=[_bill(paid=True), _bill(paid=True)])
        service.finalize_move_out(1, "mohan")  # must not raise

    def test_finalize_never_changes_a_bills_paid_status(self):
        # An unpaid bill is real money owed to the owner -- freeing up a
        # username must never make it silently disappear by marking it paid.
        bill = _bill(paid=False)
        service, _vacate_repo, bill_repo, *_rest = _build_service([_settled_request()], bills=[bill])
        with self.assertRaises(PermissionError):
            service.finalize_move_out(1, "mohan")
        self.assertFalse(bill.paid)
        self.assertEqual(bill_repo.saved, [])

    def test_finalize_archives_a_snapshot(self):
        service, vacate_repo, bill_repo, *_rest, archive_repo, user_repo = _build_service(
            [_settled_request()], bills=[_bill(paid=True)],
        )
        result = service.finalize_move_out(1, "mohan")
        self.assertEqual(result["username"], "Room1")
        self.assertEqual(len(archive_repo.saved), 1)
        archived = archive_repo.saved[0]
        self.assertEqual(archived.username, "Room1")
        self.assertEqual(archived.archived_by, "mohan")
        snapshot = json.loads(archived.data)
        self.assertEqual(snapshot["fullName"], "Old Tenant")
        self.assertEqual(len(snapshot["bills"]), 1)
        self.assertEqual(snapshot["bills"][0]["monthYear"], "2026-09")
        self.assertTrue(snapshot["bills"][0]["paid"])
        self.assertEqual(len(snapshot["vacateRequests"]), 1)
        self.assertEqual(snapshot["vacateRequests"][0]["settlementRefundAmount"], 9000.0)
        self.assertTrue(snapshot["vacateRequests"][0]["tenantAcknowledged"])
        self.assertEqual(snapshot["vacateRequests"][0]["tenantFeedback"], "Received, thank you.")

    def test_finalize_clears_live_tables_for_that_tenant(self):
        service, vacate_repo, bill_repo, complaint_repo, occupant_repo, deposit_repo, *_ = _build_service(
            [_settled_request()], bills=[_bill()],
        )
        service.finalize_move_out(1, "mohan")
        self.assertIn("Room1", vacate_repo.deleted_for)
        self.assertIn("Room1", bill_repo.deleted_for)
        self.assertIn("Room1", complaint_repo.deleted_for)
        self.assertIn("Room1", occupant_repo.deleted_for)
        self.assertIn("Room1", deposit_repo.deleted_for)

    def test_finalize_resets_the_user_for_a_fresh_registration(self):
        user = _user()
        service, *_rest, user_repo = _build_service([_settled_request()], user=user)
        result = service.finalize_move_out(1, "mohan")

        self.assertFalse(user.registration_completed)
        self.assertIsNone(user.mail)
        self.assertIsNone(user.phone)
        self.assertIsNone(user.full_name)
        self.assertIsNone(user.move_in_date)
        self.assertIsNone(user.demanded_deposit)
        self.assertNotEqual(user.password, "oldhash")
        self.assertEqual(user.session_version, 4)  # bumped, invalidating any old session
        self.assertIsNotNone(user.registration_code)
        self.assertEqual(result["registrationKey"], user.registration_code)
        # Username itself is preserved -- that's the whole point, it's reused.
        self.assertEqual(user.username, "Room1")


if __name__ == "__main__":
    unittest.main()
