import logging
import os
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
]
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
]


def _ensure_later_columns():
    from sqlalchemy import inspect, text
    for table, column, ddl in _LATER_COLUMNS:
        if column not in {c["name"] for c in inspect(engine).get_columns(table)}:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            app.logger.info("Added %s.%s column.", table, column)


_ensure_later_columns()
register_routes(app)
app.logger.info("Application startup complete")

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
