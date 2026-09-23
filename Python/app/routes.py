from __future__ import annotations

import json
import logging
import re
import secrets
import mimetypes
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Flask, Response, g, jsonify, request
from werkzeug.security import generate_password_hash

from app.assistant import AssistantService
from app.auth import generate_token, require_auth, require_role, require_self_or_admin
from app.bulk_bills import BulkError, MAX_BYTES, build_template, check_kind, import_planned, plan_rows, public_rows, read_rows, summarize
from app.database import get_db
from app.debug_trace import trace_request
from app.models import Complaint, Occupant, TenantBill, User
from app.payments import PaymentService
from app.repositories import ArchivedTenantRepository, ComplaintRepository, DepositRepository, OccupantRepository, TenantBillRepository, TransactionLogRepository, UserRepository, VacateRequestRepository
from app.services import ComplaintService, DepositService, EmailService, LoginLockedError, LoginThrottle, OccupantService, OTPCooldownError, OTPService, TenantBillService, TenantOffboardService, TransactionService, UserService, VacateService, fetch_uploaded_file, generate_registration_key, to_iso_utc, verify_password

logger = logging.getLogger("app.auth")


def _normalize_phone(value):
    """Return a bare 10-digit Indian mobile number, or None if `value` isn't one.
    Accepts spaces/dashes and an optional +91 / 91 / 0 prefix."""
    digits = re.sub(r"[\s\-()]", "", str(value or ""))
    digits = re.sub(r"^(\+91|91|0)(?=\d{10}$)", "", digits)
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def _parse_optional_float(value: Any) -> Optional[float]:
    """Bill amount fields are nullable Float columns, but a form that hides a
    field for the current bill type (e.g. no Electricity input on a Rent
    bill) still submits it as "" rather than omitting it -- Postgres rejects
    an empty string for a numeric column outright, so this must normalize
    "" (and None) to None before it ever reaches the model."""
    if value is None or value == "":
        return None
    return float(value)


def register_routes(app: Flask) -> None:
    email_service = EmailService()
    otp_service = OTPService(email_service)
    started_at = datetime.now(timezone.utc).isoformat()

    @app.route("/api/version", methods=["GET"])
    def get_version():
        # RENDER_GIT_COMMIT/RENDER_GIT_BRANCH are auto-injected by Render on
        # every deploy -- no manual version bump needed, this always reflects
        # exactly what commit is actually running.
        commit = os.getenv("RENDER_GIT_COMMIT", "local-dev")
        return jsonify({
            "commit": commit[:7] if commit != "local-dev" else commit,
            "branch": os.getenv("RENDER_GIT_BRANCH", "unknown"),
            "startedAt": started_at,
        }), 200

    @app.route("/api/auth/login", methods=["POST"])
    def auth_login():
        data = request.get_json(silent=True) or {}
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            return jsonify({"error": "Invalid credentials"}), 401

        try:
            LoginThrottle.check(username)
        except LoginLockedError as exc:
            return jsonify({"error": str(exc), "retryAfterSeconds": exc.retry_after_seconds}), 429

        db = get_db()
        repo = UserRepository(db)
        user = repo.find_by_username(username)
        if not user:
            logger.warning("Login failed for '%s': user not found.", username)
            LoginThrottle.record_failure(username)
            return jsonify({"error": "Invalid credentials"}), 401
        if not user.registration_completed:
            logger.warning("Login failed for '%s': registration incomplete.", username)
            return jsonify({"error": "Registration incomplete"}), 403
        if not verify_password(user.password, password):
            logger.warning("Login failed for '%s': incorrect password.", username)
            LoginThrottle.record_failure(username)
            return jsonify({"error": "Invalid credentials"}), 401

        LoginThrottle.record_success(username)
        # Invalidates any token from an earlier login -- one signed-in device per account.
        user.session_version = (user.session_version or 0) + 1
        repo.save(user)
        logger.info("User '%s' logged in successfully (role=%s).", username, user.role)
        token = generate_token(user.username, user.role, user.session_version)
        return jsonify({"role": user.role, "username": username, "token": token}), 200

    @app.route("/api/users/add", methods=["POST"])
    @require_role("ADMIN")
    def add_user():
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        role = (data.get("role") or "TENANT").upper()
        if not username:
            return jsonify({"error": "username required"}), 400
        if role not in ("ADMIN", "TENANT"):
            return jsonify({"error": "role must be ADMIN or TENANT"}), 400

        db = get_db()
        repo = UserRepository(db)
        if repo.find_by_username(username):
            logger.warning("Add-user rejected: username '%s' already exists.", username)
            return jsonify({"error": "User already exists"}), 409

        # The tenant chooses their own password when they register. Until then the
        # account holds a random one nobody knows, and login is blocked anyway
        # because registration_completed is False.
        password = data.get("password") or secrets.token_urlsafe(32)
        # Tenant accounts need a registration key to start registration (see
        # start_registration) -- admins skip straight to registered, so they
        # never need one.
        registration_code = generate_registration_key() if role == "TENANT" else None
        user = User(username=username, password=generate_password_hash(password), role=role, registration_completed=False, registration_code=registration_code)
        saved = repo.save(user)
        logger.info("Admin created new user account '%s' (role=%s).", saved.username, saved.role)
        return jsonify({
            "id": saved.id,
            "username": saved.username,
            "phone": saved.phone,
            "mail": saved.mail,
            "role": saved.role,
            "registrationCompleted": saved.registration_completed,
            "registrationKey": saved.registration_code,
        }), 201

    @app.route("/api/users/signup", methods=["POST"])
    def signup():
        data = request.get_json(silent=True) or {}
        email = data.get("email")
        password = data.get("password")
        if not email or not password:
            return jsonify({"error": "email,password required"}), 400

        db = get_db()
        repo = UserRepository(db)
        user_service = UserService(repo)
        user = user_service.register_user(email, password)
        return jsonify({"id": user.id, "email": user.mail}), 201

    @app.route("/api/users/registration/start", methods=["POST"])
    def start_registration():
        data = request.get_json(silent=True) or {}
        username = data.get("username")
        email = data.get("email")
        if not username or not email:
            return jsonify({"error": "username,email required"}), 400
        phone = _normalize_phone(data.get("phone"))
        if phone is None:
            return jsonify({"error": "Enter a valid 10-digit mobile number"}), 400
        full_name = " ".join(str(data.get("fullName") or "").split())
        if len(full_name) < 2 or len(full_name) > 100:
            return jsonify({"error": "Enter your full name"}), 400

        db = get_db()
        repo = UserRepository(db)
        user = repo.find_by_username(username)
        if user is None:
            logger.warning("Registration start failed: username '%s' not found.", username)
            return jsonify({"error": "Username not found"}), 404
        if user.registration_completed:
            logger.warning("Registration start rejected for '%s': already completed.", username)
            return jsonify({"error": "Registration already completed"}), 409
        # None means this account predates the registration-key feature -- allow it
        # through rather than locking out a registration that was already pending.
        if user.registration_code is not None:
            provided = str(data.get("registrationKey") or "").strip().upper()
            if provided != user.registration_code:
                logger.warning("Registration start rejected for '%s': wrong or missing registration key.", username)
                return jsonify({"error": "Invalid registration key. Please check with the property owner."}), 403

        user.mail = email.strip()
        user.phone = phone
        user.full_name = full_name
        repo.save(user)
        try:
            otp_service.generate_otp(user.mail)
        except OTPCooldownError as exc:
            return jsonify({"error": str(exc), "retryAfterSeconds": exc.retry_after_seconds}), 429
        except Exception:
            logger.exception("Registration start failed: could not send OTP email to %s.", user.mail)
            return jsonify({"error": "Could not send OTP email. Please try again shortly."}), 502
        logger.info("Registration started for '%s'; OTP sent to %s.", username, user.mail)
        return jsonify({"message": "OTP sent to email"}), 200

    @app.route("/api/users/registration/finish", methods=["POST"])
    def finish_registration():
        data = request.get_json(silent=True) or {}
        username = data.get("username")
        otp = data.get("otp")
        password = data.get("password")
        if not username or not otp or not password:
            return jsonify({"error": "username,otp,password required"}), 400

        db = get_db()
        repo = UserRepository(db)
        user = repo.find_by_username(username)
        if user is None:
            logger.warning("Registration finish failed: username '%s' not found.", username)
            return jsonify({"error": "Username not found"}), 404
        if user.registration_completed:
            logger.warning("Registration finish rejected for '%s': already completed.", username)
            return jsonify({"error": "Already completed"}), 409
        if not user.mail:
            logger.warning("Registration finish rejected for '%s': registration was never started.", username)
            return jsonify({"error": "Start registration first"}), 400
        if not otp_service.verify_otp(user.mail, otp):
            return jsonify({"error": "Invalid OTP"}), 401

        user.password = generate_password_hash(password)
        user.registration_completed = True
        repo.save(user)
        logger.info("Registration completed for '%s'.", username)
        return jsonify({"message": "Registration completed"}), 200

    @app.route("/api/users/login", methods=["POST"])
    def login_user():
        data = request.get_json(silent=True) or {}
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            return jsonify({"error": "username,password required"}), 400

        try:
            LoginThrottle.check(username)
        except LoginLockedError as exc:
            return jsonify({"error": str(exc), "retryAfterSeconds": exc.retry_after_seconds}), 429

        db = get_db()
        repo = UserRepository(db)
        user = repo.find_by_username(username)
        if user is None:
            logger.warning("Login failed for '%s': user not found.", username)
            LoginThrottle.record_failure(username)
            return jsonify({"error": "Invalid credentials"}), 401
        if not user.registration_completed:
            logger.warning("Login failed for '%s': registration incomplete.", username)
            return jsonify({"error": "Registration incomplete"}), 403
        if not verify_password(user.password, password):
            logger.warning("Login failed for '%s': incorrect password.", username)
            LoginThrottle.record_failure(username)
            return jsonify({"error": "Invalid credentials"}), 401

        LoginThrottle.record_success(username)
        # Invalidates any token from an earlier login -- one signed-in device per account.
        user.session_version = (user.session_version or 0) + 1
        repo.save(user)
        logger.info("User '%s' logged in successfully (role=%s).", username, user.role)
        token = generate_token(user.username, user.role, user.session_version)
        return jsonify({"username": user.username, "fullName": user.full_name, "role": user.role, "token": token}), 200

    @app.route("/api/users/forgot-password", methods=["POST"])
    def forgot_password():
        data = request.get_json(silent=True) or {}
        username = data.get("username")
        if not username:
            return jsonify({"error": "username required"}), 400

        db = get_db()
        repo = UserRepository(db)
        user = repo.find_by_username(username)
        if user is None:
            logger.warning("Forgot-password failed: username '%s' not found.", username)
            return jsonify({"error": "User not found"}), 404
        if not user.mail:
            logger.warning("Forgot-password rejected for '%s': no email on file.", username)
            return jsonify({"error": "No email registered"}), 400

        try:
            otp_service.generate_otp(user.mail)
        except OTPCooldownError as exc:
            return jsonify({"error": str(exc), "retryAfterSeconds": exc.retry_after_seconds}), 429
        except Exception:
            logger.exception("Forgot-password failed: could not send OTP email to %s.", user.mail)
            return jsonify({"error": "Could not send OTP email. Please try again shortly."}), 502
        logger.info("Password-reset OTP sent to %s for user '%s'.", user.mail, username)
        return jsonify({"message": "OTP sent"}), 200

    @app.route("/api/users/reset-password", methods=["POST"])
    def reset_password():
        data = request.get_json(silent=True) or {}
        username = data.get("username")
        otp = data.get("otp")
        new_password = data.get("newPassword")
        if not username or not otp or not new_password:
            return jsonify({"error": "username,otp,newPassword required"}), 400

        db = get_db()
        repo = UserRepository(db)
        user = repo.find_by_username(username)
        if user is None:
            logger.warning("Reset-password failed: username '%s' not found.", username)
            return jsonify({"error": "User not found"}), 404
        if not user.mail or not otp_service.verify_otp(user.mail, otp):
            return jsonify({"error": "Invalid OTP"}), 401

        user.password = generate_password_hash(new_password)
        repo.save(user)
        logger.info("Password reset completed for '%s'.", username)
        return jsonify({"message": "Password updated"}), 200

    @app.route("/api/users/all", methods=["GET"])
    @require_role("ADMIN")
    def get_all_users():
        db = get_db()
        repo = UserRepository(db)
        users = repo.find_all()
        deposit_totals = DepositRepository(db).totals_by_tenant()
        return jsonify([
            {
                "id": u.id,
                "username": u.username,
                "phone": u.phone,
                "fullName": u.full_name,
                "mail": u.mail,
                "role": u.role,
                "registrationCompleted": u.registration_completed,
                "moveInDate": u.move_in_date.isoformat() if u.move_in_date else None,
                "demandedDeposit": u.demanded_deposit,
                "totalAmountDeposited": deposit_totals.get(u.username, 0.0),
                # Only meaningful before registration -- once registered the code is a
                # stale leftover, so don't hand it out for an account it can't be used on.
                "registrationKey": u.registration_code if not u.registration_completed else None,
            }
            for u in users
        ]), 200

    @app.route("/api/users/names", methods=["GET"])
    @require_role("ADMIN")
    def get_tenant_usernames():
        db = get_db()
        repo = UserRepository(db)
        return jsonify(repo.find_tenant_usernames()), 200

    @app.route("/api/users/update/<int:user_id>", methods=["PUT"])
    @require_role("ADMIN")
    def update_user(user_id: int):
        data = request.get_json(silent=True) or {}
        db = get_db()
        repo = UserRepository(db)
        user = repo.find_by_id(user_id)
        if user is None:
            logger.warning("Update-user failed: user id %s not found.", user_id)
            return jsonify({"error": "User not found"}), 404
        if data.get("username"):
            user.username = data["username"]
        if data.get("role"):
            user.role = data["role"]
        if data.get("password"):
            user.password = generate_password_hash(data["password"])
        if data.get("mail"):
            user.mail = data["mail"]
        if "registrationCompleted" in data:
            registered = data["registrationCompleted"] is True
            if not registered and user.role == "ADMIN":
                return jsonify({"error": "An admin account cannot be set to not registered (you would be locked out)."}), 400
            # Resetting a previously-registered tenant back to "not registered"
            # (e.g. reusing a room's username for a new tenant) needs a fresh
            # registration key -- otherwise the OLD tenant, who may still
            # remember their own key, could register again on this username.
            if not registered and user.registration_completed and user.role == "TENANT":
                user.registration_code = generate_registration_key()
            user.registration_completed = registered
        # Present-but-empty means "clear it"; absent means "leave it alone".
        if "fullName" in data:
            user.full_name = " ".join(str(data["fullName"] or "").split())[:100] or None
        if "phone" in data:
            if str(data["phone"] or "").strip():
                phone = _normalize_phone(data["phone"])
                if phone is None:
                    return jsonify({"error": "Enter a valid 10-digit mobile number"}), 400
                user.phone = phone
            else:
                user.phone = None
        repo.save(user)
        logger.info("Admin updated user '%s' (id=%s, fields=%s).", user.username, user_id, list(data.keys()))
        return jsonify({
            "id": user.id,
            "username": user.username,
            "phone": user.phone,
            "fullName": user.full_name,
            "mail": user.mail,
            "role": user.role,
            "registrationCompleted": user.registration_completed,
            "registrationKey": user.registration_code,
        }), 200

    @app.route("/api/users/<int:user_id>/movein-deposit", methods=["PUT"])
    @require_role("ADMIN")
    def update_movein_deposit(user_id: int):
        data = request.get_json(silent=True) or {}
        db = get_db()
        deposit_service = DepositService(DepositRepository(db), UserRepository(db))
        try:
            if data.get("moveInDate"):
                deposit_service.set_move_in_date(user_id, data["moveInDate"])
            if data.get("demandedDeposit") is not None and data.get("demandedDeposit") != "":
                deposit_service.set_demanded_deposit(user_id, float(data["demandedDeposit"]))
            if data.get("manualDepositAmount"):
                deposit_service.add_manual_deposit(user_id, float(data["manualDepositAmount"]), data.get("notes"))
            user = UserRepository(db).find_by_id(user_id)
            if user is None:
                return jsonify({"error": "User not found"}), 404
            return jsonify(deposit_service.get_summary(user.username)), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/users/me/movein-deposit", methods=["GET"])
    @require_auth
    def get_movein_deposit():
        username = request.args.get("username") or g.current_user["username"]
        if g.current_user["role"] != "ADMIN" and username != g.current_user["username"]:
            return jsonify({"error": "Forbidden"}), 403
        db = get_db()
        deposit_service = DepositService(DepositRepository(db), UserRepository(db))
        try:
            return jsonify(deposit_service.get_summary(username)), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.route("/api/users/deposit/createOrder", methods=["POST"])
    @require_auth
    def create_deposit_order():
        data = request.get_json(silent=True) or {}
        db = get_db()
        payment_service = PaymentService(TenantBillRepository(db))
        try:
            amount = float(data.get("amount"))
            return jsonify(payment_service.create_deposit_order(g.current_user["username"], amount)), 200
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc) or "Invalid amount"}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503

    @app.route("/api/users/deposit/verify", methods=["POST"])
    @require_auth
    def verify_deposit():
        data = request.get_json(silent=True) or {}
        db = get_db()
        payment_service = PaymentService(TenantBillRepository(db))
        username = g.current_user["username"]
        try:
            amount = payment_service.verify_deposit_payment(username, data.get("orderId"), data.get("paymentId"), data.get("signature"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        deposit_service = DepositService(DepositRepository(db), UserRepository(db))
        summary = deposit_service.record_payment(username, amount, data.get("paymentId"))
        return jsonify(summary), 200

    @app.route("/api/users/delete/<int:user_id>", methods=["DELETE"])
    @require_role("ADMIN")
    def delete_user(user_id: int):
        db = get_db()
        repo = UserRepository(db)
        if not repo.exists_by_id(user_id):
            logger.warning("Delete-user failed: user id %s not found.", user_id)
            return jsonify({"error": "User not found"}), 404
        repo.delete_by_id(user_id)
        logger.info("Admin deleted user id %s.", user_id)
        return jsonify({"message": "User deleted"}), 200

    @app.route("/api/tenants/<name>", methods=["GET"])
    @require_self_or_admin(lambda name: name)
    def get_tenant_bills(name: str):
        db = get_db()
        repo = TenantBillRepository(db)
        service = TenantBillService(repo, UserRepository(db), email_service)
        bills = service.get_tenant_bills(name)
        return jsonify([
            {
                "id": b.id,
                "tenantName": b.tenant_name,
                "monthYear": b.month_year,
                "billType": b.bill_type,
                "rent": b.rent,
                "water": b.water,
                "electricity": b.electricity,
                "miscellaneous": b.miscellaneous,
                "paid": b.paid,
                "paidDate": to_iso_utc(b.paid_date),
                "createdDate": b.created_date.isoformat() if b.created_date else None,
            }
            for b in bills
        ]), 200

    @app.route("/api/tenants/createOrder/<int:bill_id>", methods=["POST"])
    @require_auth
    def create_order(bill_id: int):
        db = get_db()
        repo = TenantBillRepository(db)
        bill = repo.find_by_id(bill_id)
        if bill is None:
            return jsonify({"error": "Bill not found"}), 404
        if g.current_user["role"] != "ADMIN" and bill.tenant_name != g.current_user["username"]:
            logger.warning("Forbidden: user '%s' attempted to create a payment order for bill %s (tenant=%s).",
                           g.current_user["username"], bill_id, bill.tenant_name)
            return jsonify({"error": "Forbidden"}), 403
        service = PaymentService(repo)
        try:
            return jsonify(service.create_order(bill_id)), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503

    @app.route("/api/tenants/markPaid/<int:bill_id>", methods=["PUT"])
    @require_auth
    def mark_paid(bill_id: int):
        db = get_db()
        repo = TenantBillRepository(db)
        user_repo = UserRepository(db)
        bill = repo.find_by_id(bill_id)
        if bill is None:
            return jsonify({"error": "Bill not found"}), 404
        if g.current_user["role"] != "ADMIN" and bill.tenant_name != g.current_user["username"]:
            logger.warning("Forbidden: user '%s' attempted to mark bill %s (tenant=%s) as paid.",
                           g.current_user["username"], bill_id, bill.tenant_name)
            return jsonify({"error": "Forbidden"}), 403

        data = request.get_json(silent=True) or {}
        payment_service = PaymentService(repo)
        try:
            payment_service.verify_payment(bill_id, data.get("orderId"), data.get("paymentId"), data.get("signature"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503

        service = TenantBillService(repo, user_repo, email_service)
        try:
            return jsonify({"message": service.mark_paid(bill_id)}), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.route("/api/tenants/addBill", methods=["POST"])
    @require_role("ADMIN")
    def add_bill():
        data = request.get_json(silent=True) or {}
        bill = TenantBill(
            tenant_name=data.get("tenantName"),
            month_year=data.get("monthYear"),
            bill_type=(data.get("billType") or "RENT").upper(),
            rent=_parse_optional_float(data.get("rent")),
            water=_parse_optional_float(data.get("water")),
            electricity=_parse_optional_float(data.get("electricity")),
            miscellaneous=_parse_optional_float(data.get("miscellaneous")),
            paid=False,
        )
        db = get_db()
        repo = TenantBillRepository(db)
        service = TenantBillService(repo, UserRepository(db), email_service)
        try:
            service.add_bill(bill)
            return jsonify({"message": "Bill added successfully and notification triggered."}), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.route("/api/tenants/bulkBills", methods=["POST"])
    @require_role("ADMIN")
    def bulk_bills():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"error": "Choose an Excel (.xlsx) or CSV file first."}), 400
        data = upload.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return jsonify({"error": "The file is too large (limit 1 MB)."}), 400
        dry_run = (request.form.get("dryRun") or "true").lower() != "false"
        db = get_db()
        bill_repo = TenantBillRepository(db)
        try:
            kind = check_kind(request.form.get("kind"))
            rows = read_rows(upload.filename, data, kind)
            planned = plan_rows(rows, kind, (request.form.get("month") or "").strip() or None, UserRepository(db), bill_repo)
        except BulkError as exc:
            return jsonify({"error": str(exc)}), 400
        if not dry_run:
            import_planned(planned, TenantBillService(bill_repo, UserRepository(db), email_service))
            logger.info("Bulk bill import by '%s': %s", g.current_user["username"], summarize(planned))
        return jsonify({"dryRun": dry_run, "summary": summarize(planned), "rows": public_rows(planned)}), 200

    @app.route("/api/tenants/bulkBills/template", methods=["GET"])
    @require_role("ADMIN")
    def bulk_bills_template():
        kind = request.args.get("type", "")
        try:
            content = build_template(kind)
        except BulkError as exc:
            return jsonify({"error": str(exc)}), 400
        return Response(
            content,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=bills-{kind}-template.xlsx"},
        )

    @app.route("/api/tenants/all", methods=["GET"])
    @require_role("ADMIN")
    def get_all_bills():
        db = get_db()
        repo = TenantBillRepository(db)
        bills = repo.find_all()
        return jsonify([
            {
                "id": b.id,
                "tenantName": b.tenant_name,
                "monthYear": b.month_year,
                "billType": b.bill_type,
                "rent": b.rent,
                "water": b.water,
                "electricity": b.electricity,
                "miscellaneous": b.miscellaneous,
                "paid": b.paid,
                "paidDate": to_iso_utc(b.paid_date),
                "createdDate": b.created_date.isoformat() if b.created_date else None,
            }
            for b in bills
        ]), 200

    @app.route("/api/tenants/deleteBill/<int:bill_id>", methods=["DELETE"])
    @require_role("ADMIN")
    def delete_bill(bill_id: int):
        db = get_db()
        repo = TenantBillRepository(db)
        service = TenantBillService(repo, UserRepository(db), email_service)
        try:
            return jsonify({"message": service.delete_bill(bill_id)}), 200
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.route("/api/tenants/updateBill/<int:bill_id>", methods=["PUT"])
    @require_role("ADMIN")
    def update_bill(bill_id: int):
        data = request.get_json(silent=True) or {}
        db = get_db()
        repo = TenantBillRepository(db)
        service = TenantBillService(repo, UserRepository(db), email_service)
        updated = TenantBill(
            tenant_name=data.get("tenantName"),
            month_year=data.get("monthYear"),
            rent=_parse_optional_float(data.get("rent")),
            water=_parse_optional_float(data.get("water")),
            electricity=_parse_optional_float(data.get("electricity")),
            miscellaneous=_parse_optional_float(data.get("miscellaneous")),
        )
        try:
            return jsonify({"message": service.update_bill(bill_id, updated)}), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.route("/api/tenants/paid-bills/<month_year>", methods=["GET"])
    @require_role("ADMIN")
    def get_paid_bills_for_month(month_year: str):
        db = get_db()
        repo = TenantBillRepository(db)
        service = TenantBillService(repo, UserRepository(db), email_service)
        bills = service.get_paid_bills_for_month(month_year)
        return jsonify([
            {
                "id": b.id,
                "tenantName": b.tenant_name,
                "monthYear": b.month_year,
                "billType": b.bill_type,
                "rent": b.rent,
                "water": b.water,
                "electricity": b.electricity,
                "miscellaneous": b.miscellaneous,
                "paid": b.paid,
            }
            for b in bills
        ]), 200

    @app.route("/api/tenants/complaints", methods=["POST"])
    @require_self_or_admin(lambda: (request.get_json(silent=True) or {}).get("tenantName"))
    def create_complaint():
        data = request.get_json(silent=True) or {}
        complaint = Complaint(
            tenant_name=data.get("tenantName"),
            description=data.get("description"),
        )
        db = get_db()
        repo = ComplaintRepository(db)
        service = ComplaintService(repo)
        complaint = service.create_complaint(complaint)
        return jsonify({"message": "Complaint submitted successfully.", "id": complaint.id}), 200

    @app.route("/api/tenants/complaints/<tenant_name>", methods=["GET"])
    @require_self_or_admin(lambda tenant_name: tenant_name)
    def get_complaints_for_tenant(tenant_name: str):
        db = get_db()
        repo = ComplaintRepository(db)
        service = ComplaintService(repo)
        complaints = service.get_complaints_for_tenant(tenant_name)
        return jsonify([
            {
                "id": c.id,
                "tenantName": c.tenant_name,
                "description": c.description,
                "status": c.status,
                "createdDate": to_iso_utc(c.created_date),
                "resolutionComment": c.resolution_comment,
                "closedDate": to_iso_utc(c.closed_date),
            }
            for c in complaints
        ]), 200

    @app.route("/api/tenants/complaints", methods=["GET"])
    @require_role("ADMIN")
    def get_all_complaints():
        db = get_db()
        repo = ComplaintRepository(db)
        service = ComplaintService(repo)
        complaints = service.get_all_complaints()
        return jsonify([
            {
                "id": c.id,
                "tenantName": c.tenant_name,
                "description": c.description,
                "status": c.status,
                "createdDate": to_iso_utc(c.created_date),
                "resolutionComment": c.resolution_comment,
                "closedDate": to_iso_utc(c.closed_date),
            }
            for c in complaints
        ]), 200

    def _complaint_owner(complaint_id: int):
        c = ComplaintRepository(get_db()).find_by_id(complaint_id)
        return c.tenant_name if c else None

    @app.route("/api/tenants/complaints/<int:complaint_id>/withdraw", methods=["PUT"])
    @require_self_or_admin(_complaint_owner)
    def withdraw_complaint(complaint_id: int):
        service = ComplaintService(ComplaintRepository(get_db()))
        try:
            service.withdraw_complaint(complaint_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"message": "Complaint withdrawn"}), 200

    @app.route("/api/tenants/complaints/<int:complaint_id>/close", methods=["PUT"])
    @require_role("ADMIN")
    def close_complaint(complaint_id: int):
        data = request.get_json(silent=True) or {}
        db = get_db()
        repo = ComplaintRepository(db)
        service = ComplaintService(repo)
        try:
            complaint = service.close_complaint(complaint_id, data.get("resolutionComment"))
            return jsonify({"message": "Closed", "complaint": {
                "id": complaint.id,
                "status": complaint.status,
                "resolutionComment": complaint.resolution_comment,
            }}), 200
        except ValueError:
            return jsonify({"error": "Complaint not found"}), 404

    @app.route("/api/tenants/vacate/preview", methods=["GET"])
    @require_auth
    def preview_vacate():
        service = VacateService(VacateRequestRepository(get_db()))
        return jsonify({"vacateDate": service.preview_vacate_date()}), 200

    @app.route("/api/tenants/vacate/status", methods=["GET"])
    @require_auth
    def vacate_status():
        service = VacateService(VacateRequestRepository(get_db()))
        return jsonify(service.get_status(g.current_user["username"])), 200

    @app.route("/api/tenants/vacate/request", methods=["POST"])
    @require_auth
    def request_vacate():
        service = VacateService(VacateRequestRepository(get_db()))
        try:
            return jsonify(service.request_vacate(g.current_user["username"])), 201
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.route("/api/tenants/vacate/cancel", methods=["PUT"])
    @require_auth
    def cancel_vacate():
        service = VacateService(VacateRequestRepository(get_db()))
        try:
            service.cancel_vacate(g.current_user["username"])
            return jsonify({"message": "Vacate request cancelled"}), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.route("/api/tenants/vacate/acknowledge", methods=["PUT"])
    @require_auth
    def acknowledge_vacate_settlement():
        data = request.get_json(silent=True) or {}
        service = VacateService(VacateRequestRepository(get_db()))
        try:
            return jsonify(service.acknowledge_settlement(g.current_user["username"], data.get("feedback"))), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.route("/api/admin/vacate/all", methods=["GET"])
    @require_role("ADMIN")
    def all_vacate_requests():
        db = get_db()
        service = VacateService(VacateRequestRepository(db), DepositRepository(db))
        return jsonify(service.list_all_open()), 200

    @app.route("/api/admin/vacate/<int:request_id>/approve", methods=["PUT"])
    @require_role("ADMIN")
    def approve_vacate_request(request_id: int):
        db = get_db()
        service = VacateService(VacateRequestRepository(db), DepositRepository(db))
        try:
            return jsonify(service.approve_vacate(request_id)), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.route("/api/admin/vacate/<int:request_id>/settle", methods=["PUT"])
    @require_role("ADMIN")
    def settle_vacate_request(request_id: int):
        data = request.get_json(silent=True) or {}
        db = get_db()
        service = VacateService(VacateRequestRepository(db), DepositRepository(db))
        try:
            deduction = float(data.get("deduction") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "deduction must be a number"}), 400
        try:
            return jsonify(service.settle_vacate(
                request_id, deduction, (data.get("refundMethod") or "").upper(), data.get("note"),
            )), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/admin/vacate/settled", methods=["GET"])
    @require_role("ADMIN")
    def settled_vacate_requests():
        db = get_db()
        service = VacateService(VacateRequestRepository(db))
        complaint_repo = ComplaintRepository(db)
        occupant_repo = OccupantRepository(db)
        bill_repo = TenantBillRepository(db)
        results = service.list_all_settled()
        # So the admin page can show why "Free up this username" is blocked
        # before they even click it, instead of only finding out from the
        # error after -- see TenantOffboardService.finalize_move_out for the
        # actual enforcement.
        for r in results:
            complaints = complaint_repo.find_by_tenant_name_order_by_created_desc(r["tenantUsername"])
            r["openComplaints"] = sum(1 for c in complaints if c.status != "CLOSED")
            occupants = occupant_repo.find_by_tenant_username_order_by_uploaded_desc(r["tenantUsername"])
            r["unverifiedOccupants"] = sum(1 for o in occupants if not o.verified)
            bills = bill_repo.find_by_tenant_name_order_by_month_desc(r["tenantUsername"])
            r["unpaidBills"] = sum(1 for b in bills if not b.paid)
        return jsonify(results), 200

    @app.route("/api/admin/vacate/<int:request_id>/finalize", methods=["PUT"])
    @require_role("ADMIN")
    def finalize_vacate_request(request_id: int):
        db = get_db()
        service = TenantOffboardService(
            VacateRequestRepository(db),
            TenantBillRepository(db),
            ComplaintRepository(db),
            OccupantRepository(db),
            DepositRepository(db),
            ArchivedTenantRepository(db),
            UserRepository(db),
        )
        try:
            return jsonify(service.finalize_move_out(request_id, g.current_user["username"])), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.route("/api/admin/archived-tenants", methods=["GET"])
    @require_role("ADMIN")
    def list_archived_tenants():
        repo = ArchivedTenantRepository(get_db())
        return jsonify([
            {
                "id": r.id,
                "username": r.username,
                "archivedDate": to_iso_utc(r.archived_date),
                "archivedBy": r.archived_by,
                "data": json.loads(r.data),
            }
            for r in repo.find_all()
        ]), 200

    @app.route("/api/tenants/occupants/<tenant>", methods=["GET"])
    @require_self_or_admin(lambda tenant: tenant)
    def list_occupants(tenant: str):
        db = get_db()
        repo = OccupantRepository(db)
        service = OccupantService(repo, UserRepository(db))
        return jsonify(service.list(tenant)), 200

    @app.route("/api/admin/occupants", methods=["GET"])
    @require_role("ADMIN")
    def list_all_occupants():
        db = get_db()
        repo = OccupantRepository(db)
        service = OccupantService(repo, UserRepository(db))
        return jsonify(service.list_all()), 200

    @app.route("/uploads/<path:filepath>", methods=["GET"])
    @require_auth
    def serve_upload(filepath: str):
        # Files are stored as "aadhaar/<tenant_username>/<filename>" -- these are
        # sensitive government ID documents, so only the owning tenant or an admin may
        # fetch one, never an unauthenticated request or a different tenant.
        parts = filepath.split("/")
        owner = parts[1] if len(parts) >= 2 and parts[0] == "aadhaar" else None
        if g.current_user["role"] != "ADMIN" and owner != g.current_user["username"]:
            logger.warning("Forbidden: user '%s' attempted to fetch upload '%s'.", g.current_user["username"], filepath)
            return jsonify({"error": "Forbidden"}), 403
        # Proxies from Supabase Storage when configured (Render's own disk is
        # wiped on every deploy), falling back to the local uploads/ dir.
        result = fetch_uploaded_file(filepath)
        if result is None:
            return jsonify({"error": "File not found"}), 404
        content, content_type = result
        return Response(content, mimetype=content_type or mimetypes.guess_type(filepath)[0] or "application/octet-stream")

    @app.route("/api/tenants/occupants/<tenant>", methods=["POST"])
    @require_self_or_admin(lambda tenant: tenant)
    def add_occupant(tenant: str):
        if "file" not in request.files:
            return jsonify({"error": "File required"}), 400
        file = request.files["file"]
        name = request.form.get("name")
        db = get_db()
        repo = OccupantRepository(db)
        service = OccupantService(repo, UserRepository(db))
        try:
            payload = service.add(tenant, name, file)
            return jsonify(payload), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/tenants/occupants/<int:occupant_id>", methods=["DELETE"])
    @require_role("ADMIN")
    def delete_occupant(occupant_id: int):
        db = get_db()
        repo = OccupantRepository(db)
        service = OccupantService(repo, UserRepository(db))
        try:
            service.delete(occupant_id, ignore_verified=True)
            return jsonify({"message": "Deleted"}), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.route("/api/tenants/occupants/verify/<int:occupant_id>", methods=["PATCH"])
    @require_role("ADMIN")
    def verify_occupant(occupant_id: int):
        db = get_db()
        repo = OccupantRepository(db)
        service = OccupantService(repo, UserRepository(db))
        try:
            return jsonify(service.verify_occupant(occupant_id)), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.route("/api/tenants/logSuccess", methods=["POST"])
    @require_self_or_admin(lambda: (request.get_json(silent=True) or {}).get("tenantName"))
    def log_success():
        data = request.get_json(silent=True) or {}
        db = get_db()
        repo = TransactionLogRepository(db)
        service = TransactionService(repo)
        service.log_success(data.get("tenantName", ""), data.get("paymentId", ""))
        return jsonify({"message": "Success logged"}), 200

    @app.route("/api/tenants/logFailure", methods=["POST"])
    @require_auth
    def log_failure():
        data = request.get_json(silent=True) or {}
        db = get_db()
        repo = TransactionLogRepository(db)
        service = TransactionService(repo)
        service.log_failure(data)
        return jsonify({"message": "Failure logged"}), 200

    @app.route("/api/assistant/ask", methods=["POST"])
    @trace_request
    @require_auth
    def assistant_ask():
        data = request.get_json(silent=True) or {}
        message = data.get("message", "")
        db = get_db()
        service = AssistantService(TenantBillRepository(db), UserRepository(db), DepositRepository(db))
        try:
            answer, trace = service.ask_traced(g.current_user["username"], message)
            body = {"answer": answer}
            # Only ever sent when the server itself is started in debug mode
            # (ASSISTANT_DEBUG=true in the local .env); never on production.
            if os.getenv("ASSISTANT_DEBUG", "").lower() == "true":
                body["trace"] = trace
            return jsonify(body), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
