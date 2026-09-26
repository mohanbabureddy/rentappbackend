import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler

if __package__ in (None, ""):
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from flask import Flask, request
from flask_cors import CORS

from app.database import Base, close_db, engine
from app.routes import register_routes


app = Flask(__name__)
app.teardown_appcontext(close_db)

logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(logs_dir, exist_ok=True)

app.logger.handlers.clear()
file_handler = RotatingFileHandler(
    os.path.join(logs_dir, "app.log"),
    maxBytes=1 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

# Render's log viewer (and most hosts) only captures stdout/stderr -- the log file
# above lives on ephemeral disk that's invisible without Shell access, so without
# this handler every exception logged here would be silently unobservable in prod.
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setLevel(logging.DEBUG)
stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

app.logger.addHandler(file_handler)
app.logger.addHandler(stream_handler)
app.logger.setLevel(logging.DEBUG)
app.logger.propagate = False

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
if not any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == file_handler.baseFilename for h in root_logger.handlers):
    root_logger.addHandler(file_handler)
if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
    root_logger.addHandler(stream_handler)

_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "https://vgrpay.uk",
    "https://d8aff7a8.rentapp1.pages.dev",
    "https://rentappfrontend.onrender.com",
    # Free Static Site that replaces the Docker web service (which slept after 15 idle minutes).
    "https://rentapp-web-ympg.onrender.com",
]
if os.getenv("APP_ENV") == "local":
    # Testing from a phone on the home Wi-Fi: http://192.168.x.x:3000 (never allowed in production).
    _ALLOWED_ORIGINS.append(re.compile(r"^http://192\.168\.\d{1,3}\.\d{1,3}:3000$"))
CORS(
    app,
    # /uploads/* also needs CORS -- it's fetched cross-origin with an
    # Authorization header (to check the requester owns the document),
    # which browsers always preflight; without this the preflight itself
    # returns 200 (Flask's default OPTIONS handling) but the browser then
    # blocks the real GET for having no Access-Control-Allow-Origin,
    # surfacing as a bare "Failed to fetch" with no server-side error at all.
    resources={
        r"/api/*": {"origins": _ALLOWED_ORIGINS},
        r"/uploads/*": {"origins": _ALLOWED_ORIGINS},
    },
)


@app.before_request
def log_requests():
    payload = None
    if request.is_json:
        payload = request.get_json(silent=True)
    app.logger.info(
        "Incoming request: method=%s path=%s remote_addr=%s query=%s payload=%s",
        request.method,
        request.path,
        request.remote_addr,
        request.query_string.decode("utf-8", errors="replace"),
        payload,
    )


@app.after_request
def log_responses(response):
    app.logger.info(
        "Response: method=%s path=%s status=%s size=%s",
        request.method,
        request.path,
        response.status_code,
        response.content_length,
    )
    return response


Base.metadata.create_all(bind=engine)


# Columns added after the first release. create_all never alters an existing
# table, so a database created earlier (e.g. an old local MySQL) gets them here.
_LATER_COLUMNS = [
    ("users", "full_name", "VARCHAR(100)"),
    ("users", "demanded_deposit", "DOUBLE PRECISION"),
    ("tenant_bills", "bill_type", "VARCHAR(20) NOT NULL DEFAULT 'RENT'"),
    ("users", "session_version", "INTEGER NOT NULL DEFAULT 0"),
    ("vacate_requests", "approved_date", "DATETIME"),
    ("vacate_requests", "settlement_deduction", "DOUBLE PRECISION"),
    ("vacate_requests", "settlement_refund_amount", "DOUBLE PRECISION"),
    ("vacate_requests", "settlement_refund_method", "VARCHAR(20)"),
    ("vacate_requests", "settlement_note", "VARCHAR(500)"),
    ("vacate_requests", "settled_date", "DATETIME"),
    ("users", "registration_code", "VARCHAR(20)"),
    ("tenant_bills", "paid_via", "VARCHAR(20)"),
    ("vacate_requests", "tenant_acknowledged", "INTEGER NOT NULL DEFAULT 0"),
    ("vacate_requests", "tenant_feedback", "TEXT"),
    ("vacate_requests", "acknowledged_date", "DATETIME"),
]


def _ensure_later_columns():
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    if "vacate_requests" in inspector.get_table_names():
        existing_columns = {c["name"] for c in inspector.get_columns("vacate_requests")}
        if "approved_date" not in existing_columns:
            # Pre-dates the owner-approval workflow: requests made under the old
            # ACTIVE/CANCELLED-only model were already confirmed, so they map to
            # today's APPROVED rather than the new PENDING (which would otherwise
            # wrongly ask the owner to approve something already in motion).
            with engine.begin() as conn:
                conn.execute(text("UPDATE vacate_requests SET status = 'APPROVED' WHERE status = 'ACTIVE'"))

    users_columns = {c["name"] for c in inspector.get_columns("users")} if "users" in inspector.get_table_names() else set()
    # Registration keys are new -- every tenant account created before this
    # column existed has none yet, and without one they'd have nothing to
    # hand a new registrant and nothing for the owner to export.
    need_key_backfill = "registration_code" not in users_columns

    for table, column, ddl in _LATER_COLUMNS:
        if column not in {c["name"] for c in inspect(engine).get_columns(table)}:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            app.logger.info("Added %s.%s column.", table, column)

    if need_key_backfill:
        from app.database import SessionLocal
        from app.models import User
        from app.services import generate_registration_key

        session = SessionLocal()
        try:
            pending = session.query(User).filter(
                User.role == "TENANT", User.registration_completed == False, User.registration_code.is_(None)  # noqa: E712 (BitBoolean; == compiles to "= 0", .is_() would emit "IS 0" which MySQL rejects)
            ).all()
            for u in pending:
                u.registration_code = generate_registration_key()
            if pending:
                session.commit()
                app.logger.info("Generated registration keys for %d pre-existing pending tenant(s).", len(pending))
        finally:
            session.close()


_ensure_later_columns()
register_routes(app)
app.logger.info("Application startup complete")

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
