from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.types import TypeDecorator

from app.database import Base



def utc_now() -> datetime:
    """Current UTC time as a naive datetime -- the same value the deprecated
    datetime.utcnow() returned, which is what every DateTime column here stores."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BitBoolean(TypeDecorator):
    """This schema's boolean-like columns (registration_completed, paid, verified) are
    MySQL BIT(1), left over from the original Java/Hibernate backend. PyMySQL returns
    BIT(1) values as raw bytes (b'\\x00'/b'\\x01'), and Python's bool(b'\\x00') is True
    since it checks length, not content -- so plain sqlalchemy.Boolean silently reads
    every false value as true. impl=Integer (not Boolean) is deliberate: Integer's own
    result processor is a no-op, so the raw bytes reach process_result_value below
    unmangled; Boolean's processor would otherwise run first and already turn b'\\x00'
    into True before we ever see it."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return 1 if value else 0

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray)):
            return value != b"\x00"
        return bool(value)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), unique=True, nullable=False)
    password = Column(String(255), nullable=False)
    full_name = Column(String(100), nullable=True)
    phone = Column(String(50), nullable=True)
    mail = Column(String(255), nullable=True)
    role = Column(String(50), nullable=True)
    registration_completed = Column(BitBoolean, default=False, nullable=False)
    move_in_date = Column(Date, nullable=True)
    demanded_deposit = Column(Float, nullable=True)


class TenantBill(Base):
    __tablename__ = "tenant_bills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_name = Column(String(255), nullable=True)
    month_year = Column(String(20), nullable=True)
    # "RENT" (rent+water+miscellaneous) or "ELECTRICITY" (electricity only).
    # Split out because electricity bills consistently arrive a week or more
    # after rent is due (~10th vs ~17th-18th) -- forcing them into one bill
    # meant rent couldn't be raised until the electricity figure was known.
    # The amount fields and their payment logic are unchanged either way;
    # whichever fields are zero/null for a given bill_type just contribute
    # nothing to the total.
    bill_type = Column(String(20), default="RENT", nullable=False)
    rent = Column(Float, nullable=True)
    water = Column(Float, nullable=True)
    electricity = Column(Float, nullable=True)
    miscellaneous = Column(Float, nullable=True)
    paid = Column(BitBoolean, default=False, nullable=False)
    paid_date = Column(DateTime, nullable=True)
    created_date = Column(Date, nullable=True)


class Complaint(Base):
    __tablename__ = "complaints"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_name = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    status = Column(String(50), nullable=True)
    created_date = Column(DateTime, default=utc_now, nullable=False)
    resolution_comment = Column(Text, nullable=True)
    closed_date = Column(DateTime, nullable=True)


class Occupant(Base):
    __tablename__ = "occupants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_username = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    aadhar_file_name = Column(String(255), nullable=True)
    aadhar_content_type = Column(String(255), nullable=True)
    aadhar_storage_path = Column(String(500), nullable=True)
    uploaded_at = Column(DateTime, default=utc_now, nullable=False)
    verified = Column(BitBoolean, default=False, nullable=False)
    verified_by = Column(String(255), nullable=True)
    verified_at = Column(DateTime, nullable=True)


class DepositPayment(Base):
    """One entry in a tenant's security-deposit ledger. The tenant's total
    deposit is always the SUM of these rows for their username, never a
    single mutable field -- so it can't drift out of sync between what an
    admin sets and what the tenant has actually paid. 'source' distinguishes
    a real Razorpay payment from an admin manually recording an earlier
    cash/offline deposit."""

    __tablename__ = "deposit_payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_username = Column(String(255), nullable=False)
    amount = Column(Float, nullable=False)
    source = Column(String(20), nullable=False)  # "razorpay" or "manual"
    payment_id = Column(String(255), nullable=True)
    notes = Column(String(500), nullable=True)
    paid_date = Column(DateTime, default=utc_now, nullable=False)


class TransactionLog(Base):
    __tablename__ = "transaction_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_name = Column(String(255), nullable=True)
    payment_id = Column(String(255), nullable=True)
    status = Column(String(50), nullable=True)
    error_reason = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=utc_now, nullable=False)
