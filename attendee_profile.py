# attendee_profile.py
from datetime import datetime
import os
import glob
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from flask import request, render_template, redirect, url_for, flash, session, current_app
from bson import ObjectId

from db import db, users_collection  # uses your existing collection
# IMPORTANT: import the existing attendee blueprint defined in attendee.py
from attendee import attendee_bp

# ---------- Config ----------
ALLOWED_IMG_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2MB

def _require_attendee_login():
    if "uid" not in session:
        flash("Please log in to continue.", "warning")
        return False
    if session.get("role") != "attendee":
        flash("Only attendees can access this page.", "danger")
        return False
    return True

def _ext_ok(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMG_EXTS

def _user_doc():
    return users_collection.find_one({"_id": ObjectId(session["uid"])})

# ---------- Profile (view + update) ----------
@attendee_bp.route("/profile", methods=["GET", "POST"])
def attendee_profile():
    if not _require_attendee_login():
        return redirect(url_for("login.login"))

    user = _user_doc()
    if not user:
        flash("Account not found.", "danger")
        return redirect(url_for("login.login"))

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        phone     = (request.form.get("phone") or "").strip()
        email     = (request.form.get("email") or "").strip().lower()
        marketing = (request.form.get("marketing_opt_in") == "on")

        errors = []
        if len(full_name) < 2:
            errors.append("Full name must be at least 2 characters.")
        if not ("@" in email and "." in email):
            errors.append("Enter a valid email address.")
        digits = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
        if not (7 <= len(digits) <= 15):
            errors.append("Enter a valid phone number (7–15 digits).")

        # Uniqueness checks if email/phone changed
        if email and email != user.get("email"):
            if users_collection.find_one({"email": email, "_id": {"$ne": user["_id"]}}):
                errors.append("This email is already in use.")
        if phone and phone != user.get("phone"):
            if users_collection.find_one({"phone": phone, "_id": {"$ne": user["_id"]}}):
                errors.append("This phone is already in use.")

        if errors:
            for e in errors:
                flash(e, "danger")
            # re-render with previously entered values
            return render_template("attendee/profile.html", user={
                **user,
                "full_name": full_name,
                "phone": phone,
                "email": email,
                "settings": {**(user.get("settings") or {}), "marketing_opt_in": marketing}
            })

        # If email changed, mark email_verified False again
        set_updates = {
            "full_name": full_name,
            "phone": phone,
            "email": email,
            "updated_at": datetime.utcnow(),
        }
        settings = user.get("settings") or {}
        settings["marketing_opt_in"] = marketing
        if email != user.get("email"):
            settings["email_verified"] = False
        users_collection.update_one({"_id": user["_id"]}, {"$set": {**set_updates, "settings": settings}})

        # keep session display name in sync if you store it
        session["name"] = full_name

        flash("Profile updated.", "success")
        return redirect(url_for("attendee.attendee_profile"))

    # GET
    return render_template("attendee/profile.html", user=user)

# ---------- Avatar upload (saved directly in /uploads) ----------
@attendee_bp.route("/profile/avatar", methods=["POST"])
def attendee_profile_avatar():
    if not _require_attendee_login():
        return redirect(url_for("login.login"))

    user = _user_doc()
    if not user:
        flash("Account not found.", "danger")
        return redirect(url_for("login.login"))

    f = request.files.get("avatar")
    if not f or f.filename == "":
        flash("Choose an image to upload.", "warning")
        return redirect(url_for("attendee.attendee_profile"))

    if not _ext_ok(f.filename):
        flash("Only PNG, JPG, JPEG, GIF, or WEBP allowed.", "danger")
        return redirect(url_for("attendee.attendee_profile"))

    # Size check
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    if size > MAX_AVATAR_BYTES:
        flash("Image too large (max 2MB).", "danger")
        return redirect(url_for("attendee.attendee_profile"))

    # Resolve uploads dir from config (falls back to <app>/uploads)
    uploads_dir = current_app.config.get("UPLOAD_FOLDER") or os.path.join(current_app.root_path, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    # Clean any previous avatar for this user (different extension)
    uid = str(session["uid"])
    for old in glob.glob(os.path.join(uploads_dir, f"{uid}.*")):
        try:
            os.remove(old)
        except OSError:
            pass

    # Save as <uid>.<ext> in /uploads
    ext = f.filename.rsplit(".", 1)[1].lower()
    filename = secure_filename(f"{uid}.{ext}")
    fpath = os.path.join(uploads_dir, filename)
    f.save(fpath)

    # URL served by your public.py -> /uploads/<filename>
    avatar_url = f"/uploads/{filename}"
    users_collection.update_one(
        {"_id": user["_id"]},
        {"$set": {"avatar_url": avatar_url, "updated_at": datetime.utcnow()}}
    )

    flash("Avatar updated.", "success")
    return redirect(url_for("attendee.attendee_profile"))

# ---------- Change Password ----------
@attendee_bp.route("/change-password", methods=["POST"])
def attendee_change_password():
    if not _require_attendee_login():
        return redirect(url_for("login.login"))

    user = _user_doc()
    if not user:
        flash("Account not found.", "danger")
        return redirect(url_for("login.login"))

    current_pwd = request.form.get("current_password") or ""
    new_pwd     = request.form.get("new_password") or ""
    confirm     = request.form.get("confirm_password") or ""

    def _valid_pwd(v: str) -> bool:
        return len(v) >= 8 and any(c.isalpha() for c in v) and any(c.isdigit() for c in v)

    if not check_password_hash(user.get("password_hash", ""), current_pwd):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for("attendee.attendee_profile"))
    if not _valid_pwd(new_pwd):
        flash("New password must be 8+ chars with letters & numbers.", "danger")
        return redirect(url_for("attendee.attendee_profile"))
    if new_pwd != confirm:
        flash("New passwords do not match.", "danger")
        return redirect(url_for("attendee.attendee_profile"))
    if check_password_hash(user.get("password_hash", ""), new_pwd):
        flash("New password must be different from the current one.", "warning")
        return redirect(url_for("attendee.attendee_profile"))

    users_collection.update_one(
        {"_id": user["_id"]},
        {"$set": {"password_hash": generate_password_hash(new_pwd), "updated_at": datetime.utcnow()}}
    )
    flash("Password changed successfully.", "success")
    return redirect(url_for("attendee.attendee_profile"))
