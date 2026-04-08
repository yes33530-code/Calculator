import re
import sqlite3
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "elite_invest.db"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
RATE_LIMIT_STORE: dict = {}   # {ip: [unix_timestamps]}

PHONE_RE = re.compile(r"^996\d{9}$")


# ── DATABASE ─────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                submitted_at     TEXT    NOT NULL,
                goal             TEXT,
                mode             TEXT,
                currency         TEXT,
                start_amount     REAL,
                monthly_pmt      REAL,
                term_months      INTEGER,
                target_amount    REAL,
                result_elite     REAL,
                result_bank      REAL,
                result_apartment REAL,
                phone            TEXT    NOT NULL,
                telegram         TEXT,
                ip_address       TEXT,
                user_agent       TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_leads_sub ON leads(submitted_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone)"
        )


# ── RATE LIMITER ──────────────────────────────────────────────────────────────

def check_rate_limit(ip: str, max_per_hour: int = 5) -> bool:
    now = datetime.now(timezone.utc).timestamp()
    window = RATE_LIMIT_STORE.setdefault(ip, [])
    window[:] = [t for t in window if now - t < 3600]
    if len(window) >= max_per_hour:
        return False
    window.append(now)
    return True


# ── BASIC AUTH ────────────────────────────────────────────────────────────────

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.password != ADMIN_PASSWORD:
            return (
                jsonify({"error": "unauthorized"}),
                401,
                {"WWW-Authenticate": 'Basic realm="EliteInvest Admin"'},
            )
        return f(*args, **kwargs)
    return decorated


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "calculator.html")


@app.route("/health")
def health():
    try:
        with get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        return jsonify({"status": "ok", "db": "ok", "leads_count": count})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/api/leads", methods=["POST"])
def create_lead():
    ip = request.remote_addr or "unknown"
    if not check_rate_limit(ip):
        return jsonify({"ok": False, "error": "rate_limit"}), 429

    data = request.get_json(silent=True) or {}

    # Phone validation
    phone_raw = str(data.get("phone", "")).strip()
    phone_digits = re.sub(r"\D", "", phone_raw)
    if not phone_digits:
        return jsonify({"ok": False, "error": "missing_phone"}), 400
    if not PHONE_RE.match(phone_digits):
        return jsonify({"ok": False, "error": "invalid_phone"}), 400

    submitted_at = datetime.now(timezone.utc).isoformat()
    ua = (request.user_agent.string or "")[:500] if request.user_agent else ""

    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO leads (
                submitted_at, goal, mode, currency,
                start_amount, monthly_pmt, term_months, target_amount,
                result_elite, result_bank, result_apartment,
                phone, telegram, ip_address, user_agent
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                submitted_at,
                data.get("goal"),
                data.get("mode"),
                data.get("currency"),
                data.get("start_amount"),
                data.get("monthly_pmt"),
                data.get("term_months"),
                data.get("target_amount"),
                data.get("result_elite"),
                data.get("result_bank"),
                data.get("result_apartment"),
                phone_digits,
                data.get("telegram") or None,
                ip,
                ua,
            ),
        )
        lead_id = cur.lastrowid

    return jsonify({"ok": True, "lead_id": lead_id})


@app.route("/api/leads", methods=["GET"])
@require_admin
def list_leads():
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        days = 30
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE submitted_at >= ? ORDER BY submitted_at DESC",
            (cutoff,),
        ).fetchall()
    leads = [dict(r) for r in rows]
    return jsonify({"leads": leads, "count": len(leads)})


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", 5001))
    debug = os.getenv("FLASK_ENV", "production") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
