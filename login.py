# login.py
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import check_password_hash
from db import db

login_bp = Blueprint("login", __name__)

# Redirect endpoints per role (kept for reuse)
ROLE_REDIRECTS = {
    "organizer": "organizer.dashboard",
    "attendee":  "attendee.attendee_dashboard",
}

def _is_email(v: str) -> bool:
    return "@" in v and "." in v

def _norm_phone(v: str) -> str:
    return "".join(ch for ch in (v or "") if ch.isdigit() or ch == "+")

def _is_phone(v: str) -> bool:
    digits = _norm_phone(v)
    return 7 <= len(digits) <= 15

@login_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identifier = (request.form.get("identifier") or "").strip()
        password   = (request.form.get("password") or "")

        # basic validation
        if not ((_is_email(identifier) or _is_phone(identifier)) and len(password) >= 8):
            flash("Please enter a valid email/phone and an 8+ character password.", "danger")
            return render_template("login.html", identifier=identifier), 400

        # lookup by email or phone (normalize)
        query = {"email": identifier.lower()} if _is_email(identifier) else {"phone": _norm_phone(identifier)}
        user = db.users.find_one(query)
        if not user:
            flash("No account found with those details.", "danger")
            return render_template("login.html", identifier=identifier), 401

        # password check (hash first, fallback to legacy plain if present)
        stored_hash = user.get("password_hash")
        stored_plain = user.get("password")
        ok = check_password_hash(stored_hash, password) if stored_hash else (stored_plain == password if stored_plain else False)
        if not ok:
            flash("Invalid credentials.", "danger")
            return render_template("login.html", identifier=identifier), 401

        if user.get("status") == "disabled":
            flash("Your account is disabled. Please contact support.", "danger")
            return render_template("login.html", identifier=identifier), 403

        # update last login (best effort)
        try:
            db.users.update_one({"_id": user["_id"]}, {"$set": {"last_login_at": datetime.utcnow()}})
        except Exception:
            pass

        # establish session
        session.clear()
        role = (user.get("role") or "").lower()
        session["uid"]  = str(user["_id"])
        session["role"] = role
        session["name"] = user.get("full_name")

        # ---- explicit role-based redirects (organizer first) ----
        if role == "organizer":
            return redirect(url_for("organizer.dashboard"))
        if role == "attendee":
            return redirect(url_for("attendee.attendee_dashboard"))

        # unknown/missing role -> safe fallback
        flash("Logged in, but your role is not set. Taking you home.", "warning")
        return redirect(url_for("home"))

    # GET
    return render_template("login.html")

@login_bp.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    flash("You’ve been logged out.", "success")
    return redirect(url_for("login.login"))
