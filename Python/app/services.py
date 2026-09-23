import json
import logging
import os
import random
import re
import secrets
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import bcrypt
import requests
from werkzeug.security import check_password_hash, generate_password_hash

from app.models import ArchivedTenant, Complaint, DepositPayment, Occupant, TenantBill, TransactionLog, User, VacateRequest, utc_now
from app.repositories import ArchivedTenantRepository, ComplaintRepository, DepositRepository, OccupantRepository, TenantBillRepository, TransactionLogRepository, UserRepository, VacateRequestRepository
from app.vacate import calculate_vacate_date

# Excludes 0/O/1/I so a code read aloud over the phone can't be confused --
# these are handed from owner to incoming tenant outside the app (in person,
# a call, a text), so they have to survive being spoken, not just typed.
_REGISTRATION_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_registration_key() -> str:
    chars = [secrets.choice(_REGISTRATION_KEY_ALPHABET) for _ in range(8)]
    return "".join(chars[:4]) + "-" + "".join(chars[4:])

UPLOADS_ROOT = Path(__file__).resolve().parents[1] / "uploads"

# Render's disk is ephemeral -- anything written to UPLOADS_ROOT is wiped on
# every restart/redeploy. When these are set, occupant documents go to a
# private Supabase Storage bucket instead, which survives deploys. Falls
# back to the local disk (original behavior) when unset, so local dev
# doesn't require Supabase credentials.
_SUPABASE_URL = os.getenv("SUPABASE_URL")
_SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
_SUPABASE_BUCKET = "aadhaar"


def _storage_configured() -> bool:
    return bool(_SUPABASE_URL and _SUPABASE_SERVICE_KEY)


def _storage_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_SUPABASE_SERVICE_KEY}", "apikey": _SUPABASE_SERVICE_KEY}


def _storage_object_url(object_path: str) -> str:
    return f"{_SUPABASE_URL}/storage/v1/object/{_SUPABASE_BUCKET}/{object_path}"


def fetch_uploaded_file(relative_path: str):
    """Returns (content_bytes, content_type) for a path like 'aadhaar/<tenant>/<file>',
    from Supabase Storage if configured, else the local uploads/ disk. None if missing."""
    if _storage_configured():
        object_path = relative_path.split("/", 1)[-1]
        resp = requests.get(_storage_object_url(object_path), headers=_storage_headers(), timeout=15)
        if not resp.ok:
            return None
        return resp.content, resp.headers.get("Content-Type", "application/octet-stream")

    file_path = UPLOADS_ROOT / relative_path
    if not file_path.is_file():
        return None
    return file_path.read_bytes(), None


def to_iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime for the API with an explicit UTC marker. Every timestamp in
    this app is set via utc_now(), which is naive (no tzinfo) -- plain
    dt.isoformat() then produces a string with no timezone indicator at all. The
    browser's `new Date(str)` parses such a string as local time instead of UTC, so
    timestamps render shifted by the viewer's UTC offset (e.g. ~5:30h off in IST).
    Appending "Z" tells the browser it's UTC so it converts to local time correctly."""
    if dt is None:
        return None
    return dt.isoformat() + "Z"


def verify_password(stored_hash: str, password: str) -> bool:
    """Verify a password against either a werkzeug hash or a legacy bcrypt hash
    (accounts created by the old Java/Spring backend, which uses BCryptPasswordEncoder)."""
    if stored_hash.startswith(("$2a$", "$2b$", "$2y$")):
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
    return check_password_hash(stored_hash, password)


class LoginLockedError(Exception):
    """Raised when a username has too many recent failed login attempts. Keyed
    by username rather than IP -- Render's request.remote_addr is always
    127.0.0.1 (the app sits behind Render's proxy without X-Forwarded-For
    parsing), so IP-based throttling would silently do nothing."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Too many failed login attempts. Try again in {retry_after_seconds}s.")


class LoginThrottle:
    MAX_FAILED_ATTEMPTS = 5
    LOCKOUT_SECONDS = 15 * 60
    _attempts: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def check(cls, username: str) -> None:
        entry = cls._attempts.get(username)
        if entry is None:
            return
        locked_until = entry.get("locked_until")
        if locked_until is None:
            return
        remaining = locked_until - time.time()
        if remaining > 0:
            raise LoginLockedError(int(remaining) + 1)
        cls._attempts.pop(username, None)

    @classmethod
    def record_failure(cls, username: str) -> None:
        entry = cls._attempts.setdefault(username, {"failures": 0, "locked_until": None})
        entry["failures"] += 1
        if entry["failures"] >= cls.MAX_FAILED_ATTEMPTS:
            entry["locked_until"] = time.time() + cls.LOCKOUT_SECONDS

    @classmethod
    def record_success(cls, username: str) -> None:
        cls._attempts.pop(username, None)


class OTPCooldownError(Exception):
    """Raised when an OTP is requested again before OTP_COOLDOWN_SECONDS has
    elapsed for that email, to stop registration/forgot-password being used
    to spam an inbox or burn through Resend's daily send quota."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Please wait {retry_after_seconds}s before requesting another OTP.")


class OTPService:
    OTP_TTL_MILLIS = 5 * 60 * 1000
    OTP_COOLDOWN_SECONDS = 60
    otp_storage: Dict[str, Dict[str, Any]] = {}

    def __init__(self, email_service: "EmailService"):
        self.email_service = email_service
        self.logger = logging.getLogger("app.services")

    def generate_otp(self, email: str) -> str:
        existing = self.otp_storage.get(email)
        if existing is not None:
            elapsed = time.time() - existing.get("requested_at", 0)
            if elapsed < self.OTP_COOLDOWN_SECONDS:
                retry_after = int(self.OTP_COOLDOWN_SECONDS - elapsed) + 1
                self.logger.warning("OTP request for %s throttled; retry after %ss.", email, retry_after)
                raise OTPCooldownError(retry_after)

        otp = str(random.randint(1000, 9999))
        self.otp_storage[email] = {
            "otp": otp,
            "expires_at": time.time() * 1000 + self.OTP_TTL_MILLIS,
            "requested_at": time.time(),
        }
        self.logger.info("Generated OTP for %s (valid for 5 minutes).", email)
        self.email_service.send_otp_email(email, otp)
        return otp

    def verify_otp(self, email: str, otp: str) -> bool:
        entry = self.otp_storage.get(email)
        if entry is None:
            self.logger.warning("OTP verification failed for %s: no OTP on record (not requested or already used).", email)
            return False
        if time.time() * 1000 > entry["expires_at"]:
            self.otp_storage.pop(email, None)
            self.logger.warning("OTP verification failed for %s: OTP expired.", email)
            return False
        is_valid = otp == entry["otp"]
        if is_valid:
            self.otp_storage.pop(email, None)
            self.logger.info("OTP verified successfully for %s.", email)
        else:
            self.logger.warning("OTP verification failed for %s: incorrect code.", email)
        return is_valid


class UserService:
    def __init__(self, user_repo: UserRepository):
        self.user_repo = user_repo
        self.logger = logging.getLogger("app.services")

    def register_user(self, mail: str, password: str) -> User:
        user = User(
            mail=mail,
            password=generate_password_hash(password),
            role="TENANT",
            registration_completed=False,
        )
        saved = self.user_repo.save(user)
        self.logger.info("Registered new tenant account: id=%s mail=%s", saved.id, saved.mail)
        return saved


class DepositService:
    """Security-deposit ledger. Total deposited is always computed as the sum
    of DepositRepository rows for a tenant -- never a single stored number --
    so an admin's manual entry and a tenant's own Razorpay payment can never
    silently overwrite each other."""

    def __init__(self, deposit_repo: DepositRepository, user_repo: UserRepository):
        self.deposit_repo = deposit_repo
        self.user_repo = user_repo
        self.logger = logging.getLogger("app.services")

    def _entry_dto(self, p: DepositPayment) -> Dict[str, Any]:
        return {
            "id": p.id,
            "amount": p.amount,
            "source": p.source,
            "notes": p.notes,
            "paidDate": to_iso_utc(p.paid_date),
        }

    def get_summary(self, username: str) -> Dict[str, Any]:
        user = self.user_repo.find_by_username(username)
        if user is None:
            raise ValueError("User not found")
        history = self.deposit_repo.find_by_tenant_order_by_date_desc(username)
        return {
            "moveInDate": user.move_in_date.isoformat() if user.move_in_date else None,
            "demandedDeposit": user.demanded_deposit,
            "totalAmountDeposited": self.deposit_repo.total_for_tenant(username),
            "history": [self._entry_dto(p) for p in history],
        }

    def set_move_in_date(self, user_id: int, move_in_date: str) -> None:
        user = self.user_repo.find_by_id(user_id)
        if user is None:
            raise ValueError("User not found")
        try:
            user.move_in_date = datetime.strptime(move_in_date, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("moveInDate must be in YYYY-MM-DD format")
        self.user_repo.save(user)
        self.logger.info("Set move-in date for '%s' to %s.", user.username, user.move_in_date)

    def set_demanded_deposit(self, user_id: int, amount: float) -> None:
        user = self.user_repo.find_by_id(user_id)
        if user is None:
            raise ValueError("User not found")
        if amount < 0:
            raise ValueError("Demanded deposit cannot be negative")
        user.demanded_deposit = amount
        self.user_repo.save(user)
        self.logger.info("Set demanded deposit for '%s' to %s.", user.username, amount)

    def add_manual_deposit(self, user_id: int, amount: float, notes: Optional[str] = None) -> Dict[str, Any]:
        user = self.user_repo.find_by_id(user_id)
        if user is None:
            raise ValueError("User not found")
        if amount <= 0:
            raise ValueError("Amount must be greater than zero")
        payment = DepositPayment(tenant_username=user.username, amount=amount, source="manual", notes=notes)
        self.deposit_repo.save(payment)
        self.logger.info("Admin recorded manual deposit of %s for '%s'.", amount, user.username)
        return self.get_summary(user.username)

    def record_payment(self, username: str, amount: float, payment_id: str) -> Dict[str, Any]:
        payment = DepositPayment(tenant_username=username, amount=amount, source="razorpay", payment_id=payment_id)
        self.deposit_repo.save(payment)
        self.logger.info("Recorded deposit payment of %s for '%s' (payment_id=%s).", amount, username, payment_id)
        return self.get_summary(username)


class TenantBillService:
    def __init__(self, repo: TenantBillRepository, user_repo: UserRepository, email_service: Optional["EmailService"] = None):
        self.repo = repo
        self.user_repo = user_repo
        self.email_service = email_service
        self.logger = logging.getLogger("app.services")

    def get_tenant_bills(self, name: str) -> List[TenantBill]:
        return self.repo.find_by_tenant_name_order_by_month_desc(name)

    def _tenant_user(self, tenant_name: Optional[str]) -> Optional[User]:
        if not tenant_name:
            return None
        return self.user_repo.find_by_username(tenant_name)

    def _admin_user(self) -> Optional[User]:
        for user in self.user_repo.find_all():
            if user.role == "ADMIN":
                return user
        return None

    def mark_paid(self, bill_id: int) -> str:
        bill = self.repo.find_by_id(bill_id)
        if bill is None:
            self.logger.warning("Mark-paid failed: bill %s not found.", bill_id)
            raise ValueError("Bill not found")
        bill.paid = True
        bill.paid_date = utc_now()
        self.repo.save(bill)
        self.logger.info("Marked bill %s as paid for tenant %s (%s).", bill_id, bill.tenant_name, bill.month_year)
        if self.email_service is not None:
            tenant = self._tenant_user(bill.tenant_name)
            admin = self._admin_user()
            try:
                if tenant and tenant.mail:
                    self.email_service.send_bill_paid_email(bill, tenant.mail, admin.mail if admin and admin.mail else tenant.mail)
                elif admin and admin.mail:
                    self.email_service.send_bill_paid_email(bill, admin.mail, admin.mail)
                else:
                    self.logger.warning(
                        "Skipping payment notification for bill %s: no tenant or admin email on file (tenant=%s, admin=%s).",
                        bill_id, bill.tenant_name, admin.username if admin else None,
                    )
            except Exception:
                self.logger.exception("Failed to send payment notification for bill %s; payment was still recorded.", bill_id)
        return "Payment marked as paid and invoice sent."

    def add_bill(self, bill: TenantBill) -> str:
        existing = self.repo.find_by_tenant_name_and_month(bill.tenant_name, bill.month_year, bill.bill_type)
        if existing is not None:
            self.logger.warning("Add-bill rejected: %s bill already exists for tenant %s, month %s.", bill.bill_type, bill.tenant_name, bill.month_year)
            raise ValueError(f"{bill.bill_type.title()} bill already exists for this tenant and month.")
        bill.created_date = date.today()
        self.repo.save(bill)
        self.logger.info("Added new bill for tenant %s, month %s (rent=%s, water=%s, electricity=%s, miscellaneous=%s).",
                          bill.tenant_name, bill.month_year, bill.rent, bill.water, bill.electricity, bill.miscellaneous)
        if self.email_service is not None:
            tenant = self._tenant_user(bill.tenant_name)
            try:
                if tenant and tenant.mail:
                    self.email_service.notify_bill_generated(bill, tenant.mail, bill.month_year or "current month")
                else:
                    self.logger.warning(
                        "Skipping bill-generated notification for tenant %s: no tenant found or no email on file.",
                        bill.tenant_name,
                    )
            except Exception:
                self.logger.exception("Failed to send bill-generated notification for tenant %s; bill was still saved.", bill.tenant_name)
        return "Bill added successfully and notification triggered."

    def delete_bill(self, bill_id: int) -> str:
        bill = self.repo.find_by_id(bill_id)
        if bill is not None and bill.paid:
            self.logger.warning("Delete-bill rejected: bill %s is already paid.", bill_id)
            raise PermissionError("Cannot delete a bill that has already been paid.")
        self.repo.delete_by_id(bill_id)
        self.logger.info("Deleted bill %s.", bill_id)
        return "Bill deleted successfully."

    def update_bill(self, bill_id: int, updated: TenantBill) -> str:
        bill = self.repo.find_by_id(bill_id)
        if bill is None:
            self.logger.warning("Update-bill failed: bill %s not found.", bill_id)
            raise ValueError("Bill not found")
        if bill.paid:
            self.logger.warning("Update-bill rejected: bill %s is already paid.", bill_id)
            raise PermissionError("Cannot edit a bill that has already been paid.")
        bill.tenant_name = updated.tenant_name
        bill.month_year = updated.month_year
        bill.rent = updated.rent
        bill.water = updated.water
        bill.electricity = updated.electricity
        bill.miscellaneous = updated.miscellaneous
        self.repo.save(bill)
        self.logger.info("Updated bill %s for tenant %s (%s).", bill_id, bill.tenant_name, bill.month_year)
        return "Bill updated successfully."

    def get_paid_bills_for_month(self, month_year: str) -> List[TenantBill]:
        return self.repo.find_by_paid_true_and_month(month_year)


class ComplaintService:
    def __init__(self, repo: ComplaintRepository):
        self.repo = repo
        self.logger = logging.getLogger("app.services")

    def create_complaint(self, complaint: Complaint) -> Complaint:
        complaint.status = "OPEN"
        complaint.created_date = utc_now()
        complaint.closed_date = None
        complaint.resolution_comment = None
        saved = self.repo.save(complaint)
        self.logger.info("Created new complaint %s for tenant %s.", saved.id, saved.tenant_name)
        return saved

    def get_complaints_for_tenant(self, tenant_name: str) -> List[Complaint]:
        return self.repo.find_by_tenant_name_order_by_created_desc(tenant_name)

    def get_all_complaints(self) -> List[Complaint]:
        return self.repo.find_all_order_by_created_desc()

    def close_complaint(self, complaint_id: int, resolution_comment: Optional[str]) -> Complaint:
        complaint = self.repo.find_by_id(complaint_id)
        if complaint is None:
            self.logger.warning("Close-complaint failed: complaint %s not found.", complaint_id)
            raise ValueError("Complaint not found")
        if complaint.status == "CLOSED":
            self.logger.info("Complaint %s is already closed; no action taken.", complaint_id)
            return complaint
        complaint.status = "CLOSED"
        complaint.closed_date = utc_now()
        if resolution_comment is not None:
            complaint.resolution_comment = resolution_comment.strip()
        saved = self.repo.save(complaint)
        self.logger.info("Closed complaint %s for tenant %s.", complaint_id, saved.tenant_name)
        return saved

    def withdraw_complaint(self, complaint_id: int) -> Complaint:
        complaint = self.repo.find_by_id(complaint_id)
        if complaint is None:
            raise ValueError("Complaint not found")
        if complaint.status == "CLOSED":
            raise ValueError("This complaint is already closed")
        complaint.status = "CLOSED"
        complaint.closed_date = utc_now()
        complaint.resolution_comment = "Withdrawn by tenant"
        saved = self.repo.save(complaint)
        self.logger.info("Tenant %s withdrew complaint %s.", saved.tenant_name, complaint_id)
        return saved


class VacateService:
    """A tenant's move-out request. The vacate date is calculated once, at
    request time, from IST 'today' (not the server's own clock -- see the
    utc_now()+IST_OFFSET pattern used elsewhere for why that matters), and
    is never recalculated later.

    Lifecycle: PENDING (tenant requested) -> APPROVED (owner approved) ->
    SETTLED (owner recorded the deposit settlement after move-out). CANCELLED
    can happen from PENDING or APPROVED. Rent keeps being billed as normal for
    every month up to and including the move-out month -- this service never
    touches TenantBillService, deliberately, so there's no proration logic to
    get wrong."""

    IST_OFFSET = timedelta(hours=5, minutes=30)
    REFUND_METHODS = ("CASH", "BANK_TRANSFER", "UPI")

    def __init__(self, repo: VacateRequestRepository, deposit_repo: Optional[DepositRepository] = None, bill_repo: Optional[TenantBillRepository] = None):
        self.repo = repo
        self.deposit_repo = deposit_repo
        self.bill_repo = bill_repo
        self.logger = logging.getLogger("app.services")

    def _dto(self, r: VacateRequest) -> Dict[str, Any]:
        return {
            "id": r.id,
            "tenantUsername": r.tenant_username,
            "requestedDate": r.requested_date.isoformat(),
            "vacateDate": r.vacate_date.isoformat(),
            "status": r.status,
            "approvedDate": to_iso_utc(r.approved_date),
            # Only populated when a DepositRepository was given (the admin
            # listing) -- shown so the owner can see how much deposit is on
            # file for this tenant right where they decide a deduction,
            # instead of having to look it up on a different page.
            "depositTotal": self.deposit_repo.total_for_tenant(r.tenant_username) if self.deposit_repo else None,
            "settlement": {
                "deduction": r.settlement_deduction,
                "refundAmount": r.settlement_refund_amount,
                "refundMethod": r.settlement_refund_method,
                "note": r.settlement_note,
                "settledDate": to_iso_utc(r.settled_date),
                "tenantAcknowledged": bool(r.tenant_acknowledged),
                "tenantFeedback": r.tenant_feedback,
                "acknowledgedDate": to_iso_utc(r.acknowledged_date),
            } if r.status == "SETTLED" else None,
        }

    def preview_vacate_date(self) -> str:
        """What the date WOULD be if requested right now -- shown before the
        tenant confirms, computed fresh each time (not stored)."""
        today = (utc_now() + self.IST_OFFSET).date()
        return calculate_vacate_date(today).isoformat()

    def get_status(self, tenant_username: str) -> Optional[Dict[str, Any]]:
        """The tenant's current request -- open (PENDING/APPROVED) if there is
        one, otherwise their most recent SETTLED request so they can still see
        the final refund breakdown. A CANCELLED request is hidden once
        cancelled, so the tenant can freely request again."""
        latest = self.repo.find_latest_by_tenant(tenant_username)
        if latest is None or latest.status == "CANCELLED":
            return None
        return self._dto(latest)

    def request_vacate(self, tenant_username: str) -> Dict[str, Any]:
        if self.repo.find_open_by_tenant(tenant_username) is not None:
            raise ValueError("You already have an active vacate request.")
        if self.bill_repo is not None:
            unpaid = [b for b in self.bill_repo.find_by_tenant_name_order_by_month_desc(tenant_username) if not b.paid]
            if unpaid:
                raise PermissionError(f"You have {len(unpaid)} unpaid bill(s). Please pay them before requesting to vacate.")
        today = (utc_now() + self.IST_OFFSET).date()
        request = VacateRequest(
            tenant_username=tenant_username,
            requested_date=today,
            vacate_date=calculate_vacate_date(today),
            status="PENDING",
        )
        saved = self.repo.save(request)
        self.logger.info("Tenant %s requested to vacate; calculated date %s (pending owner approval).", tenant_username, saved.vacate_date)
        return self._dto(saved)

    def cancel_vacate(self, tenant_username: str) -> None:
        active = self.repo.find_open_by_tenant(tenant_username)
        if active is None:
            raise ValueError("You don't have an active vacate request.")
        if active.status != "PENDING":
            # Once the owner has approved it, they're already planning around
            # the move-out date -- the tenant can no longer back out unilaterally.
            raise PermissionError("This request has already been approved by the owner and can no longer be cancelled.")
        active.status = "CANCELLED"
        active.cancelled_date = utc_now()
        self.repo.save(active)
        self.logger.info("Tenant %s cancelled their vacate request (id=%s).", tenant_username, active.id)

    def acknowledge_settlement(self, tenant_username: str, feedback: Optional[str]) -> Dict[str, Any]:
        """The tenant confirms they actually received the refund, with
        optional feedback for the owner. Required before the owner can free
        up the username -- see TenantOffboardService.finalize_move_out."""
        latest = self.repo.find_latest_by_tenant(tenant_username)
        if latest is None or latest.status != "SETTLED":
            raise ValueError("There's no settled move-out for you to acknowledge.")
        if latest.tenant_acknowledged:
            raise ValueError("You've already acknowledged this settlement.")
        latest.tenant_acknowledged = True
        latest.tenant_feedback = (feedback or "").strip() or None
        latest.acknowledged_date = utc_now()
        saved = self.repo.save(latest)
        self.logger.info("Tenant %s acknowledged their settlement (request id=%s).", tenant_username, saved.id)
        return self._dto(saved)

    def list_all_open(self) -> List[Dict[str, Any]]:
        return [self._dto(r) for r in self.repo.find_all_open()]

    def list_all_settled(self) -> List[Dict[str, Any]]:
        """Settled requests still waiting on the owner to free up the
        username (see TenantOffboardService.finalize_move_out)."""
        return [self._dto(r) for r in self.repo.find_all_settled()]

    def approve_vacate(self, request_id: int) -> Dict[str, Any]:
        req = self.repo.find_by_id(request_id)
        if req is None or req.status != "PENDING":
            raise ValueError("No pending vacate request with that id.")
        req.status = "APPROVED"
        req.approved_date = utc_now()
        saved = self.repo.save(req)
        self.logger.info("Approved vacate request %s for tenant %s (move-out %s).", saved.id, saved.tenant_username, saved.vacate_date)
        return self._dto(saved)

    def settle_vacate(self, request_id: int, deduction: float, refund_method: str, note: Optional[str]) -> Dict[str, Any]:
        """Owner records the post-inspection settlement: damage deducted from
        the deposit, how the remainder was refunded, and an optional note.
        The refund amount is computed once here from the tenant's deposit
        ledger total and stored -- never recomputed -- so it can't drift if
        more deposit rows are added afterwards."""
        req = self.repo.find_by_id(request_id)
        if req is None or req.status != "APPROVED":
            raise ValueError("No approved vacate request with that id.")
        if deduction < 0:
            raise ValueError("Deduction cannot be negative")
        if refund_method not in self.REFUND_METHODS:
            raise ValueError("refundMethod must be one of: " + ", ".join(self.REFUND_METHODS))
        if self.deposit_repo is None:
            raise ValueError("Deposit records are unavailable")
        deposited = self.deposit_repo.total_for_tenant(req.tenant_username)
        if deduction > deposited:
            raise ValueError(f"Deduction cannot exceed the deposited amount (Rs. {deposited:.2f}).")

        req.status = "SETTLED"
        req.settlement_deduction = deduction
        req.settlement_refund_amount = deposited - deduction
        req.settlement_refund_method = refund_method
        req.settlement_note = (note or "").strip() or None
        req.settled_date = utc_now()
        saved = self.repo.save(req)
        self.logger.info(
            "Settled vacate request %s for tenant %s: deposited=%s deduction=%s refund=%s method=%s.",
            saved.id, saved.tenant_username, deposited, deduction, saved.settlement_refund_amount, refund_method,
        )
        return self._dto(saved)


def _archive_bill(b: TenantBill) -> Dict[str, Any]:
    return {
        "monthYear": b.month_year, "billType": b.bill_type, "rent": b.rent, "water": b.water,
        "electricity": b.electricity, "miscellaneous": b.miscellaneous, "paid": bool(b.paid),
        "paidDate": to_iso_utc(b.paid_date), "createdDate": b.created_date.isoformat() if b.created_date else None,
    }


def _archive_complaint(c: Complaint) -> Dict[str, Any]:
    return {
        "description": c.description, "status": c.status, "createdDate": to_iso_utc(c.created_date),
        "resolutionComment": c.resolution_comment, "closedDate": to_iso_utc(c.closed_date),
    }


def _archive_occupant(o: Occupant) -> Dict[str, Any]:
    return {
        "name": o.name, "aadharFileName": o.aadhar_file_name, "aadharStoragePath": o.aadhar_storage_path,
        "uploadedAt": to_iso_utc(o.uploaded_at), "verified": bool(o.verified), "verifiedBy": o.verified_by,
        "verifiedAt": to_iso_utc(o.verified_at),
    }


def _archive_deposit(d: DepositPayment) -> Dict[str, Any]:
    return {"amount": d.amount, "source": d.source, "paymentId": d.payment_id, "notes": d.notes, "paidDate": to_iso_utc(d.paid_date)}


def _archive_vacate_request(v: VacateRequest) -> Dict[str, Any]:
    return {
        "requestedDate": v.requested_date.isoformat(), "vacateDate": v.vacate_date.isoformat(), "status": v.status,
        "approvedDate": to_iso_utc(v.approved_date), "cancelledDate": to_iso_utc(v.cancelled_date),
        "settlementDeduction": v.settlement_deduction, "settlementRefundAmount": v.settlement_refund_amount,
        "settlementRefundMethod": v.settlement_refund_method, "settlementNote": v.settlement_note,
        "settledDate": to_iso_utc(v.settled_date),
        "tenantAcknowledged": bool(v.tenant_acknowledged), "tenantFeedback": v.tenant_feedback,
        "acknowledgedDate": to_iso_utc(v.acknowledged_date),
    }


class TenantOffboardService:
    """Frees a room's username up for a new tenant once their move-out has
    been settled: the outgoing tenant's whole history is snapshotted into
    ArchivedTenantRepository, the live tables are cleared for that username,
    and the User account is reset to accept a fresh registration -- protected
    by a brand-new registration key so the outgoing tenant (or anyone else who
    merely knows the username) can't register on it again."""

    def __init__(
        self,
        vacate_repo: VacateRequestRepository,
        bill_repo: TenantBillRepository,
        complaint_repo: ComplaintRepository,
        occupant_repo: OccupantRepository,
        deposit_repo: DepositRepository,
        archive_repo: ArchivedTenantRepository,
        user_repo: UserRepository,
    ):
        self.vacate_repo = vacate_repo
        self.bill_repo = bill_repo
        self.complaint_repo = complaint_repo
        self.occupant_repo = occupant_repo
        self.deposit_repo = deposit_repo
        self.archive_repo = archive_repo
        self.user_repo = user_repo
        self.logger = logging.getLogger("app.services")

    def finalize_move_out(self, request_id: int, admin_username: str) -> Dict[str, Any]:
        req = self.vacate_repo.find_by_id(request_id)
        if req is None or req.status != "SETTLED":
            raise ValueError("No settled vacate request with that id.")
        if not req.tenant_acknowledged:
            raise PermissionError("The tenant hasn't confirmed receiving their refund yet.")
        username = req.tenant_username

        # A username can't be handed to a new tenant while loose ends remain
        # on record for the old one -- an unresolved complaint or an
        # unverified occupant photo needs the owner's attention first, not to
        # quietly vanish into the archive.
        complaints = self.complaint_repo.find_by_tenant_name_order_by_created_desc(username)
        open_complaints = [c for c in complaints if c.status != "CLOSED"]
        if open_complaints:
            raise PermissionError(f"{len(open_complaints)} complaint(s) are still open. Close them before freeing up this username.")

        occupants = self.occupant_repo.find_by_tenant_username_order_by_uploaded_desc(username)
        unverified_occupants = [o for o in occupants if not o.verified]
        if unverified_occupants:
            raise PermissionError(f"{len(unverified_occupants)} occupant photo(s) are not yet verified. Verify them before freeing up this username.")

        # An unpaid bill is real money owed to the owner -- freeing up the
        # username must never make it disappear by silently marking it paid.
        # The tenant has to actually pay (or the owner marks it paid once
        # they have, the normal way) before the username can be freed.
        bills = self.bill_repo.find_by_tenant_name_order_by_month_desc(username)
        unpaid_bills = [b for b in bills if not b.paid]
        if unpaid_bills:
            raise PermissionError(f"{len(unpaid_bills)} bill(s) are still unpaid. The tenant must pay them before you can free up this username.")

        user = self.user_repo.find_by_username(username)
        if user is None:
            raise ValueError("Tenant account not found.")

        snapshot = {
            "fullName": user.full_name,
            "mail": user.mail,
            "phone": user.phone,
            "moveInDate": user.move_in_date.isoformat() if user.move_in_date else None,
            "demandedDeposit": user.demanded_deposit,
            "bills": [_archive_bill(b) for b in bills],
            "complaints": [_archive_complaint(c) for c in complaints],
            "occupants": [_archive_occupant(o) for o in occupants],
            "depositPayments": [_archive_deposit(d) for d in self.deposit_repo.find_by_tenant_order_by_date_desc(username)],
            "vacateRequests": [_archive_vacate_request(v) for v in self.vacate_repo.find_all_by_tenant(username)],
        }
        self.archive_repo.save(ArchivedTenant(username=username, archived_by=admin_username, data=json.dumps(snapshot)))

        self.bill_repo.delete_all_for_tenant(username)
        self.complaint_repo.delete_all_for_tenant(username)
        self.occupant_repo.delete_all_for_tenant(username)
        self.deposit_repo.delete_all_for_tenant(username)
        self.vacate_repo.delete_all_for_tenant(username)

        new_key = generate_registration_key()
        user.registration_completed = False
        user.mail = None
        user.phone = None
        user.full_name = None
        user.move_in_date = None
        user.demanded_deposit = None
        # Fresh unguessable password -- the outgoing tenant's old password
        # must not still work once the username is handed to someone new.
        user.password = generate_password_hash(secrets.token_urlsafe(32))
        # Invalidates any token from the outgoing tenant's last login.
        user.session_version = (user.session_version or 0) + 1
        user.registration_code = new_key
        self.user_repo.save(user)

        self.logger.info(
            "Offboarded tenant '%s' (archived vacate request %s); username freed for a new registration.",
            username, request_id,
        )
        return {"username": username, "registrationKey": new_key}


class OccupantService:
    def __init__(self, repo: OccupantRepository, user_repo: UserRepository):
        self.repo = repo
        self.user_repo = user_repo
        self.logger = logging.getLogger("app.services")

    def to_dto(self, occupant: Occupant) -> Dict[str, Any]:
        url = None
        if occupant.aadhar_storage_path:
            url = "/uploads/" + occupant.aadhar_storage_path.replace("\\", "/")
        return {
            "id": occupant.id,
            "tenantUsername": occupant.tenant_username,
            "name": occupant.name,
            "aadharFileName": occupant.aadhar_file_name,
            "aadharUrl": url,
            "uploadedAt": to_iso_utc(occupant.uploaded_at),
            "verified": occupant.verified,
        }

    def list(self, tenant_username: str) -> List[Dict[str, Any]]:
        return [self.to_dto(o) for o in self.repo.find_by_tenant_username_order_by_uploaded_desc(tenant_username)]

    def list_all(self) -> List[Dict[str, Any]]:
        return [self.to_dto(o) for o in self.repo.find_all_order_by_uploaded_desc()]

    def add(self, tenant_username: str, name: str, file: Any) -> Dict[str, Any]:
        if not name or not name.strip():
            raise ValueError("Name required")
        if file is None or file.filename == "":
            raise ValueError("File required")
        content_type = file.mimetype
        allowed = ["application/pdf", "image/jpeg", "image/png"]
        if content_type not in allowed:
            self.logger.warning("Occupant upload rejected for %s: invalid file type %s.", tenant_username, content_type)
            raise ValueError("Invalid file type")
        if file.content_length and file.content_length > 2 * 1024 * 1024:
            self.logger.warning("Occupant upload rejected for %s: file too large (%s bytes).", tenant_username, file.content_length)
            raise ValueError("File too large")

        user = self.user_repo.find_by_username(tenant_username)
        if user is None:
            self.logger.warning("Occupant upload rejected: tenant %s not found.", tenant_username)
            raise ValueError("Tenant not found")

        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name.strip())
        ext_map = {
            "image/jpeg": ".jpeg",
            "image/png": ".png",
            "application/pdf": ".pdf",
        }
        file_name = sanitized + ext_map.get(content_type, "")
        object_path = f"{tenant_username}/{file_name}"

        if _storage_configured():
            file_bytes = file.read()
            resp = requests.post(
                _storage_object_url(object_path),
                headers={**_storage_headers(), "Content-Type": content_type, "x-upsert": "true"},
                data=file_bytes,
                timeout=15,
            )
            if not resp.ok:
                self.logger.error("Supabase Storage upload failed for %s: %s %s", object_path, resp.status_code, resp.text)
                raise RuntimeError("Failed to store uploaded file")
        else:
            target_dir = UPLOADS_ROOT / "aadhaar" / tenant_username
            target_dir.mkdir(parents=True, exist_ok=True)
            file.save(target_dir / file_name)

        occupant = Occupant(
            tenant_username=tenant_username,
            name=name.strip(),
            aadhar_file_name=file.filename,
            aadhar_content_type=content_type,
            # Forward slashes always -- this is a URL/object-key path, not a
            # filesystem path, so it must not pick up Path's OS-dependent
            # separator (str(Path(...)) uses backslashes on Windows, which
            # silently broke both the Supabase object key and, on this
            # platform, any string-based prefix stripping of this field).
            aadhar_storage_path=f"aadhaar/{tenant_username}/{file_name}",
        )
        self.repo.save(occupant)
        self.logger.info("Added occupant '%s' for tenant %s (file=%s).", occupant.name, tenant_username, occupant.aadhar_file_name)
        return self.to_dto(occupant)

    def delete(self, occupant_id: int, ignore_verified: bool = False) -> None:
        occupant = self.repo.find_by_id(occupant_id)
        if occupant is None:
            self.logger.warning("Delete-occupant failed: occupant %s not found.", occupant_id)
            raise ValueError("Not found")
        if occupant.verified and not ignore_verified:
            self.logger.warning("Delete-occupant rejected: occupant %s ('%s') is verified.", occupant_id, occupant.name)
            raise ValueError("Cannot delete a verified occupant")
        if occupant.aadhar_storage_path:
            if _storage_configured():
                object_path = occupant.aadhar_storage_path.split("/", 1)[-1]
                resp = requests.delete(_storage_object_url(object_path), headers=_storage_headers(), timeout=15)
                if not resp.ok:
                    self.logger.warning("Supabase Storage delete failed for %s: %s %s", object_path, resp.status_code, resp.text)
            else:
                p = Path("uploads") / occupant.aadhar_storage_path
                if p.exists():
                    p.unlink(missing_ok=True)
        self.repo.delete(occupant)
        self.logger.info("Deleted occupant %s ('%s') for tenant %s.", occupant_id, occupant.name, occupant.tenant_username)

    def verify_occupant(self, occupant_id: int) -> Dict[str, Any]:
        occupant = self.repo.find_by_id(occupant_id)
        if occupant is None:
            self.logger.warning("Verify-occupant failed: occupant %s not found.", occupant_id)
            raise ValueError("Occupant not found")
        if not occupant.verified:
            occupant.verified = True
            occupant.verified_by = "OWNER"
            occupant.verified_at = utc_now()
            self.repo.save(occupant)
            self.logger.info("Verified occupant %s ('%s') for tenant %s.", occupant_id, occupant.name, occupant.tenant_username)
        else:
            self.logger.info("Occupant %s ('%s') is already verified; no action taken.", occupant_id, occupant.name)
        return {"status": "ok", "id": occupant.id, "verified": True}


class TransactionService:
    def __init__(self, repo: TransactionLogRepository):
        self.repo = repo
        self.logger = logging.getLogger("app.services")

    def log_success(self, tenant_name: str, payment_id: str) -> TransactionLog:
        log = TransactionLog(tenant_name=tenant_name, payment_id=payment_id, status="SUCCESS")
        saved = self.repo.save(log)
        self.logger.info("Recorded successful payment for tenant %s (payment_id=%s).", tenant_name, payment_id)
        return saved

    def log_failure(self, error_data: Dict[str, Any]) -> TransactionLog:
        payment_id = error_data.get("metadata.payment_id")
        log = TransactionLog(status="FAIL", payment_id=str(payment_id), error_reason=str(error_data))
        saved = self.repo.save(log)
        self.logger.warning("Recorded failed payment attempt (payment_id=%s): %s", payment_id, error_data)
        return saved


class EmailService:
    def __init__(self) -> None:
        self.logger = logging.getLogger("app.email")
        self.logger.setLevel(logging.DEBUG)
        if not self.logger.handlers:
            self.logger.propagate = True

    def _load_dotenv(self) -> Dict[str, str]:
        env_path = Path(__file__).resolve().parents[1] / ".env"
        values: Dict[str, str] = {}
        if not env_path.exists():
            return values

        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"\'')
        return values

    def _resend_settings(self) -> Dict[str, Any]:
        # Re-read fresh on every call (not cached) -- Render only re-injects
        # updated dashboard env vars on a real deploy, not a plain restart, so
        # this must never memoize a stale value across a restart-only cycle.
        env_values = self._load_dotenv()
        return {
            "api_key": os.getenv("RESEND_API_KEY", env_values.get("RESEND_API_KEY")),
            "sender": os.getenv("MAIL_SENDER", env_values.get("MAIL_SENDER")) or os.getenv("MAIL_FROM") or "onboarding@resend.dev",
        }

    def _send_email(self, recipient: str, subject: str, body: str, html_body: Optional[str] = None) -> None:
        if not recipient:
            self.logger.warning("Skipping email send: no recipient address provided. subject=%s", subject)
            return
        settings = self._resend_settings()
        if not settings["api_key"]:
            self.logger.error("Cannot send email to %s: RESEND_API_KEY is not configured. subject=%s", recipient, subject)
            raise RuntimeError("RESEND_API_KEY is not configured")

        payload = {
            "from": settings["sender"],
            "to": [recipient],
            "subject": subject,
            "text": body,
        }
        if html_body:
            payload["html"] = html_body

        self.logger.info("Sending email to %s via Resend. subject=%s sender=%s", recipient, subject, settings["sender"])
        try:
            response = requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings['api_key']}"},
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
        except Exception:  # pragma: no cover - network-dependent path
            self.logger.exception("Mail not sent to %s via Resend. subject=%s sender=%s", recipient, subject, settings["sender"])
            raise
        else:
            self.logger.info("Email sent successfully to %s. subject=%s", recipient, subject)

    def _invoice_total(self, bill: TenantBill) -> float:
        rent = bill.rent or 0
        water = bill.water or 0
        electricity = bill.electricity or 0
        miscellaneous = bill.miscellaneous or 0
        return rent + water + electricity + miscellaneous

    def _brand_logo(self) -> str:
        return """
        <svg width="420" height="210" viewBox="0 0 420 210" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="VGR logo">
          <g fill="none" fill-rule="evenodd">
            <g transform="translate(42,16)">
              <circle cx="160" cy="90" r="92" stroke="#3e2a1d" stroke-width="12" fill="none"/>
              <g stroke="#3e2a1d" stroke-linecap="round" stroke-width="12">
                <path d="M25 90H15M295 90H305M160 2V-8M160 178V188M66 35L58 27M262 153L270 161M66 145L58 153M262 27L270 19"/>
              </g>
              <path d="M147 60c28 2 45 18 45 42 0 31-27 54-58 54-26 0-45-15-54-40 10 15 27 25 46 25 20 0 38-11 48-29 8-15 10-30 7-52h-34z" fill="#3e2a1d"/>
              <path d="M147 80c30 1 47 15 47 38 0 25-21 44-48 44-18 0-32-8-42-22 9 10 20 16 34 16 23 0 41-15 44-38 2-15-2-30-17-38h-18z" fill="#3e2a1d" opacity="0.9"/>
              <path d="M128 71c26 5 44 22 49 49-9 5-18 9-29 10-23 2-42-7-55-28 4-16 16-31 35-31z" fill="#3e2a1d"/>
            </g>
          </g>
        </svg>
        """

    def _bill_template(self, bill: TenantBill, tenant_name: str, title: str, message: str, footer: str) -> str:
        total = self._invoice_total(bill)
        misc_row = ""
        if bill.miscellaneous:
            misc_row = f"""
                <tr>
                  <td style="padding: 10px 14px; border-bottom: 1px solid #e5e7eb;"><strong>Miscellaneous</strong></td>
                  <td style="padding: 10px 14px; border-bottom: 1px solid #e5e7eb; text-align: right;">₹{bill.miscellaneous:.2f}</td>
                </tr>"""
        # An electricity-only bill has no rent/water, so those rows would
        # just show a confusing "₹0.00" -- only show what this bill type
        # actually charges for.
        rent_water_rows = "" if bill.bill_type == "ELECTRICITY" else f"""
                <tr>
                  <td style="padding: 10px 14px; border-bottom: 1px solid #e5e7eb;"><strong>Rent</strong></td>
                  <td style="padding: 10px 14px; border-bottom: 1px solid #e5e7eb; text-align: right;">₹{bill.rent or 0:.2f}</td>
                </tr>
                <tr>
                  <td style="padding: 10px 14px; border-bottom: 1px solid #e5e7eb;"><strong>Water</strong></td>
                  <td style="padding: 10px 14px; border-bottom: 1px solid #e5e7eb; text-align: right;">₹{bill.water or 0:.2f}</td>
                </tr>"""
        electricity_row = f"""
                <tr>
                  <td style="padding: 10px 14px; border-bottom: 1px solid #e5e7eb;"><strong>Electricity</strong></td>
                  <td style="padding: 10px 14px; border-bottom: 1px solid #e5e7eb; text-align: right;">₹{bill.electricity or 0:.2f}</td>
                </tr>""" if bill.bill_type == "ELECTRICITY" or bill.electricity else ""
        return f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #1f2937; line-height: 1.6; background-color: #efe8dd; padding: 24px; margin: 0;">
            <div style="max-width: 760px; margin: 0 auto; background: #efe8dd; border: 3px solid #3e2a1d; padding: 28px 24px 24px;">
              <div style="text-align: center; margin-bottom: 12px;">{self._brand_logo()}</div>
              <div style="font-size: 120px; font-weight: 900; letter-spacing: -8px; line-height: 0.9; text-align: center; color: #3e2a1d; margin: 0 0 28px;">VGR</div>

              <h2 style="margin: 0 0 12px; color: #111827; font-size: 24px;">{title}</h2>
              <p style="margin: 0 0 20px; font-size: 15px;">Hello {tenant_name},</p>
              <p style="margin: 0 0 20px; font-size: 15px;">{message}</p>

              <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px; background: #ffffff; border: 1px solid #e5e7eb;">
                <tr>
                  <td style="padding: 10px 14px; border-bottom: 1px solid #e5e7eb;"><strong>Tenant</strong></td>
                  <td style="padding: 10px 14px; border-bottom: 1px solid #e5e7eb; text-align: right;">{bill.tenant_name}</td>
                </tr>
                <tr>
                  <td style="padding: 10px 14px; border-bottom: 1px solid #e5e7eb;"><strong>Billing Month</strong></td>
                  <td style="padding: 10px 14px; border-bottom: 1px solid #e5e7eb; text-align: right;">{bill.month_year}</td>
                </tr>
                {rent_water_rows}{electricity_row}{misc_row}
                <tr>
                  <td style="padding: 12px 14px; font-size: 18px;"><strong>Total Due</strong></td>
                  <td style="padding: 12px 14px; font-size: 18px; text-align: right;"><strong>₹{total:.2f}</strong></td>
                </tr>
              </table>

              <p style="margin: 0; color: #374151; font-size: 15px;">{footer}</p>
            </div>
          </body>
        </html>
        """

    def send_otp_email(self, recipient: str, otp: str) -> None:
        subject = "Your OTP code"
        body = f"Your OTP is {otp}. It will expire in 5 minutes.\n\nIf you did not request this, you can ignore this email."
        self._send_email(recipient, subject, body)

    def notify_bill_generated(self, bill: TenantBill, tenant_email: Optional[str], month: str) -> None:
        if tenant_email:
            label = "Electricity" if bill.bill_type == "ELECTRICITY" else "Rent"
            subject = f"{label} bill generated for {month}"
            body = (
                f"Hello,\n\nYour {label.lower()} bill for {month} has been generated.\n"
                f"Amount due: ₹{self._invoice_total(bill):.2f}\n\nThank you."
            )
            html_body = self._bill_template(
                bill=bill,
                tenant_name=bill.tenant_name or "Tenant",
                title=f"{label} Invoice Generated",
                message=f"Your {label.lower()} invoice for {month} has been generated and is ready for payment.",
                footer="Please pay the total amount before the due date. Thank you.",
            )
            self._send_email(tenant_email, subject, body, html_body)

    def send_bill_paid_email(self, bill: TenantBill, tenant_email: str, admin_email: str) -> None:
        subject = f"Payment received for {bill.month_year}"
        body = (
            f"Hello,\n\nYour payment for {bill.month_year} has been received successfully.\n"
            f"Paid amount: ₹{self._invoice_total(bill):.2f}\n\nThank you."
        )
        html_body = self._bill_template(
            bill=bill,
            tenant_name=bill.tenant_name or "Tenant",
            title="Payment Received",
            message=f"Your payment for {bill.month_year} has been received successfully.",
            footer="Thank you for your payment. This invoice is now marked as paid.",
        )
        self._send_email(tenant_email, subject, body, html_body)
        if admin_email and admin_email != tenant_email:
            self._send_email(admin_email, subject, body, html_body)
