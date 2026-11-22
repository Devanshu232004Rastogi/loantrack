import logging
import os
import socket
import sys
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

import psycopg2
from flask import Flask, jsonify, request
from psycopg2 import pool

# Logs go to stdout so Docker and Kubernetes can collect them.
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("loantrack")

app = Flask(__name__)

# Container hostname. In Docker Compose it is the container ID,
# in Kubernetes it is the pod name. Used to show which replica answered.
POD_NAME = socket.gethostname()

# Database settings come from environment variables only. No secrets in code.
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "db"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("POSTGRES_DB", "loantrack"),
    "user": os.environ.get("POSTGRES_USER", "loantrack"),
    "password": os.environ.get("POSTGRES_PASSWORD", ""),
}

MIN_POOL_SIZE = 1
MAX_POOL_SIZE = 5
MAX_STARTUP_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 1
MAX_LOAN_AMOUNT = Decimal("1000000000000")  # stays within NUMERIC(14,2)

db_pool = None


def create_pool():
    """Try to create the connection pool, retrying with exponential backoff."""
    global db_pool
    for attempt in range(1, MAX_STARTUP_ATTEMPTS + 1):
        try:
            db_pool = pool.ThreadedConnectionPool(
                MIN_POOL_SIZE, MAX_POOL_SIZE, **DB_CONFIG
            )
            log.info("database pool ready (attempt %d)", attempt)
            return True
        except psycopg2.OperationalError as exc:
            wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            log.warning(
                "database not ready (attempt %d/%d): %s. retrying in %ds",
                attempt, MAX_STARTUP_ATTEMPTS, str(exc).strip(), wait,
            )
            time.sleep(wait)
    log.error("database still unavailable after startup retries")
    return False


# Try at import time so gunicorn workers start with a pool when the DB is up.
# If the DB is still down after the retries, the app does not crash.
# get_connection() tries again on the next request.
create_pool()


def get_connection():
    """Get a pooled connection, creating the pool first if it does not exist yet."""
    if db_pool is None and not create_pool():
        raise RuntimeError("database unavailable")
    return db_pool.getconn()


@contextmanager
def db_cursor():
    """Borrow a connection, run the block, commit, and return the connection to the pool."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        # If the connection is broken, close it instead of returning it to the pool.
        db_pool.putconn(conn, close=bool(conn.closed))


@app.get("/healthz")
def healthz():
    # Liveness: only confirms the process is running. Does not touch the database.
    return jsonify(status="ok", pod=POD_NAME), 200


@app.get("/readyz")
def readyz():
    # Readiness: confirms the database is reachable. Returns 503 if not.
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return jsonify(status="ready", pod=POD_NAME), 200
    except Exception as exc:
        log.warning("readiness check failed: %s", exc)
        return jsonify(status="not ready", pod=POD_NAME), 503


@app.get("/loans")
def list_loans():
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, borrower_name, loan_amount, property_city, status, created_at "
            "FROM loans ORDER BY id"
        )
        rows = cur.fetchall()

    loans = [
        {
            "id": row[0],
            "borrower_name": row[1],
            "loan_amount": str(row[2]),  # string keeps exact decimal value
            "property_city": row[3],
            "status": row[4],
            "created_at": row[5].isoformat(),
        }
        for row in rows
    ]
    return jsonify(loans=loans, pod=POD_NAME), 200


@app.post("/loans")
def create_loan():
    data = request.get_json(silent=True) or {}

    name = (data.get("borrower_name") or "").strip()
    city = (data.get("property_city") or "").strip() or None

    try:
        amount = Decimal(str(data.get("loan_amount")))
    except (InvalidOperation, ValueError):
        return jsonify(error="loan_amount must be a number"), 400

    if not name:
        return jsonify(error="borrower_name is required"), 400
    if not amount.is_finite() or amount <= 0 or amount >= MAX_LOAN_AMOUNT:
        return jsonify(error="loan_amount must be between 0 and 1e12"), 400

    # Parameterized query: %s placeholders keep user input out of the SQL string.
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO loans (borrower_name, loan_amount, property_city) "
            "VALUES (%s, %s, %s) RETURNING id, status, created_at",
            (name, amount, city),
        )
        new_id, status, created_at = cur.fetchone()

    return jsonify(
        id=new_id,
        borrower_name=name,
        loan_amount=str(amount),
        property_city=city,
        status=status,
        created_at=created_at.isoformat(),
        pod=POD_NAME,
    ), 201