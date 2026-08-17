"""
auth.py — SQLite-based authentication for Blueprint Document Agent
Tables: users
Roles:  admin | document_editor
Status: pending | approved | rejected
"""

import sqlite3
import hashlib
import hmac
import os
import smtplib
import secrets
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────
DB_PATH      = Path(__file__).parent / "users.db"
ADMIN_EMAIL  = os.getenv("ADMIN_EMAIL", "Srinivasan.r@bristlecone.com")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

# Outlook/Office 365 SMTP — set these in your .env
SMTP_HOST    = os.getenv("SMTP_HOST", "smtp.office365.com")
SMTP_PORT    = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER    = os.getenv("SMTP_USER", "")   # your Outlook email
SMTP_PASS    = os.getenv("SMTP_PASS", "")   # your Outlook password/app password

# ── DB Init ───────────────────────────────────────────────────────
def init_db():
    """Create the users table if it doesn't exist, and ensure admin account exists."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            email          TEXT UNIQUE NOT NULL,
            password_hash  TEXT NOT NULL,
            role           TEXT NOT NULL DEFAULT 'document_editor',
            status         TEXT NOT NULL DEFAULT 'pending',
            approval_token TEXT,
            created_at     TEXT NOT NULL,
            approved_at    TEXT
        )
    """)
    conn.commit()
    _init_sessions_table(conn)

    # Create default admin if not exists. No hardcoded fallback password:
    # if ADMIN_PASSWORD isn't set, generate a random one and print it once.
    admin_email = ADMIN_EMAIL.lower()
    existing = c.execute("SELECT id FROM users WHERE email = ?", (admin_email,)).fetchone()
    if not existing:
        admin_pass = os.getenv("ADMIN_PASSWORD", "")
        if not admin_pass:
            admin_pass = secrets.token_urlsafe(12)
            print("[AUTH] ADMIN_PASSWORD not set — generated a one-time admin password:")
            print(f"[AUTH]   {admin_email} / {admin_pass}")
            print("[AUTH] Save it now (or set ADMIN_PASSWORD in .env and delete users.db to recreate).")
        pw_hash = _hash_password(admin_pass)
        c.execute("""
            INSERT INTO users (email, password_hash, role, status, created_at)
            VALUES (?, ?, 'admin', 'approved', ?)
        """, (admin_email, pw_hash, datetime.utcnow().isoformat()))
        conn.commit()
        print(f"[AUTH] Admin account created: {admin_email}")

    conn.close()

# ── Password helpers ──────────────────────────────────────────────
_PBKDF2_ITERATIONS = 600_000  # OWASP-recommended for PBKDF2-HMAC-SHA256

def _hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 with a random per-user salt (stdlib only).

    Stored format: pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
    """
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"

def _legacy_hash_password(password: str) -> str:
    """Legacy scheme (single SHA-256 + static salt) — kept only to verify old hashes."""
    salt = os.getenv("PASSWORD_SALT", "brd_blueprint_salt_2025")
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()

def _verify_password(password: str, stored_hash: str) -> bool:
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            _, iterations, salt_hex, hash_hex = stored_hash.split("$")
            dk = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
            )
            return hmac.compare_digest(dk.hex(), hash_hex)
        except (ValueError, TypeError):
            return False
    # Legacy 64-hex SHA-256 hash
    return hmac.compare_digest(_legacy_hash_password(password), stored_hash)

def _is_legacy_hash(stored_hash: str) -> bool:
    return not stored_hash.startswith("pbkdf2_sha256$")

# ── Sessions ──────────────────────────────────────────────────────
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "12"))

def _init_sessions_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            email      TEXT NOT NULL,
            role       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)
    conn.commit()

def create_session(email: str, role: str) -> str:
    """Create a server-side session and return its opaque token."""
    from datetime import timedelta
    token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    conn = sqlite3.connect(DB_PATH)
    _init_sessions_table(conn)
    conn.execute(
        "INSERT INTO sessions (token, email, role, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (token, email, role, now.isoformat(), (now + timedelta(hours=SESSION_TTL_HOURS)).isoformat()),
    )
    # Opportunistically purge expired sessions
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
    conn.commit()
    conn.close()
    return token

def validate_session(token: str):
    """Return {email, role} if the token is valid and unexpired, else None."""
    if not token:
        return None
    conn = sqlite3.connect(DB_PATH)
    _init_sessions_table(conn)
    row = conn.execute(
        "SELECT email, role, expires_at FROM sessions WHERE token = ?", (token,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    email, role, expires_at = row
    try:
        expired = datetime.fromisoformat(expires_at) < datetime.utcnow()
    except (ValueError, TypeError):
        expired = True  # unparseable expiry → treat as expired
    if expired:
        delete_session(token)
        return None
    return {"email": email, "role": role}

def delete_session(token: str):
    if not token:
        return
    conn = sqlite3.connect(DB_PATH)
    _init_sessions_table(conn)
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()

# ── Signup ────────────────────────────────────────────────────────
def signup_user(email: str, password: str, role: str = "document_editor") -> dict:
    """
    Register a new user.
    Returns: { success: bool, message: str }
    """
    email = email.strip().lower()

    if not email or "@" not in email:
        return {"success": False, "message": "Please enter a valid email address."}

    if len(password) < 8:
        return {"success": False, "message": "Password must be at least 8 characters."}

    if role not in ("admin", "document_editor"):
        role = "document_editor"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    existing = c.execute("SELECT id, status FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.close()
        if existing[1] == "pending":
            return {"success": False, "message": "This email is already registered and awaiting approval."}
        elif existing[1] == "approved":
            return {"success": False, "message": "This email is already registered. Please log in."}
        else:
            return {"success": False, "message": "This account was rejected. Please contact the admin."}

    pw_hash        = _hash_password(password)
    approval_token = secrets.token_urlsafe(32)

    c.execute("""
        INSERT INTO users (email, password_hash, role, status, approval_token, created_at)
        VALUES (?, ?, ?, 'pending', ?, ?)
    """, (email, pw_hash, role, approval_token, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

    # Send approval email to admin
    _send_approval_request(email, role, approval_token)

    return {
        "success": True,
        "message": "Sign up successful! Your account is pending admin approval. You will be notified once approved."
    }

# ── Login ─────────────────────────────────────────────────────────
def login_user(email: str, password: str) -> dict:
    """
    Authenticate a user.
    Returns: { success: bool, message: str, role: str|None }
    """
    email = email.strip().lower()
    conn  = sqlite3.connect(DB_PATH)
    c     = conn.cursor()

    user = c.execute(
        "SELECT id, password_hash, role, status FROM users WHERE email = ?", (email,)
    ).fetchone()
    conn.close()

    if not user:
        return {"success": False, "message": "Invalid email or password."}

    _, pw_hash, role, status = user

    if not _verify_password(password, pw_hash):
        return {"success": False, "message": "Invalid email or password."}

    # Transparently upgrade legacy SHA-256 hashes to PBKDF2 on successful login
    if _is_legacy_hash(pw_hash):
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE email = ?",
            (_hash_password(password), email),
        )
        conn.commit()
        conn.close()

    if status == "pending":
        return {"success": False, "message": "Your account is pending admin approval. Please wait for the approval email.", "status": "pending"}

    if status == "rejected":
        return {"success": False, "message": "Your account has been rejected. Please contact the admin.", "status": "rejected"}

    if status != "approved":
        return {"success": False, "message": "Account not active. Please contact the admin."}

    return {"success": True, "message": "Login successful.", "role": role}

# ── Approve ───────────────────────────────────────────────────────
def approve_user(token: str) -> dict:
    """
    Approve a user via their approval token.
    Returns: { success: bool, message: str, email: str|None }
    """
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    user = c.execute(
        "SELECT id, email, status FROM users WHERE approval_token = ?", (token,)
    ).fetchone()

    if not user:
        conn.close()
        return {"success": False, "message": "Invalid or expired approval link."}

    user_id, email, status = user

    if status == "approved":
        conn.close()
        return {"success": True, "message": f"{email} is already approved.", "email": email}

    c.execute("""
        UPDATE users SET status = 'approved', approval_token = NULL, approved_at = ?
        WHERE id = ?
    """, (datetime.utcnow().isoformat(), user_id))
    conn.commit()
    conn.close()

    # Notify the user their account is approved
    _send_approval_notification(email)

    return {"success": True, "message": f"User {email} has been approved successfully.", "email": email}

# ── List users (admin) ────────────────────────────────────────────
def list_users() -> list:
    """Return all users (for admin view)."""
    conn  = sqlite3.connect(DB_PATH)
    c     = conn.cursor()
    users = c.execute(
        "SELECT id, email, role, status, created_at, approved_at FROM users ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [
        {"id": u[0], "email": u[1], "role": u[2], "status": u[3], "created_at": u[4], "approved_at": u[5]}
        for u in users
    ]

# ── Email helpers ─────────────────────────────────────────────────
def _send_email(to: str, subject: str, html_body: str):
    """Send email via Outlook/Office 365 SMTP."""
    if not SMTP_USER or not SMTP_PASS:
        print(f"[AUTH] Email not configured. Would send to {to}: {subject}")
        print(f"[AUTH] Set SMTP_USER and SMTP_PASS in your .env file.")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = to
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to, msg.as_string())

        print(f"[AUTH] Email sent to {to}: {subject}")

    except Exception as e:
        print(f"[AUTH] Email failed: {e}")


def _send_approval_request(user_email: str, role: str, token: str):
    """Email admin with approve link."""
    approve_url = f"{APP_BASE_URL}/approve/{token}"
    subject     = f"[Blueprint Agent] New signup: {user_email}"

    # Always print approval URL to terminal (works even without email configured)
    print(f"[AUTH] ✅ APPROVAL LINK for {user_email}:")
    print(f"[AUTH] 👉 {approve_url}")
    print(f"[AUTH] Copy and paste this URL in your browser to approve the user.")
    html = f"""
    <div style="font-family: Segoe UI, sans-serif; max-width: 520px; margin: 0 auto; background: #f9f9f9; padding: 32px; border-radius: 12px;">
      <h2 style="color: #1a1d27; margin-bottom: 4px;">New User Sign Up</h2>
      <p style="color: #666; margin-bottom: 24px;">A new user has requested access to the Blueprint Document Agent.</p>

      <table style="width: 100%; background: #fff; border-radius: 8px; padding: 16px; border: 1px solid #e0e0e0; margin-bottom: 24px;">
        <tr><td style="color: #888; padding: 6px 0; width: 100px;">Email</td><td style="font-weight: 600;">{user_email}</td></tr>
        <tr><td style="color: #888; padding: 6px 0;">Role</td><td style="font-weight: 600;">{role.replace('_', ' ').title()}</td></tr>
        <tr><td style="color: #888; padding: 6px 0;">Time</td><td style="font-weight: 600;">{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</td></tr>
      </table>

      <a href="{approve_url}"
         style="display: inline-block; background: #5b6af0; color: #fff; padding: 14px 28px; border-radius: 8px;
                text-decoration: none; font-weight: 600; font-size: 15px;">
        ✅ Approve this user
      </a>

      <p style="color: #aaa; font-size: 12px; margin-top: 24px;">
        If you did not expect this request, you can safely ignore this email.
      </p>
    </div>
    """
    _send_email(ADMIN_EMAIL, subject, html)


def _send_approval_notification(user_email: str):
    """Notify user that their account has been approved."""
    subject = "[Blueprint Agent] Your account has been approved!"
    html = f"""
    <div style="font-family: Segoe UI, sans-serif; max-width: 520px; margin: 0 auto; background: #f9f9f9; padding: 32px; border-radius: 12px;">
      <h2 style="color: #1a1d27;">You're approved! 🎉</h2>
      <p style="color: #666; margin-bottom: 24px;">
        Your account for the <strong>Blueprint Document Agent</strong> has been approved.
        You can now log in and start generating documents.
      </p>
      <a href="{APP_BASE_URL}"
         style="display: inline-block; background: #5b6af0; color: #fff; padding: 14px 28px; border-radius: 8px;
                text-decoration: none; font-weight: 600; font-size: 15px;">
        Go to Blueprint Agent
      </a>
      <p style="color: #aaa; font-size: 12px; margin-top: 24px;">Login with your registered email and password.</p>
    </div>
    """
    _send_email(user_email, subject, html)