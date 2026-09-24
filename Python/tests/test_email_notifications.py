import unittest
from datetime import date
from types import SimpleNamespace as N
from unittest import mock

from app.services import DepositService, EmailService, VacateService


class _Emails:
    """Records what the service asked the email layer to send."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def _record(self, kind, *args):
        if self.fail:
            raise RuntimeError("mail down")
        self.calls.append((kind, args))

    def send_deposit_paid_email(self, *args):
        self._record("deposit_paid", *args)

    def send_manual_deposit_email(self, *args):
        self._record("manual_deposit", *args)

    def send_vacate_requested_email(self, *args):
        self._record("vacate_requested", *args)

    def send_settlement_email(self, *args):
        self._record("settlement", *args)


def _tenant(mail="tenant@x.com", demanded=20000.0):
    return N(id=5, username="Room1", full_name="Ravindra", mail=mail, role="TENANT", demanded_deposit=demanded, move_in_date=None)


def _admin(mail="owner@x.com"):
    return N(id=1, username="mohan", full_name=None, mail=mail, role="ADMIN", demanded_deposit=None, move_in_date=None)


class _Users:
    def __init__(self, *users):
        self.users = list(users)

    def find_by_id(self, i):
        return next((u for u in self.users if u.id == i), None)

    def find_by_username(self, name):
        return next((u for u in self.users if u.username == name), None)

    def find_all(self):
        return self.users


class _Deposits:
    def __init__(self, total=10000.0):
        self.total = total
        self.saved = []

    def save(self, payment):
        self.saved.append(payment)
        return payment

    def total_for_tenant(self, username):
        return self.total

    def find_by_tenant_order_by_date_desc(self, username):
        return []


class DepositEmailTest(unittest.TestCase):
    def test_online_payment_emails_tenant_and_owner(self):
        emails = _Emails()
        svc = DepositService(_Deposits(), _Users(_tenant(), _admin()), emails)
        svc.record_payment("Room1", 5000.0, "pay_123")
        kind, args = emails.calls[0]
        self.assertEqual(kind, "deposit_paid")
        # (name, amount, total, demanded, payment_id, tenant_email, admin_email)
        self.assertEqual(args, ("Ravindra", 5000.0, 10000.0, 20000.0, "pay_123", "tenant@x.com", "owner@x.com"))

    def test_online_payment_still_reaches_owner_when_tenant_has_no_email(self):
        emails = _Emails()
        svc = DepositService(_Deposits(), _Users(_tenant(mail=None), _admin()), emails)
        svc.record_payment("Room1", 5000.0, "pay_123")
        self.assertEqual(len(emails.calls), 1)
        self.assertIsNone(emails.calls[0][1][5])
        self.assertEqual(emails.calls[0][1][6], "owner@x.com")

    def test_online_payment_with_no_emails_anywhere_sends_nothing(self):
        emails = _Emails()
        svc = DepositService(_Deposits(), _Users(_tenant(mail=None), _admin(mail=None)), emails)
        svc.record_payment("Room1", 5000.0, "pay_123")
        self.assertEqual(emails.calls, [])

    def test_manual_entry_emails_the_tenant_only(self):
        emails = _Emails()
        svc = DepositService(_Deposits(), _Users(_tenant(), _admin()), emails)
        svc.add_manual_deposit(5, 2000.0, "Cash on arrival")
        self.assertEqual([c[0] for c in emails.calls], ["manual_deposit"])
        # (name, amount, total, demanded, notes, tenant_email)
        self.assertEqual(emails.calls[0][1], ("Ravindra", 2000.0, 10000.0, 20000.0, "Cash on arrival", "tenant@x.com"))

    def test_manual_entry_for_a_tenant_without_email_sends_nothing(self):
        emails = _Emails()
        svc = DepositService(_Deposits(), _Users(_tenant(mail=None), _admin()), emails)
        svc.add_manual_deposit(5, 2000.0)
        self.assertEqual(emails.calls, [])

    def test_a_mail_failure_never_undoes_the_deposit(self):
        deposits = _Deposits()
        svc = DepositService(deposits, _Users(_tenant(), _admin()), _Emails(fail=True))
        svc.record_payment("Room1", 5000.0, "pay_123")   # must not raise
        svc.add_manual_deposit(5, 2000.0)                # must not raise
        self.assertEqual(len(deposits.saved), 2)

    def test_without_an_email_service_nothing_is_attempted(self):
        deposits = _Deposits()
        svc = DepositService(deposits, _Users(_tenant(), _admin()))
        svc.record_payment("Room1", 5000.0, "pay_123")
        self.assertEqual(len(deposits.saved), 1)

    def test_invalid_manual_amount_sends_nothing(self):
        emails = _Emails()
        svc = DepositService(_Deposits(), _Users(_tenant(), _admin()), emails)
        with self.assertRaises(ValueError):
            svc.add_manual_deposit(5, 0)
        self.assertEqual(emails.calls, [])


class _VacateRepo:
    def find_open_by_tenant(self, username):
        return None

    def save(self, request):
        request.id = 1
        return request


class VacateEmailTest(unittest.TestCase):
    def _svc(self, emails, users):
        return VacateService(_VacateRepo(), user_repo=users, email_service=emails)

    def test_a_new_request_emails_the_owner(self):
        emails = _Emails()
        self._svc(emails, _Users(_tenant(), _admin())).request_vacate("Room1")
        kind, args = emails.calls[0]
        self.assertEqual(kind, "vacate_requested")
        name, requested, vacate, admin_email = args
        self.assertEqual(name, "Ravindra (Room1)")
        self.assertLess(requested, vacate)
        self.assertEqual(admin_email, "owner@x.com")

    def test_no_owner_email_skips_but_still_creates_the_request(self):
        emails = _Emails()
        result = self._svc(emails, _Users(_tenant(), _admin(mail=None))).request_vacate("Room1")
        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(emails.calls, [])

    def test_a_mail_failure_never_loses_the_request(self):
        result = self._svc(_Emails(fail=True), _Users(_tenant(), _admin())).request_vacate("Room1")
        self.assertEqual(result["status"], "PENDING")

    def test_a_blocked_request_sends_no_email(self):
        emails = _Emails()
        bills = N(find_by_tenant_name_order_by_month_desc=lambda u: [N(paid=False)])
        svc = VacateService(_VacateRepo(), bill_repo=bills, user_repo=_Users(_tenant(), _admin()), email_service=emails)
        with self.assertRaises(PermissionError):
            svc.request_vacate("Room1")
        self.assertEqual(emails.calls, [])


class _SettleRepo:
    """Holds one APPROVED request that settle_vacate can find and save."""

    def __init__(self):
        self.req = N(id=1, tenant_username="Room1", status="APPROVED", vacate_date=date(2026, 11, 8),
                     settlement_deduction=None, settlement_refund_amount=None, settlement_refund_method=None,
                     settlement_note=None, settled_date=None, requested_date=date(2026, 9, 25), approved_date=None,
                     tenant_acknowledged=False, tenant_feedback=None, acknowledged_date=None)

    def find_by_id(self, request_id):
        return self.req

    def save(self, request):
        return request


class SettlementEmailTest(unittest.TestCase):
    def _svc(self, emails, users):
        return VacateService(_SettleRepo(), _Deposits(total=20000.0), user_repo=users, email_service=emails)

    def test_settlement_emails_the_tenant_the_full_breakdown(self):
        emails = _Emails()
        self._svc(emails, _Users(_tenant(), _admin())).settle_vacate(1, 2500.0, "UPI", "Wall paint")
        self.assertEqual([c[0] for c in emails.calls], ["settlement"])
        # (name, move-out date, deposit, deduction, refund, method, note, tenant_email)
        self.assertEqual(emails.calls[0][1],
                         ("Ravindra", "2026-11-08", 20000.0, 2500.0, 17500.0, "UPI", "Wall paint", "tenant@x.com"))

    def test_settlement_goes_to_the_tenant_not_the_owner(self):
        emails = _Emails()
        self._svc(emails, _Users(_tenant(), _admin())).settle_vacate(1, 0, "CASH", None)
        self.assertEqual(len(emails.calls), 1)
        self.assertEqual(emails.calls[0][1][-1], "tenant@x.com")

    def test_no_tenant_email_skips_but_the_settlement_is_still_recorded(self):
        emails = _Emails()
        result = self._svc(emails, _Users(_tenant(mail=None), _admin())).settle_vacate(1, 0, "CASH", None)
        self.assertEqual(result["status"], "SETTLED")
        self.assertEqual(emails.calls, [])

    def test_a_mail_failure_never_undoes_the_settlement(self):
        result = self._svc(_Emails(fail=True), _Users(_tenant(), _admin())).settle_vacate(1, 500.0, "CASH", None)
        self.assertEqual(result["status"], "SETTLED")
        self.assertEqual(result["settlement"]["refundAmount"], 19500.0)

    def test_an_invalid_settlement_sends_nothing(self):
        emails = _Emails()
        svc = self._svc(emails, _Users(_tenant(), _admin()))
        with self.assertRaises(ValueError):
            svc.settle_vacate(1, 999999.0, "CASH", None)   # more than the deposit
        with self.assertRaises(ValueError):
            svc.settle_vacate(1, 100.0, "VENMO", None)     # unknown method
        self.assertEqual(emails.calls, [])


class EmailContentTest(unittest.TestCase):
    def setUp(self):
        self.email = EmailService()
        self.sent = []
        patcher = mock.patch.object(self.email, "_send_email", side_effect=lambda *a, **k: self.sent.append(a))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_deposit_paid_goes_to_both_addresses(self):
        self.email.send_deposit_paid_email("Ravindra", 5000.0, 10000.0, 20000.0, "pay_1", "t@x.com", "o@x.com")
        self.assertEqual([m[0] for m in self.sent], ["t@x.com", "o@x.com"])
        self.assertIn("5000.00", self.sent[0][1])
        self.assertIn("Remaining", self.sent[0][3])   # html body

    def test_deposit_paid_is_sent_once_when_both_addresses_match(self):
        self.email.send_deposit_paid_email("Ravindra", 5000.0, 10000.0, None, None, "same@x.com", "same@x.com")
        self.assertEqual(len(self.sent), 1)

    def test_free_text_is_html_escaped(self):
        self.email.send_manual_deposit_email("Ravindra", 100.0, 100.0, None, "<script>alert(1)</script>", "t@x.com")
        html_body = self.sent[0][3]
        self.assertNotIn("<script>", html_body)
        self.assertIn("&lt;script&gt;", html_body)

    def test_settlement_email_lists_every_figure_and_asks_for_confirmation(self):
        self.email.send_settlement_email("Ravindra", "2026-11-08", 20000.0, 2500.0, 17500.0, "BANK_TRANSFER", "Paint <b>", "t@x.com")
        recipient, subject, body, html_body = self.sent[0]
        self.assertEqual(recipient, "t@x.com")
        self.assertIn("17500.00", subject)
        for expected in ("20000.00", "2500.00", "17500.00", "Bank transfer", "2026-11-08"):
            self.assertIn(expected, body)
        self.assertIn("confirm", html_body)
        self.assertIn("Paint &lt;b&gt;", html_body)   # owner's note is escaped

    def test_vacate_email_states_who_and_when(self):
        self.email.send_vacate_requested_email("Ravindra (Room1)", "2026-09-25", "2026-11-08", "o@x.com")
        recipient, subject, body, html_body = self.sent[0]
        self.assertEqual(recipient, "o@x.com")
        self.assertIn("Ravindra (Room1)", subject)
        self.assertIn("2026-11-08", body)



class EmailBrandingTest(unittest.TestCase):
    """Every email carries the logo (as an attached image -- Gmail/Outlook strip
    inline <svg>) and, once configured, the business address."""

    def setUp(self):
        self.email = EmailService()

    def _all_html(self):
        bill = N(bill_type="RENT", rent=6000, water=200, electricity=None, miscellaneous=None,
                 tenant_name="Room1", month_year="2026-09")
        return {
            "bill invoice": self.email._bill_template(bill, "Room1", "Rent Invoice", "msg", "footer"),
            "notice": self.email._notice_template("Title", "Room1", "msg", [("a", "b")], "footer"),
            "otp": self.email._notice_template("Your OTP Code", None, "msg", [("OTP", "1234")], "footer"),
        }

    def test_every_template_uses_the_attached_logo_not_an_inline_svg(self):
        for name, html_body in self._all_html().items():
            self.assertIn("cid:vgr-logo", html_body, name)
            self.assertNotIn("<svg", html_body, name)

    def test_logo_file_is_a_real_png_and_is_attached_by_content_id(self):
        att = self.email._logo_attachment()
        self.assertEqual(att["content_id"], "vgr-logo")
        import base64
        self.assertTrue(base64.b64decode(att["content"]).startswith(b"\x89PNG"))

    def test_send_attaches_the_logo_only_when_the_html_uses_it(self):
        posted = []
        with mock.patch("app.services.requests.post", side_effect=lambda url, **kw: (posted.append(kw["json"]), N(raise_for_status=lambda: None))[1]), \
             mock.patch.object(self.email, "_resend_settings", return_value={"api_key": "k", "sender": "s@x.com"}):
            self.email._send_email("a@x.com", "s", "plain", self.email._notice_template("T", None, "m", [], "f"))
            self.email._send_email("a@x.com", "s", "plain only")
        self.assertEqual(posted[0]["attachments"][0]["content_id"], "vgr-logo")
        self.assertNotIn("attachments", posted[1])

    def test_otp_email_is_now_branded_html(self):
        sent = []
        with mock.patch.object(self.email, "_send_email", side_effect=lambda *a, **k: sent.append(a)):
            self.email.send_otp_email("a@x.com", "4821")
        self.assertIn("4821", sent[0][3])
        self.assertIn("cid:vgr-logo", sent[0][3])

    def _owner(self, name="Test Owner", address="1 Sample Street, Near Sample Park|Sample Taluk|Sample City-000000", phone="+91 00000 00000"):
        return mock.patch.object(EmailService, "_owner_details", return_value={
            "name": name, "address": [ln for ln in address.split("|") if ln], "phone": phone})

    def test_owner_name_address_and_phone_are_printed_in_every_template(self):
        with self._owner():
            for name, html_body in self._all_html().items():
                for expected in ("<strong>Test Owner</strong>", "1 Sample Street, Near Sample Park<br>Sample Taluk<br>Sample City-000000",
                                 "Phone: +91 00000 00000"):
                    self.assertIn(expected, html_body, name)

    def test_the_plain_text_version_carries_them_too(self):
        posted = []
        with self._owner(),              mock.patch("app.services.requests.post", side_effect=lambda url, **kw: (posted.append(kw["json"]), N(raise_for_status=lambda: None))[1]),              mock.patch.object(self.email, "_resend_settings", return_value={"api_key": "k", "sender": "s@x.com"}):
            self.email._send_email("a@x.com", "s", "Hello there", self.email._notice_template("T", None, "m", [], "f"))
        self.assertIn("Hello there", posted[0]["text"])
        self.assertIn("Test Owner", posted[0]["text"])
        self.assertIn("Phone: +91 00000 00000", posted[0]["text"])

    def test_anything_not_configured_is_left_out(self):
        with self._owner(name="", address="", phone="+91 1"):
            footer = self.email._footer_html()
        self.assertIn("Phone: +91 1", footer)
        self.assertNotIn("<strong>", footer)

    def test_no_footer_at_all_when_nothing_is_configured(self):
        with self._owner(name="", address="", phone=""):
            self.assertEqual(self.email._footer_html(), "")
            self.assertEqual(self.email._footer_text(), "")

    def test_settings_are_read_from_the_environment(self):
        env = {"MAIL_OWNER_NAME": "Test Owner", "MAIL_ADDRESS": "Line 1|Line 2", "MAIL_PHONE": "12345"}
        with mock.patch.dict("os.environ", env), mock.patch.object(self.email, "_load_dotenv", return_value={}):
            self.assertEqual(self.email._owner_details(), {"name": "Test Owner", "address": ["Line 1", "Line 2"], "phone": "12345"})

    def test_owner_details_are_html_escaped(self):
        with self._owner(name="A & B", address="<Co>", phone="1"):
            footer = self.email._footer_html()
        self.assertIn("A &amp; B", footer)
        self.assertIn("&lt;Co&gt;", footer)


class EmailSubjectTest(unittest.TestCase):
    """Every email has a subject that starts with the brand and says what it is
    about -- and who, so the owner's copy of a payment or deposit isn't anonymous."""

    def setUp(self):
        self.email = EmailService()
        self.subjects = {}
        self._current = None
        patcher = mock.patch.object(self.email, "_send_email", side_effect=lambda to, subj, *a, **k: self.subjects.setdefault(self._current, subj))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _subject_of(self, key, send):
        self._current = key
        send()
        return self.subjects[key]

    def test_every_email_type_has_a_branded_subject_naming_what_and_who(self):
        e = self.email
        bill = N(bill_type="RENT", rent=6000, water=200, electricity=None, miscellaneous=None, tenant_name="Room1", month_year="2026-09")
        elec = N(bill_type="ELECTRICITY", rent=None, water=None, electricity=450, miscellaneous=None, tenant_name="Room1", month_year="2026-09")
        cases = {
            "otp": (lambda: e.send_otp_email("a@x.com", "1234"), ["Your OTP code"]),
            "bill_generated": (lambda: e.notify_bill_generated(bill, "a@x.com", "2026-09"), ["Rent bill generated", "2026-09", "Room1"]),
            "bill_paid_rent": (lambda: e.send_bill_paid_email(bill, "a@x.com", "o@x.com"), ["Rent payment received", "2026-09", "Room1"]),
            "bill_paid_elec": (lambda: e.send_bill_paid_email(elec, "a@x.com", "o@x.com"), ["Electricity payment received", "Room1"]),
            "deposit_paid": (lambda: e.send_deposit_paid_email("Ravindra", 5000.0, 5000.0, None, None, "a@x.com", None), ["Deposit payment received from Ravindra", "5000.00"]),
            "deposit_manual": (lambda: e.send_manual_deposit_email("Ravindra", 2000.0, 2000.0, None, None, "a@x.com"), ["Deposit of", "2000.00"]),
            "vacate": (lambda: e.send_vacate_requested_email("Ravindra (Room1)", "2026-09-25", "2026-11-08", "o@x.com"), ["Vacate request from Ravindra (Room1)"]),
            "settlement": (lambda: e.send_settlement_email("Ravindra", "2026-11-08", 20000.0, 2500.0, 17500.0, "UPI", None, "a@x.com"), ["Full and final settlement", "17500.00"]),
        }
        for key, (send, expected) in cases.items():
            subject = self._subject_of(key, send)
            self.assertTrue(subject.startswith("VGR: "), f"{key}: {subject}")
            for part in expected:
                self.assertIn(part, subject, key)


if __name__ == "__main__":
    unittest.main()
