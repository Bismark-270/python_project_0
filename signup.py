# signup.py
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash
from werkzeug.security import generate_password_hash
from pymongo.errors import DuplicateKeyError
from urllib.parse import quote
import os, requests

from db import db, users_collection  # ✅ import the collection

signup_bp = Blueprint("signup", __name__)

# ---------------- Helpers ----------------

def _valid_email(v: str) -> bool:
    return "@" in v and "." in v

def _valid_phone(v: str) -> bool:
    digits = "".join(ch for ch in v if ch.isdigit() or ch == "+")
    return 7 <= len(digits) <= 15

def _valid_pwd(v: str) -> bool:
    return len(v) >= 8 and any(c.isalpha() for c in v) and any(c.isdigit() for c in v)

def _normalize_phone_gh(phone_raw: str) -> str | None:
    """Return Ghana number as 233XXXXXXXXX or None if invalid."""
    if not phone_raw:
        return None
    p = phone_raw.strip().replace(" ", "").replace("-", "").replace("+", "")
    if p.startswith("0") and len(p) == 10:  # local e.g., 024xxxxxxx
        p = "233" + p[1:]
    if p.startswith("233") and len(p) == 12:
        return p
    return None

ARKESEL_API_KEY = os.getenv("ARKESEL_API_KEY", "").strip()

def _send_sms_arkesel(to_233: str, sender: str, text: str) -> tuple[bool, str]:
    """
    Send SMS via Arkesel. Returns (ok, raw_response_text).
    Does not raise—safe for post-signup best-effort.
    """
    if not ARKESEL_API_KEY:
        return (False, "Missing ARKESEL_API_KEY")
    url = (
        "https://sms.arkesel.com/sms/api?action=send-sms"
        f"&api_key={ARKESEL_API_KEY}"
        f"&to={to_233}"
        f"&from={quote(sender)}"
        f"&sms={quote(text)}"
    )
    try:
        resp = requests.get(url, timeout=15)
        ok = (resp.status_code == 200 and '"code":"ok"' in resp.text)
        return (ok, resp.text)
    except Exception as e:
        return (False, f"EXC:{e}")

# ---------------- Route ----------------

@signup_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        role = (request.form.get("role") or "").strip().lower()
        full_name = (request.form.get("fullName") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        phone = (request.form.get("phone") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirmPassword") or ""

        errors = []
        if role not in {"organizer", "attendee"}:
            errors.append("Please choose Organizer or Attendee.")
        if len(full_name) < 2:
            errors.append("Full name must be at least 2 characters.")
        if not _valid_email(email):
            errors.append("Enter a valid email address.")
        if not _valid_phone(phone):
            errors.append("Enter a valid phone number (7–15 digits).")
        if not _valid_pwd(password):
            errors.append("Password must be 8+ chars with letters & numbers.")
        if password != confirm:
            errors.append("Passwords do not match.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("signup.html",
                                   fullName=full_name, email=email, phone=phone), 400

        now = datetime.utcnow()
        user_doc = {
            "role": role,
            "full_name": full_name,
            "email": email,
            "phone": phone,
            "password_hash": generate_password_hash(password),
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "avatar_url": None,
            "last_login_at": None,
            "settings": {
                "email_verified": False,
                "phone_verified": False,
                "marketing_opt_in": False,  # keep your original default
            },
        }

        try:
            # Clearer conflict messages:
            existing = users_collection.find_one({"$or": [{"email": email}, {"phone": phone}]})
            if existing:
                if existing.get("email") == email:
                    flash("An account with this email already exists.", "danger")
                if existing.get("phone") == phone:
                    flash("An account with this phone already exists.", "danger")
                return render_template("signup.html",
                                       fullName=full_name, email=email, phone=phone), 400

            result = users_collection.insert_one(user_doc)
            print("Inserted user _id:", result.inserted_id)

        except DuplicateKeyError as e:
            msg = "Email or phone already in use."
            if "email" in str(e).lower(): msg = "An account with this email already exists."
            elif "phone" in str(e).lower(): msg = "An account with this phone already exists."
            flash(msg, "danger")
            return render_template("signup.html",
                                   fullName=full_name, email=email, phone=phone), 400

        # ---------- Send welcome SMS (non-blocking best-effort) ----------
        phone_233 = _normalize_phone_gh(phone)
        if phone_233:
            first = (full_name.split()[0] if full_name else "Friend")
            sender = "Akwaaba"  # ✅ sender ID
            # ✅ welcoming + successful + light promo (<= ~160-200 chars)
            text = (
                f"Hi {first}! 🎉 Welcome to AkwaabaTickets—your hub for concerts, shows & more. "
                f"Signup successful. Watch for deals & instant e-tickets. Need help? WhatsApp 0556064611. "
                f"- AkwaabaTickets"
            )
            ok, raw = _send_sms_arkesel(phone_233, sender, text)
            if not ok:
                print("Welcome SMS failed:", raw)
        else:
            print("Welcome SMS skipped: invalid phone after normalization ->", phone)
        # ---------------------------------------------------------------

        flash("Account created! Please sign in.", "success")
        return redirect(url_for("login.login"))

    return render_template("signup.html")
