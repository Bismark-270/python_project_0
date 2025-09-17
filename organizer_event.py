# organizer_event.py
from datetime import datetime
import json, os, uuid
from bson import ObjectId
from flask import (
    request, render_template, redirect, url_for,
    flash, session, current_app
)
from werkzeug.utils import secure_filename
from db import db

# Import the blueprint instance defined in organizer.py
from organizer import organizer_bp


# ----------------- Helpers -----------------
def _require_login() -> bool:
    if "uid" not in session:
        flash("Please log in to continue.", "warning")
        return False
    return True

def _compute_status(ev) -> str:
    # Derived, for display
    starts_at = ev.get("starts_at")
    if isinstance(starts_at, str):
        try:
            starts_at = datetime.fromisoformat(starts_at)
        except Exception:
            starts_at = None
    if (ev.get("status") or "draft") == "draft":
        return "Draft"
    now = datetime.utcnow()
    if starts_at and starts_at >= now:
        return "Upcoming"
    return "Past"

def _allowed_image(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[-1].lower()
    return ext in {"jpg", "jpeg", "png", "webp", "gif"}

def _unique_name(filename: str) -> str:
    # Keep extension, randomize basename
    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
    return f"{uuid.uuid4().hex}{ext}"

def _public_image_url(fname: str | None) -> str | None:
    """
    Convert a stored filename to a public URL via organizer.media.
    Absolute URLs are passed through unchanged.
    """
    if not fname:
        return None
    s = str(fname).strip()
    low = s.lower()
    if low.startswith("http://") or low.startswith("https://") or low.startswith("//"):
        return s
    return url_for("organizer.media", filename=s.lstrip("/\\"))

def _save_one(file_storage):
    """Save a single FileStorage to UPLOAD_FOLDER if allowed. Return filename or None."""
    if not file_storage or not file_storage.filename:
        return None
    if not _allowed_image(file_storage.filename):
        return None
    safe = secure_filename(file_storage.filename)
    unique = _unique_name(safe)

    folder = current_app.config.get("UPLOAD_FOLDER")
    if not folder:
        # Sensible default: <app.root>/uploads
        folder = os.path.join(current_app.root_path, "uploads")
    os.makedirs(folder, exist_ok=True)

    path = os.path.join(folder, unique)
    file_storage.save(path)
    return unique

def _save_many(files):
    out = []
    for f in (files or []):
        saved = _save_one(f)
        if saved:
            out.append(saved)
    return out


# ----------------- Routes -----------------
@organizer_bp.route("/events", methods=["GET"], endpoint="events_list")
def events_list():
    """
    List events with filters + pagination.
    Ensures template gets: events, total, page, pages, per_page, q, status.
    """
    if not _require_login():
        return redirect(url_for("login.login"))
    uid = session["uid"]

    # Filters
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "all").lower()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    try:
        per_page = max(5, min(50, int(request.args.get("per_page", 10))))
    except Exception:
        per_page = 10

    query = {"organizer_id": uid}

    # status filter
    now = datetime.utcnow()
    if status in {"draft", "published"}:
        query["status"] = status
    elif status == "upcoming":
        query["status"] = "published"
        query["starts_at"] = {"$gte": now}
    elif status == "past":
        query["status"] = "published"
        query["starts_at"] = {"$lt": now}
    # else: "all" -> no extra filter

    # title contains (case-insensitive)
    if q:
        query["title"] = {"$regex": q, "$options": "i"}

    total = db.events.count_documents(query) if "events" in db.list_collection_names() else 0
    events = []
    if total:
        cursor = (
            db.events.find(query, {
                "_id": 1, "title": 1, "description": 1, "status": 1,
                "starts_at": 1, "location": 1, "images": 1, "tiers": 1,
                "created_at": 1
            })
            .sort([("created_at", -1)])  # newest first
            .skip((page - 1) * per_page)
            .limit(per_page)
        )
        events = list(cursor)

    # Enrich for template
    has_tickets = "tickets" in db.list_collection_names()
    for ev in events:
        ev["id"] = str(ev["_id"])
        # Derived status (Draft/Upcoming/Past)
        ev["derived_status"] = _compute_status(ev)
        # Supply + sold
        tiers = ev.get("tiers") or []
        ev["supply"] = int(sum(int(t.get("supply") or 0) for t in tiers))
        ev["sold"] = int(db.tickets.count_documents({"event_id": ev["id"]})) if has_tickets else 0
        # Images always list
        if not isinstance(ev.get("images"), list):
            ev["images"] = []
        # Map first image to public URL for cards
        ev["cover_img"] = _public_image_url(ev["images"][0]) if ev["images"] else None

    pages = max(1, (total + per_page - 1) // per_page)

    return render_template(
        "organizer/events_list.html",
        events=events,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
        q=q,
        status=status,
    )


@organizer_bp.route("/events/new", methods=["GET", "POST"], endpoint="events_new")
def events_new():
    if not _require_login():
        return redirect(url_for("login.login"))
    uid = session["uid"]

    if request.method == "POST":
        # Basic fields
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        start_raw = (request.form.get("start_datetime") or "").strip()
        location_type = (request.form.get("location_type") or "venue").lower()

        # Parse datetime-local (YYYY-MM-DDTHH:MM)
        starts_at = None
        if start_raw:
            try:
                starts_at = datetime.strptime(start_raw, "%Y-%m-%dT%H:%M")
            except Exception:
                starts_at = None

        # Location payload
        location = {"type": location_type}
        if location_type == "venue":
            location.update({
                "venue_name": (request.form.get("venue_name") or "").strip(),
                "address": (request.form.get("venue_address") or "").strip(),
                "city": (request.form.get("venue_city") or "").strip(),
            })
        else:
            location["online_url"] = (request.form.get("online_url") or "").strip()

        # Tiers from hidden JSON (keep original index for file names)
        tiers_json = request.form.get("tiers_json") or "[]"
        try:
            tiers_raw = json.loads(tiers_json)
        except Exception:
            tiers_raw = []

        action = (request.form.get("action") or "draft").lower()
        status = "published" if action == "publish" else "draft"

        # Validate
        errors = []
        if len(title) < 3:
            errors.append("Title must be at least 3 characters.")
        if not starts_at:
            errors.append("Please select a valid date & time.")
        if location_type == "venue" and not location.get("venue_name"):
            errors.append("Please enter a venue name.")
        if location_type == "online" and not location.get("online_url"):
            errors.append("Please enter the online event URL.")

        # Clean tiers
        clean_tiers = []
        for oi, t in enumerate(tiers_raw):
            name = (t.get("name") or "").strip()
            if not name:
                # Skip unnamed tiers (file indices remain aligned by 'oi')
                continue
            try:
                price = float(t.get("price") or 0)
            except Exception:
                price = 0.0
            try:
                supply = int(t.get("supply") or 0)
            except Exception:
                supply = 0
            try:
                per_order = int(t.get("per_order_limit") or 0)
            except Exception:
                per_order = 0

            s_raw = (t.get("sales_start") or "").strip()
            e_raw = (t.get("sales_end") or "").strip()
            s_dt = e_dt = None
            if s_raw:
                try: s_dt = datetime.strptime(s_raw, "%Y-%m-%dT%H:%M")
                except Exception: s_dt = None
            if e_raw:
                try: e_dt = datetime.strptime(e_raw, "%Y-%m-%dT%H:%M")
                except Exception: e_dt = None

            # Files for this tier index
            cover = request.files.get(f"tier_cover_{oi}")
            gallery_files = request.files.getlist(f"tier_gallery_{oi}[]")
            cover_fn = _save_one(cover)
            gallery_fns = _save_many(gallery_files)

            tier_doc = {
                "name": name,
                "price": price,
                "supply": supply,
                "per_order_limit": per_order,
                "sales_start": s_dt,
                "sales_end": e_dt,
                "refundable": bool(t.get("refundable")),
            }
            if cover_fn:
                tier_doc["cover_image"] = cover_fn
            if gallery_fns:
                tier_doc["gallery_images"] = gallery_fns

            clean_tiers.append(tier_doc)

        if not clean_tiers:
            errors.append("Add at least one ticket tier.")

        # Event gallery images
        event_gallery_files = request.files.getlist("event_images[]")
        event_images = _save_many(event_gallery_files)

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template(
                "organizer/events_new.html",
                form={
                    "title": title,
                    "description": description,
                    "start_datetime": start_raw,
                    "location_type": location_type,
                    "location": location,
                    "tiers_json": json.dumps(clean_tiers, default=str),
                },
            ), 400

        now = datetime.utcnow()
        doc = {
            "organizer_id": uid,
            "title": title,
            "description": description,
            "starts_at": starts_at,
            "location": location,
            "tiers": clean_tiers,
            "status": status,          # draft|published
            "images": event_images,    # list of filenames saved in UPLOAD_FOLDER
            "created_at": now,
            "updated_at": now,
        }
        db.events.insert_one(doc)

        flash("Event published!" if status == "published" else "Draft saved.", "success")
        return redirect(url_for("organizer.events_list"))

    # GET
    return render_template("organizer/events_new.html")


@organizer_bp.route("/events/<event_id>/duplicate", methods=["POST"], endpoint="events_duplicate")
def events_duplicate(event_id):
    if not _require_login():
        return redirect(url_for("login.login"))
    uid = session["uid"]
    try:
        oid = ObjectId(event_id)
    except Exception:
        flash("Invalid event id.", "danger")
        return redirect(url_for("organizer.events_list"))

    ev = db.events.find_one({"_id": oid, "organizer_id": uid})
    if not ev:
        flash("Event not found.", "danger")
        return redirect(url_for("organizer.events_list"))

    now = datetime.utcnow()
    ev.pop("_id", None)
    ev["title"] = f'{ev.get("title", "Untitled")} (Copy)'
    ev["status"] = "draft"
    ev["created_at"] = now
    ev["updated_at"] = now
    # Keep image filenames; they still live under UPLOAD_FOLDER
    db.events.insert_one(ev)
    flash("Event duplicated as draft.", "success")
    return redirect(url_for("organizer.events_list"))


@organizer_bp.route("/events/<event_id>/delete", methods=["POST"], endpoint="events_delete")
def events_delete(event_id):
    if not _require_login():
        return redirect(url_for("login.login"))
    uid = session["uid"]
    try:
        oid = ObjectId(event_id)
    except Exception:
        flash("Invalid event id.", "danger")
        return redirect(url_for("organizer.events_list"))

    res = db.events.delete_one({"_id": oid, "organizer_id": uid})
    if res.deleted_count:
        flash("Event deleted.", "success")
    else:
        flash("Event not found or already deleted.", "warning")
    return redirect(url_for("organizer.events_list"))
