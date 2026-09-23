import unittest

from app.services import VacateService


class _FakeRepo:
    OPEN_STATUSES = ("PENDING", "APPROVED")

    def __init__(self, existing=None):
        self.by_id = {}
        self.saved = []
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

    def find_by_id(self, request_id):
        return self.by_id.get(request_id)

    def save(self, request):
        if not hasattr(request, "id") or request.id is None:
            request.id = len(self.by_id) + 1
        self.by_id[request.id] = request
        self.saved.append(request)
        return request


class _FakeDepositRepo:
    def __init__(self, totals=None):
        self.totals = totals or {}

    def total_for_tenant(self, username):
        return self.totals.get(username, 0.0)


class _FakeBillRepo:
    def __init__(self, bills=None):
        self.bills = bills or []

    def find_by_tenant_name_order_by_month_desc(self, username):
        return [b for b in self.bills if b.tenant_name == username]


class _Bill:
    def __init__(self, tenant_name, paid):
        self.tenant_name = tenant_name
        self.paid = paid


class VacateServiceTest(unittest.TestCase):
    def test_request_vacate_starts_pending(self):
        svc = VacateService(_FakeRepo())
        result = svc.request_vacate("Room1")
        self.assertEqual(result["tenantUsername"], "Room1")
        self.assertEqual(result["status"], "PENDING")
        # requestedDate must be "today" (IST) and vacateDate must be later than it
        self.assertLess(result["requestedDate"], result["vacateDate"])

    def test_cannot_request_twice_while_one_is_open(self):
        svc = VacateService(_FakeRepo())
        svc.request_vacate("Room1")
        with self.assertRaises(ValueError):
            svc.request_vacate("Room1")

    def test_request_vacate_blocked_by_an_unpaid_bill(self):
        bill_repo = _FakeBillRepo([_Bill("Room1", paid=False)])
        svc = VacateService(_FakeRepo(), bill_repo=bill_repo)
        with self.assertRaises(PermissionError):
            svc.request_vacate("Room1")

    def test_request_vacate_allowed_when_all_bills_paid(self):
        bill_repo = _FakeBillRepo([_Bill("Room1", paid=True), _Bill("Room1", paid=True)])
        svc = VacateService(_FakeRepo(), bill_repo=bill_repo)
        result = svc.request_vacate("Room1")  # must not raise
        self.assertEqual(result["status"], "PENDING")

    def test_request_vacate_skips_bill_check_when_no_bill_repo_given(self):
        # Backward compatible -- routes that don't need this check (there
        # aren't any today, but tests exercising the service directly
        # shouldn't be forced to wire one up) still work.
        svc = VacateService(_FakeRepo())
        result = svc.request_vacate("Room1")  # must not raise
        self.assertEqual(result["status"], "PENDING")

    def test_request_vacate_only_checks_the_requesting_tenants_bills(self):
        bill_repo = _FakeBillRepo([_Bill("Room2", paid=False)])
        svc = VacateService(_FakeRepo(), bill_repo=bill_repo)
        result = svc.request_vacate("Room1")  # Room2's unpaid bill must not block Room1
        self.assertEqual(result["status"], "PENDING")

    def test_get_status_returns_none_with_no_request(self):
        svc = VacateService(_FakeRepo())
        self.assertIsNone(svc.get_status("Room1"))

    def test_get_status_returns_the_pending_request(self):
        svc = VacateService(_FakeRepo())
        svc.request_vacate("Room1")
        status = svc.get_status("Room1")
        self.assertEqual(status["tenantUsername"], "Room1")
        self.assertEqual(status["status"], "PENDING")
        self.assertIsNone(status["settlement"])

    def test_cancel_clears_a_pending_request(self):
        svc = VacateService(_FakeRepo())
        svc.request_vacate("Room1")
        svc.cancel_vacate("Room1")
        self.assertIsNone(svc.get_status("Room1"))

    def test_cancel_with_nothing_open_raises(self):
        svc = VacateService(_FakeRepo())
        with self.assertRaises(ValueError):
            svc.cancel_vacate("Room1")

    def test_can_request_again_after_cancelling(self):
        svc = VacateService(_FakeRepo())
        svc.request_vacate("Room1")
        svc.cancel_vacate("Room1")
        result = svc.request_vacate("Room1")  # must not raise
        self.assertEqual(result["status"], "PENDING")

    def test_list_all_open_includes_pending_and_approved_across_tenants(self):
        svc = VacateService(_FakeRepo())
        r1 = svc.request_vacate("Room1")
        svc.request_vacate("Room2")
        svc.approve_vacate(r1["id"])
        svc.cancel_vacate("Room2")
        r3 = svc.request_vacate("Room3")
        open_requests = svc.list_all_open()
        self.assertEqual(sorted(r["tenantUsername"] for r in open_requests), ["Room1", "Room3"])
        self.assertEqual(next(r for r in open_requests if r["tenantUsername"] == "Room1")["status"], "APPROVED")
        self.assertEqual(next(r for r in open_requests if r["tenantUsername"] == "Room3")["status"], "PENDING")

    def test_preview_matches_what_a_real_request_would_calculate(self):
        svc = VacateService(_FakeRepo())
        preview = svc.preview_vacate_date()
        result = svc.request_vacate("Room1")
        self.assertEqual(preview, result["vacateDate"])

    def test_approve_moves_pending_to_approved(self):
        svc = VacateService(_FakeRepo())
        r = svc.request_vacate("Room1")
        approved = svc.approve_vacate(r["id"])
        self.assertEqual(approved["status"], "APPROVED")
        self.assertIsNotNone(approved["approvedDate"])
        self.assertEqual(svc.get_status("Room1")["status"], "APPROVED")

    def test_approve_unknown_id_raises(self):
        svc = VacateService(_FakeRepo())
        with self.assertRaises(ValueError):
            svc.approve_vacate(999)

    def test_approve_already_approved_request_raises(self):
        svc = VacateService(_FakeRepo())
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        with self.assertRaises(ValueError):
            svc.approve_vacate(r["id"])

    def test_tenant_cannot_cancel_after_approval(self):
        svc = VacateService(_FakeRepo())
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        with self.assertRaises(PermissionError):
            svc.cancel_vacate("Room1")
        # Still approved -- the failed cancel attempt must not have changed anything.
        self.assertEqual(svc.get_status("Room1")["status"], "APPROVED")

    def test_tenant_can_cancel_while_still_pending(self):
        svc = VacateService(_FakeRepo())
        svc.request_vacate("Room1")
        svc.cancel_vacate("Room1")  # must not raise
        self.assertIsNone(svc.get_status("Room1"))

    def test_settle_computes_refund_as_deposit_minus_deduction(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 20000.0}))
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        settled = svc.settle_vacate(r["id"], 3000.0, "UPI", "Paint touch-up")
        self.assertEqual(settled["status"], "SETTLED")
        self.assertEqual(settled["settlement"]["deduction"], 3000.0)
        self.assertEqual(settled["settlement"]["refundAmount"], 17000.0)
        self.assertEqual(settled["settlement"]["refundMethod"], "UPI")
        self.assertEqual(settled["settlement"]["note"], "Paint touch-up")

    def test_settle_with_zero_deduction_refunds_full_deposit(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 20000.0}))
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        settled = svc.settle_vacate(r["id"], 0, "CASH", None)
        self.assertEqual(settled["settlement"]["refundAmount"], 20000.0)

    def test_settle_before_approval_raises(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 20000.0}))
        r = svc.request_vacate("Room1")
        with self.assertRaises(ValueError):
            svc.settle_vacate(r["id"], 1000.0, "CASH", None)

    def test_settle_rejects_deduction_larger_than_deposit(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 5000.0}))
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        with self.assertRaises(ValueError):
            svc.settle_vacate(r["id"], 6000.0, "CASH", None)

    def test_settle_rejects_negative_deduction(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 5000.0}))
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        with self.assertRaises(ValueError):
            svc.settle_vacate(r["id"], -1, "CASH", None)

    def test_settle_rejects_invalid_refund_method(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 5000.0}))
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        with self.assertRaises(ValueError):
            svc.settle_vacate(r["id"], 100, "VENMO", None)

    def test_settled_request_no_longer_listed_as_open(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 5000.0}))
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        svc.settle_vacate(r["id"], 0, "CASH", None)
        self.assertEqual(svc.list_all_open(), [])

    def test_tenant_still_sees_settlement_via_get_status(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 5000.0}))
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        svc.settle_vacate(r["id"], 500, "BANK_TRANSFER", "Scratch on wall")
        status = svc.get_status("Room1")
        self.assertEqual(status["status"], "SETTLED")
        self.assertEqual(status["settlement"]["refundAmount"], 4500)

    def test_deposit_total_is_none_without_a_deposit_repo(self):
        svc = VacateService(_FakeRepo())
        r = svc.request_vacate("Room1")
        self.assertIsNone(r["depositTotal"])

    def test_deposit_total_is_included_when_a_deposit_repo_is_given(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 12000.0}))
        r = svc.request_vacate("Room1")
        self.assertEqual(r["depositTotal"], 12000.0)
        self.assertEqual(svc.list_all_open()[0]["depositTotal"], 12000.0)

    def test_acknowledge_settlement_succeeds_after_settling(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 5000.0}))
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        svc.settle_vacate(r["id"], 0, "CASH", None)
        result = svc.acknowledge_settlement("Room1", "Thanks, got it!")
        self.assertTrue(result["settlement"]["tenantAcknowledged"])
        self.assertEqual(result["settlement"]["tenantFeedback"], "Thanks, got it!")
        self.assertIsNotNone(result["settlement"]["acknowledgedDate"])

    def test_acknowledge_settlement_with_no_feedback_is_fine(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 5000.0}))
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        svc.settle_vacate(r["id"], 0, "CASH", None)
        result = svc.acknowledge_settlement("Room1", None)
        self.assertTrue(result["settlement"]["tenantAcknowledged"])
        self.assertIsNone(result["settlement"]["tenantFeedback"])

    def test_acknowledge_settlement_without_a_settled_request_raises(self):
        svc = VacateService(_FakeRepo())
        with self.assertRaises(ValueError):
            svc.acknowledge_settlement("Room1", "feedback")

    def test_acknowledge_settlement_while_still_pending_raises(self):
        svc = VacateService(_FakeRepo())
        svc.request_vacate("Room1")
        with self.assertRaises(ValueError):
            svc.acknowledge_settlement("Room1", "feedback")

    def test_acknowledge_settlement_twice_raises(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 5000.0}))
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        svc.settle_vacate(r["id"], 0, "CASH", None)
        svc.acknowledge_settlement("Room1", "First")
        with self.assertRaises(ValueError):
            svc.acknowledge_settlement("Room1", "Second")

    def test_newly_settled_request_is_not_acknowledged_yet(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 5000.0}))
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        settled = svc.settle_vacate(r["id"], 0, "CASH", None)
        self.assertFalse(settled["settlement"]["tenantAcknowledged"])

    def test_tenant_can_request_again_after_being_settled(self):
        svc = VacateService(_FakeRepo(), _FakeDepositRepo({"Room1": 5000.0}))
        r = svc.request_vacate("Room1")
        svc.approve_vacate(r["id"])
        svc.settle_vacate(r["id"], 0, "CASH", None)
        result = svc.request_vacate("Room1")  # must not raise -- previous tenancy is over
        self.assertEqual(result["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
