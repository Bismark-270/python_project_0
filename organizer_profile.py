# organizer_profile.py
from datetime import datetime
import os, secrets
from bson import ObjectId
from flask import (
    request, render_template, redirect, url_for, flash, session, current_app
)
from werkzeug.security import check_password_hash, generate_password_hash
from organizer import organizer_bp
from db import db

ALLOWED_IMG_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}

def _require_login() -> bool:
    if "uid" not in session:
        flash("Please log in to continue.", "warning")
        return False
    return True

def _is_email(v: str) -> bool:
    v = (v or "").strip()
    return "@" in v and "." in v

def _norm_phone(v: str) -> str:
    return "".join(ch for ch in (v or "") if ch.isdigit() or ch == "+")

def _img_ok(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMG_EXTS

def _save_avatar(file_storage):
    """
    Saves avatar to /static/uploads/avatars and returns a public URL.
    Requires app.config["AVATAR_UPLOAD_FOLDER"] (already set in app.py).
    """
    if not file_storage or file_storage.filename.strip() == "":
        return None
    if not _img_ok(file_storage.filename):
        raise ValueError("Only png, jpg, jpeg, webp, gif allowed.")
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    safe_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(6)}.{ext}"
    dest_dir = current_app.config["AVATAR_UPLOAD_FOLDER"]
    os.makedirs(dest_dir, exist_ok=True)
    file_storage.save(os.path.join(dest_dir, safe_name))
    # Public URL served by app.py -> uploads_avatars endpoint
    return url_for("uploads_avatars", filename=safe_name)

def _load_me():
    """Fetch the logged-in user (by id only). Role is checked separately."""
    uid = session.get("uid")
    if not uid:
        return None
    try:
        return db.users.find_one({"_id": ObjectId(uid)})
    except Exception:
        return None

def _ensure_organizer(me):
    """Return True if user is organizer; otherwise flash + redirect target."""
    role = (me.get("role") or "").lower()
    return role == "organizer"

@organizer_bp.route("/profile", methods=["GET"])
def organizer_profile_view():
    if not _require_login():
        return redirect(url_for("login.login"))

    me = _load_me()
    if not me:
        flash("Organizer not found.", "warning")
        return redirect(url_for("login.login"))

    if not _ensure_organizer(me):
        flash("You must be an organizer to access that page.", "warning")
        return redirect(url_for("login.login"))

    payout = (me.get("settings") or {}).get("payout_address") or me.get("wallet_address") or ""
    return render_template("organizer/profile.html", me=me, payout_address=payout)

@organizer_bp.route("/profile/update", methods=["POST"])
def organizer_profile_update():
    if not _require_login():
        return redirect(url_for("login.login"))

    me = _load_me()
    if not me:
        flash("Organizer not found.", "warning")
        return redirect(url_for("login.login"))
    if not _ensure_organizer(me):
        flash("Organizer access required.", "warning")
        return redirect(url_for("login.login"))

    full_name = (request.form.get("full_name") or "").strip()
    email     = (request.form.get("email") or "").strip()
    phone     = _norm_phone(request.form.get("phone") or "")
    wallet    = (request.form.get("wallet_address") or "").strip()

    if len(full_name) < 2:
        flash("Full name must be at least 2 characters.", "danger")
        return redirect(url_for("organizer.organizer_profile_view"))

    if not _is_email(email):
        flash("Please enter a valid email address.", "danger")
        return redirect(url_for("organizer.organizer_profile_view"))

    if not (7 <= len(phone) <= 15):
        flash("Please enter a valid phone number.", "danger")
        return redirect(url_for("organizer.organizer_profile_view"))

    update = {
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "wallet_address": wallet or None,
        "updated_at": datetime.utcnow(),
        "settings": {
            **(me.get("settings") or {}),
            "payout_address": wallet or None
        }
    }

    file = request.files.get("avatar")
    if file and file.filename.strip():
        try:
            update["avatar_url"] = _save_avatar(file)
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(url_for("organizer.organizer_profile_view"))

    db.users.update_one({"_id": me["_id"]}, {"$set": update})
    flash("Profile updated.", "success")
    return redirect(url_for("organizer.organizer_profile_view"))

@organizer_bp.route("/profile/password", methods=["POST"])
def organizer_profile_change_password():
    if not _require_login():
        return redirect(url_for("login.login"))

    me = _load_me()
    if not me:
        flash("Organizer not found.", "warning")
        return redirect(url_for("login.login"))
    if not _ensure_organizer(me):
        flash("Organizer access required.", "warning")
        return redirect(url_for("login.login"))

    current_pw = request.form.get("current_password") or ""
    new_pw     = request.form.get("new_password") or ""
    confirm_pw = request.form.get("confirm_password") or ""

    if not check_password_hash(me.get("password_hash") or "", current_pw):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for("organizer.organizer_profile_view"))

    # must include both letters and digits
    if len(new_pw) < 8 or new_pw.isalpha() or new_pw.isdigit():
        flash("New password must be at least 8 characters and include letters and numbers.", "danger")
        return redirect(url_for("organizer.organizer_profile_view"))

    if new_pw != confirm_pw:
        flash("New password and confirmation do not match.", "danger")
        return redirect(url_for("organizer.organizer_profile_view"))

    db.users.update_one(
        {"_id": me["_id"]},
        {"$set": {"password_hash": generate_password_hash(new_pw), "updated_at": datetime.utcnow()}}
    )
    flash("Password updated successfully.", "success")
    return redirect(url_for("organizer.organizer_profile_view"))
