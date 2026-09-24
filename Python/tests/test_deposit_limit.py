import unittest
from types import SimpleNamespace as N
from unittest import mock

from app import app as flask_app
from app.auth import generate_token
from app.services import DepositService


class _Users:
    def __init__(self, demanded):
        self.user = N(id=5, username="Room1", full_name="R", mail=None, role="TENANT", demanded_deposit=demanded, move_in_date=None)

    def find_by_id(self, i):
        return self.user

    def find_by_username(self, name):
        return self.user

    def find_all(self):
        return []


class _Deposits:
    def __init__(self, paid=0.0):
        self.paid = paid
        self.saved = []

    def save(self, payment):
        self.saved.append(payment)
        self.paid += payment.amount
        return payment

    def total_for_tenant(self, username):
        return self.paid

    def find_by_tenant_order_by_date_desc(self, username):
        return []


def _svc(demanded, paid=0.0):
    deposits = _Deposits(paid)
    return DepositService(deposits, _Users(demanded)), deposits


class DemandedDepositLimitTest(unittest.TestCase):
    def test_manual_entry_within_the_limit_is_saved(self):
        svc, deposits = _svc(20000.0, paid=15000.0)
        svc.add_manual_deposit(5, 5000.0)   # exactly the remaining amount
        self.assertEqual(len(deposits.saved), 1)

    def test_manual_entry_over_the_limit_is_refused_and_not_saved(self):
        svc, deposits = _svc(20000.0, paid=15000.0)
        with self.assertRaises(ValueError) as ctx:
            svc.add_manual_deposit(5, 5000.01)
        self.assertIn("at most ₹5000.00", str(ctx.exception))
        self.assertEqual(deposits.saved, [])

    def test_nothing_more_is_accepted_once_fully_paid(self):
        svc, deposits = _svc(20000.0, paid=20000.0)
        with self.assertRaises(ValueError) as ctx:
            svc.add_manual_deposit(5, 1.0)
        self.assertIn("already been paid in full", str(ctx.exception))
        self.assertEqual(deposits.saved, [])

    def test_no_demanded_amount_means_no_limit(self):
        svc, deposits = _svc(None, paid=999999.0)
        svc.add_manual_deposit(5, 50000.0)
        self.assertEqual(len(deposits.saved), 1)

    def test_floating_point_dust_does_not_block_an_exact_final_payment(self):
        svc, deposits = _svc(0.3, paid=0.1 + 0.2 - 0.2)   # 0.1 + tiny float error
        svc.add_manual_deposit(5, 0.2)
        self.assertEqual(len(deposits.saved), 1)

    def test_an_already_verified_online_payment_is_always_recorded(self):
        # Razorpay has taken the money by this point; refusing to record it
        # would leave the tenant charged with nothing on file.
        svc, deposits = _svc(20000.0, paid=19999.0)
        svc.record_payment("Room1", 500.0, "pay_1")
        self.assertEqual(len(deposits.saved), 1)


class CreateOrderRouteTest(unittest.TestCase):
    def _post(self, demanded, paid, amount):
        actor = N(username="Room1", role="TENANT", session_version=0)
        payments = mock.Mock()
        payments.create_deposit_order.return_value = {"orderId": "o1", "amount": int(amount * 100), "currency": "INR", "keyId": "k"}
        with mock.patch("app.routes.get_db", return_value=object()), \
             mock.patch("app.routes.UserRepository", return_value=_Users(demanded)), \
             mock.patch("app.routes.DepositRepository", return_value=_Deposits(paid)), \
             mock.patch("app.routes.TenantBillRepository", return_value=object()), \
             mock.patch("app.routes.PaymentService", return_value=payments), \
             mock.patch("app.auth.UserRepository", return_value=N(find_by_username=lambda u: actor)), \
             mock.patch("app.auth.get_db", return_value=object()):
            headers = {"Authorization": "Bearer " + generate_token("Room1", "TENANT", 0)}
            r = flask_app.test_client().post("/api/users/deposit/createOrder", headers=headers, json={"amount": amount})
        return r, payments

    def test_amount_over_the_limit_is_refused_before_any_order_is_created(self):
        r, payments = self._post(demanded=20000.0, paid=18000.0, amount=5000.0)
        self.assertEqual(r.status_code, 400)
        self.assertIn("at most ₹2000.00", r.get_json()["error"])
        payments.create_deposit_order.assert_not_called()   # nobody is charged

    def test_fully_paid_deposit_gets_no_order(self):
        r, payments = self._post(demanded=20000.0, paid=20000.0, amount=100.0)
        self.assertEqual(r.status_code, 400)
        payments.create_deposit_order.assert_not_called()

    def test_amount_within_the_limit_creates_the_order(self):
        r, payments = self._post(demanded=20000.0, paid=18000.0, amount=2000.0)
        self.assertEqual(r.status_code, 200)
        payments.create_deposit_order.assert_called_once()


if __name__ == "__main__":
    unittest.main()
