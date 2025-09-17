#!/usr/env/bin Python 3
#-*- coding: utf-8 -*-
"""
# app.py
import os
import re
from datetime import datetime

from flask import (
    Flask,
    render_template,
    send_from_directory,
    request,
    jsonify,
    redirect,
    url_for,
    flash,
)

# Mongo
from db import db

# Blueprints
from login import login_bp
from signup import signup_bp
from attendee import attendee_bp            # attendee area (dashboard, etc.)
from organizer import organizer_bp          # organizer dashboard/routes
import organizer_event                      # side-effect: registers organizer event routes
import organizer_attendees                  # side-effect: registers organizer attendees routes
import organizer_profile                    # ✅ side-effect: registers /organizer/profile routes
from public import public_bp                # public site (may also serve /uploads/*)
from attendee_checkout import checkout_bp   # attendee checkout
from attendee_transactions import attendee_tx_bp

# >>> side-effect imports that attach profile routes onto attendee_bp
import attendee_profile  # noqa: F401


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # -------- Uploads config --------
    uploads_path = os.path.join(app.root_path, "uploads")
    avatars_path = os.path.join(uploads_path, "avatars")
    os.makedirs(avatars_path, exist_ok=True)

    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-change-me"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        UPLOAD_FOLDER=uploads_path,                     # used by generic upload serving
        PROFILE_AVATAR_FOLDER=avatars_path,             # used by attendee_profile.py
        PROFILE_AVATAR_URL_PREFIX="/uploads/avatars/",  # used by attendee_profile.py
        AVATAR_UPLOAD_FOLDER=avatars_path,              # ✅ organizer_profile.py expects this
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_CONTENT_LENGTH_MB", "32")) * 1024 * 1024,
        TEMPLATES_AUTO_RELOAD=True,
    )

    # -------- Serve avatars (endpoint name organizer_profile.py uses) --------
    @app.route("/uploads/avatars/<path:filename>")
    def uploads_avatars(filename):
        filename = filename.lstrip("/\\")
        return send_from_directory(app.config["AVATAR_UPLOAD_FOLDER"], filename)

    # Register blueprints
    app.register_blueprint(login_bp)
    app.register_blueprint(signup_bp)
    app.register_blueprint(attendee_bp)
    app.register_blueprint(organizer_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(checkout_bp)
    app.register_blueprint(attendee_tx_bp)

    # -------- Website routes --------
    @app.route("/")
    def home():
        return render_template("index.html")

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    # -------- Newsletter subscribe route --------
    @app.post("/subscribe")
    def subscribe():
        """
        Accepts form POST or JSON to subscribe an email.
        Stores/updates in `subscriptions` collection (idempotent).
        """
        # Accept form-POST or JSON
        email = (request.form.get("email") if request.form else None) or (
            request.json.get("email") if request.is_json and request.json else None
        )
        source = (request.form.get("source") if request.form else None) or (
            request.json.get("source") if request.is_json and request.json else None
        ) or "homepage"

        # Basic email validation
        def _valid_email(v: str) -> bool:
            if not v or "@" not in v:
                return False
            return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v.strip()))

        if not _valid_email(email):
            if request.accept_mimetypes.best == "application/json" or request.is_json:
                return jsonify({"ok": False, "message": "Enter a valid email address."}), 400
            flash("Enter a valid email address.", "danger")
            return redirect(url_for("home"))

        email = email.strip().lower()
        subscriptions = db["subscriptions"]

        now = datetime.utcnow()
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        ua = request.headers.get("User-Agent", "")

        # Upsert: if exists -> update; else -> insert
        subscriptions.update_one(
            {"email": email},
            {
                "$setOnInsert": {"email": email, "created_at": now},
                "$set": {
                    "updated_at": now,
                    "source": source,
                    "ip": ip,
                    "user_agent": ua,
                },
            },
            upsert=True,
        )

        msg = "Thanks! You’re subscribed. 🎉"
        if request.accept_mimetypes.best == "application/json" or request.is_json:
            return jsonify({"ok": True, "message": msg}), 200

        flash(msg, "success")
        return redirect(url_for("home"))

    # -------- 404 --------
    @app.errorhandler(404)
    def not_found(_e):
        try:
            return render_template("404.html"), 404
        except Exception:
            return render_template("index.html"), 404

    # ---- Optional: print organizer endpoints to verify ----
    with app.app_context():
        for rule in app.url_map.iter_rules():
            if str(rule).startswith("/organizer"):
                print(rule.endpoint, "->", rule)

    return app


# ✅ Expose a top-level WSGI callable for Gunicorn: `app:app`
app = create_app()

if __name__ == "__main__":
    # Dev server only; Render will run Gunicorn which imports `app`
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=True
    )

