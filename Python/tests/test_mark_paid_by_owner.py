import unittest
from types import SimpleNamespace as N
from unittest import mock

from app import app as flask_app
from app.auth import generate_token
from app.models import TenantBill
from app.services import TenantBillService


def _bill(**kw):
    defaults = dict(id=7, tenant_name="Room1", month_year="2026-09", bill_type="RENT", rent=500.0, paid=False)
    defaults.update(kw)
    return TenantBill(**defaults)


class MarkPaidServiceTest(unittest.TestCase):
    def setUp(self):
        self.repo = mock.MagicMock()
        self.service = TenantBillService(self.repo, mock.MagicMock(), email_service=None)

    def test_a_paid_online_bill_is_tagged_online_by_default(self):
        bill = _bill()
        self.repo.find_by_id.return_value = bill
        self.service.mark_paid(7)
        self.assertTrue(bill.paid)
        self.assertEqual(bill.paid_via, "ONLINE")

    def test_the_owner_marking_it_paid_is_tagged_owner(self):
        bill = _bill()
        self.repo.find_by_id.return_value = bill
        self.service.mark_paid(7, "OWNER")
        self.assertTrue(bill.paid)
        self.assertEqual(bill.paid_via, "OWNER")
        self.assertIsNotNone(bill.paid_date)


def _call(role, bill):
    actor = N(username="someone", role=role, session_version=0)
    bill_repo = N(find_by_id=lambda i: bill, save=lambda b: b)
    with mock.patch("app.routes.TenantBillRepository", return_value=bill_repo), \
         mock.patch("app.routes.get_db", return_value=object()), \
         mock.patch("app.routes.UserRepository", return_value=N(find_all=lambda: [], find_by_username=lambda u: None)), \
         mock.patch("app.auth.UserRepository", return_value=N(find_by_username=lambda u: actor)), \
         mock.patch("app.auth.get_db", return_value=object()):
        headers = {"Authorization": "Bearer " + generate_token("someone", role, 0)}
        return flask_app.test_client().put("/api/tenants/markPaidByOwner/7", headers=headers)


class MarkPaidRouteTest(unittest.TestCase):
    def test_the_owner_can_mark_an_unpaid_bill_paid(self):
        bill = _bill()
        r = _call("ADMIN", bill)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(bill.paid)
        self.assertEqual(bill.paid_via, "OWNER")

    def test_a_tenant_cannot_mark_their_own_bill_paid(self):
        bill = _bill()
        r = _call("TENANT", bill)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(bill.paid)

    def test_an_already_paid_bill_is_refused_and_not_changed(self):
        bill = _bill(paid=True, paid_via="ONLINE")
        r = _call("ADMIN", bill)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(bill.paid_via, "ONLINE")

    def test_an_unknown_bill_is_a_404(self):
        self.assertEqual(_call("ADMIN", None).status_code, 404)


if __name__ == "__main__":
    unittest.main()
