from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import ArchivedTenant, Complaint, DepositPayment, Occupant, TenantBill, TransactionLog, User, VacateRequest


class UserRepository:
    def __init__(self, db: Session):
        self.db = db

    def find_by_username(self, username: str) -> Optional[User]:
        return self.db.query(User).filter(User.username == username).first()

    def find_by_id(self, user_id: int) -> Optional[User]:
        return self.db.query(User).filter(User.id == user_id).first()

    def find_all(self) -> List[User]:
        return self.db.query(User).all()

    def save(self, user: User) -> User:
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def delete_by_id(self, user_id: int) -> None:
        user = self.find_by_id(user_id)
        if user is not None:
            self.db.delete(user)
            self.db.commit()

    def exists_by_id(self, user_id: int) -> bool:
        return self.find_by_id(user_id) is not None

    def find_tenant_usernames(self) -> List[str]:
        return [u.username for u in self.db.query(User).filter(User.role == "TENANT").all()]


class TenantBillRepository:
    def __init__(self, db: Session):
        self.db = db

    def find_by_tenant_name_order_by_month_desc(self, tenant_name: str) -> List[TenantBill]:
        return self.db.query(TenantBill).filter(TenantBill.tenant_name == tenant_name).order_by(TenantBill.month_year.desc()).all()

    def find_by_tenant_name_and_month(self, tenant_name: str, month_year: str, bill_type: str = "RENT") -> Optional[TenantBill]:
        return self.db.query(TenantBill).filter(
            TenantBill.tenant_name == tenant_name,
            TenantBill.month_year == month_year,
            TenantBill.bill_type == bill_type,
        ).first()

    def find_by_paid_true_and_month(self, month_year: str) -> List[TenantBill]:
        return self.db.query(TenantBill).filter(TenantBill.paid == True, TenantBill.month_year == month_year).all()  # noqa: E712 (paid is BitBoolean/Integer-backed; == compiles to "= 1", .is_() emits "IS 1" which MySQL rejects)

    def find_all(self) -> List[TenantBill]:
        return self.db.query(TenantBill).order_by(TenantBill.month_year.desc()).all()

    def find_by_id(self, bill_id: int) -> Optional[TenantBill]:
        return self.db.query(TenantBill).filter(TenantBill.id == bill_id).first()

    def save(self, bill: TenantBill) -> TenantBill:
        self.db.add(bill)
        self.db.commit()
        self.db.refresh(bill)
        return bill

    def delete_by_id(self, bill_id: int) -> None:
        bill = self.find_by_id(bill_id)
        if bill is not None:
            self.db.delete(bill)
            self.db.commit()

    def delete_all_for_tenant(self, tenant_name: str) -> None:
        self.db.query(TenantBill).filter(TenantBill.tenant_name == tenant_name).delete()
        self.db.commit()


class ComplaintRepository:
    def __init__(self, db: Session):
        self.db = db

    def save(self, complaint: Complaint) -> Complaint:
        self.db.add(complaint)
        self.db.commit()
        self.db.refresh(complaint)
        return complaint

    def find_by_tenant_name_order_by_created_desc(self, tenant_name: str) -> List[Complaint]:
        return self.db.query(Complaint).filter(Complaint.tenant_name == tenant_name).order_by(Complaint.created_date.desc()).all()

    def find_all_order_by_created_desc(self) -> List[Complaint]:
        return self.db.query(Complaint).order_by(Complaint.created_date.desc()).all()

    def find_by_id(self, complaint_id: int) -> Optional[Complaint]:
        return self.db.query(Complaint).filter(Complaint.id == complaint_id).first()

    def delete_all_for_tenant(self, tenant_name: str) -> None:
        self.db.query(Complaint).filter(Complaint.tenant_name == tenant_name).delete()
        self.db.commit()


class TransactionLogRepository:
    def __init__(self, db: Session):
        self.db = db

    def save(self, log: TransactionLog) -> TransactionLog:
        self.db.add(log)
        self.db.commit()
        self.db.refresh(log)
        return log


class OccupantRepository:
    def __init__(self, db: Session):
        self.db = db

    def find_by_tenant_username_order_by_uploaded_desc(self, tenant_username: str) -> List[Occupant]:
        return self.db.query(Occupant).filter(Occupant.tenant_username == tenant_username).order_by(Occupant.uploaded_at.desc()).all()

    def find_all_order_by_uploaded_desc(self) -> List[Occupant]:
        return self.db.query(Occupant).order_by(Occupant.uploaded_at.desc()).all()

    def find_by_id(self, occupant_id: int) -> Optional[Occupant]:
        return self.db.query(Occupant).filter(Occupant.id == occupant_id).first()

    def save(self, occupant: Occupant) -> Occupant:
        self.db.add(occupant)
        self.db.commit()
        self.db.refresh(occupant)
        return occupant

    def delete(self, occupant: Occupant) -> None:
        self.db.delete(occupant)
        self.db.commit()

    def delete_all_for_tenant(self, tenant_username: str) -> None:
        self.db.query(Occupant).filter(Occupant.tenant_username == tenant_username).delete()
        self.db.commit()


class DepositRepository:
    def __init__(self, db: Session):
        self.db = db

    def find_by_tenant_order_by_date_desc(self, tenant_username: str) -> List[DepositPayment]:
        return self.db.query(DepositPayment).filter(DepositPayment.tenant_username == tenant_username).order_by(DepositPayment.paid_date.desc()).all()

    def total_for_tenant(self, tenant_username: str) -> float:
        total = self.db.query(func.sum(DepositPayment.amount)).filter(DepositPayment.tenant_username == tenant_username).scalar()
        return float(total) if total is not None else 0.0

    def totals_by_tenant(self) -> dict:
        """One GROUP BY query for every tenant's total, instead of N+1 -- used
        by the admin user list, which needs every row's total at once."""
        rows = self.db.query(DepositPayment.tenant_username, func.sum(DepositPayment.amount)).group_by(DepositPayment.tenant_username).all()
        return {username: float(total) for username, total in rows}

    def save(self, payment: DepositPayment) -> DepositPayment:
        self.db.add(payment)
        self.db.commit()
        self.db.refresh(payment)
        return payment

    def delete_all_for_tenant(self, tenant_username: str) -> None:
        self.db.query(DepositPayment).filter(DepositPayment.tenant_username == tenant_username).delete()
        self.db.commit()


class VacateRequestRepository:
    OPEN_STATUSES = ("PENDING", "APPROVED")

    def __init__(self, db: Session):
        self.db = db

    def find_open_by_tenant(self, tenant_username: str) -> Optional[VacateRequest]:
        """The tenant's in-progress request (PENDING or APPROVED) -- there can
        only ever be one at a time; a CANCELLED or SETTLED one doesn't block
        a fresh request."""
        return (
            self.db.query(VacateRequest)
            .filter(VacateRequest.tenant_username == tenant_username, VacateRequest.status.in_(self.OPEN_STATUSES))
            .order_by(VacateRequest.id.desc())
            .first()
        )

    def find_latest_by_tenant(self, tenant_username: str) -> Optional[VacateRequest]:
        """The most recent request regardless of status, so a tenant can still
        see the outcome (approved / settled) after it's no longer 'open'."""
        return (
            self.db.query(VacateRequest)
            .filter(VacateRequest.tenant_username == tenant_username)
            .order_by(VacateRequest.id.desc())
            .first()
        )

    def find_all_open(self) -> List[VacateRequest]:
        return (
            self.db.query(VacateRequest)
            .filter(VacateRequest.status.in_(self.OPEN_STATUSES))
            .order_by(VacateRequest.vacate_date.asc())
            .all()
        )

    def find_all_settled(self) -> List[VacateRequest]:
        """Settled but not yet finalized (finalizing deletes the row) -- these
        are the ones still waiting on the owner to free up the username."""
        return (
            self.db.query(VacateRequest)
            .filter(VacateRequest.status == "SETTLED")
            .order_by(VacateRequest.settled_date.asc())
            .all()
        )

    def find_all_by_tenant(self, tenant_username: str) -> List[VacateRequest]:
        return (
            self.db.query(VacateRequest)
            .filter(VacateRequest.tenant_username == tenant_username)
            .order_by(VacateRequest.id.asc())
            .all()
        )

    def find_by_id(self, request_id: int) -> Optional[VacateRequest]:
        return self.db.query(VacateRequest).filter(VacateRequest.id == request_id).first()

    def save(self, request: VacateRequest) -> VacateRequest:
        self.db.add(request)
        self.db.commit()
        self.db.refresh(request)
        return request

    def delete_all_for_tenant(self, tenant_username: str) -> None:
        self.db.query(VacateRequest).filter(VacateRequest.tenant_username == tenant_username).delete()
        self.db.commit()


class ArchivedTenantRepository:
    def __init__(self, db: Session):
        self.db = db

    def save(self, record: ArchivedTenant) -> ArchivedTenant:
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def find_all(self) -> List[ArchivedTenant]:
        return self.db.query(ArchivedTenant).order_by(ArchivedTenant.archived_date.desc()).all()
