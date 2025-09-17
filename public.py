# public.py
import os
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, quote_plus

from bson import ObjectId
from flask import (
    Blueprint,
    render_template,
    request,
    url_for,
    abort,
    flash,
    redirect,
    session,
    make_response,
    current_app,
    send_from_directory,
)
from db import db

public_bp = Blueprint("public", __name__)

_SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")


# ---------- file/url helpers

@public_bp.route("/uploads/<path:filename>")
def uploads(filename):
    """
    Serve files from the top-level 'uploads' folder.
    Falls back to ./uploads if UPLOAD_FOLDER isn't set.
    """
    root = current_app.config.get("UPLOAD_FOLDER")
    if not root:
        # fallback: ./uploads next to app root
        root = os.path.join(current_app.root_path, "..", "uploads")
        root = os.path.abspath(root)
    return send_from_directory(root, filename)


def _upload_url(name: str) -> str:
    """Return a browser URL for an uploaded filename or pass through absolute/leading-slash URLs."""
    if not name:
        return ""
    s = str(name)
    if s.startswith(("http://", "https://", "/")):
        return s
    return url_for("public.uploads", filename=s)


# ---------- generic helpers

def _to_dt(value):
    """Return a timezone-aware UTC datetime or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None


def _parse_sales_bound(val, kind: str):
    """
    Parse sales_start/sales_end into tz-aware UTC datetimes.
    If val is 'YYYY-MM-DD':
      - kind='start' => 00:00:00 UTC
      - kind='end'   => 23:59:59.999 UTC
    """
    if not val:
        return None
    if isinstance(val, datetime):
        dt = val
    elif isinstance(val, str):
        s = val.strip()
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            try:
                y, m, d = map(int, s.split("-"))
                if kind == "end":
                    dt = datetime(y, m, d, 23, 59, 59, 999000)
                else:
                    dt = datetime(y, m, d, 0, 0, 0, 0)
            except Exception:
                return None
        else:
            try:
                dt = datetime.fromisoformat(s)
            except Exception:
                return None
    else:
        return None

    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _fmt_when(dt):
    dt = _to_dt(dt)
    if not dt:
        return ""
    # Example: Sat, Sep 07 · 19:00
    return dt.astimezone(timezone.utc).strftime("%a, %b %d · %H:%M")


def _ics_stamp(dt):
    """YYYYMMDDTHHMMSSZ for ICS/Google"""
    dt = _to_dt(dt)
    if not dt:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _where_to(location):
    if not location:
        return "Unknown"
    t = (location.get("type") or "").lower()
    if t == "online":
        return "Online"
    venue = (location.get("venue_name") or "").strip()
    city = (location.get("city") or "").strip()
    if venue and city:
        return f"{venue} — {city}"
    return venue or city or "Venue TBA"


def _cover_url(ev):
    """
    Choose a cover image:
      1) explicit cover_url (could be a filename or absolute URL)
      2) first event gallery image
      3) first tier cover_image
      4) stock fallback
    All filenames are served via /uploads/<filename>.
    """
    if ev.get("cover_url"):
        return _upload_url(ev["cover_url"])

    imgs = ev.get("images") or []
    if imgs:
        return _upload_url(imgs[0])

    for t in (ev.get("tiers") or []):
        if t.get("cover_image"):
            return _upload_url(t["cover_image"])

    return "https://images.unsplash.com/photo-1472653816316-3ad6f10a6592?q=80&w=1600&auto=format&fit=crop"


def _from_price(tiers):
    try:
        prices = [float(t.get("price") or 0) for t in (tiers or []) if t.get("price") is not None]
        return min(prices) if prices else None
    except Exception:
        return None


def _page_window(page, pages, width=7):
    if pages <= width:
        return list(range(1, pages + 1))
    half = width // 2
    start = max(1, page - half)
    end = min(pages, start + width - 1)
    start = max(1, end - width + 1)
    return list(range(start, end + 1))


def _tier_sales_open(tier, now_utc):
    s = _parse_sales_bound(tier.get("sales_start"), "start")
    e = _parse_sales_bound(tier.get("sales_end"), "end")
    if s and now_utc < s:
        return False
    if e and now_utc > e:
        return False
    return True


def _tier_availability(event_id_str, tier_idx, tier):
    """Return dict with sold, available (>=0), supply."""
    supply = int(tier.get("supply") or 0)
    sold = 0
    if "tickets" in db.list_collection_names():
        sold = db.tickets.count_documents({"event_id": event_id_str, "tier_index": int(tier_idx)})
    available = max(0, supply - sold) if supply else 0
    return {"supply": supply, "sold": sold, "available": available}


# ---------- LIST: /events

@public_bp.route("/events", methods=["GET"])
def browse_events():
    q = (request.args.get("q") or "").strip()
    cat_param = (request.args.get("category") or "").strip()
    selected_categories = [s for s in [c.strip() for c in cat_param.split(",") if c.strip()] if _SLUG_RE.match(s)]

    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page") or 10)
    except ValueError:
        per_page = 10
    if per_page not in (10, 20, 30, 50):
        per_page = 10

    query = {"status": "published"}
    if q:
        query["$or"] = [
            {"title": {"$regex": re.escape(q), "$options": "i"}},
            {"description": {"$regex": re.escape(q), "$options": "i"}},
        ]
    if selected_categories:
        query["categories"] = {"$in": selected_categories}

    # categories for filters
    all_cats = set()
    if "events" in db.list_collection_names():
        for val in db.events.distinct("categories"):
            if isinstance(val, str) and _SLUG_RE.match(val):
                all_cats.add(val)
        for val in db.events.distinct("category"):
            if isinstance(val, str) and _SLUG_RE.match(val):
                all_cats.add(val)
    categories = sorted(all_cats)

    total = db.events.count_documents(query) if "events" in db.list_collection_names() else 0
    pages = max(1, math.ceil(total / per_page)) if per_page else 1
    if page > pages:
        page = pages

    events = []
    if total:
        cursor = (
            db.events.find(query)
            .sort([("starts_at", 1), ("created_at", -1)])
            .skip((page - 1) * per_page)
            .limit(per_page)
        )
        for ev in cursor:
            events.append({
                "id": str(ev.get("_id")),
                "title": ev.get("title") or "Untitled Event",
                "when": _fmt_when(ev.get("starts_at")),
                "where": _where_to(ev.get("location") or {}),
                "categories": ev.get("categories") or ([ev.get("category")] if ev.get("category") else []),
                "from_price": _from_price(ev.get("tiers")),
                "cover": _cover_url(ev),  # -> /uploads/<filename> or absolute URL
                "url": url_for("public.event_profile", event_id=str(ev.get("_id"))),
            })

    page_nums = _page_window(page, pages, width=7)

    def _qs(**extra):
        base = {
            "q": q or None,
            "category": ",".join(selected_categories) or None,
            "per_page": per_page if per_page != 10 else None,
            "page": page if "page" not in extra else None,
        }
        base.update(extra)
        pairs = [(k, str(v)) for k, v in base.items() if v is not None]
        return url_for("public.browse_events") + ("?" + "&".join(f"{k}={v}" for k, v in pairs) if pairs else "")

    return render_template(
        "events/browse.html",
        q=q,
        categories=categories,
        selected_categories=selected_categories,
        events=events,
        total=total,
        page=page,
        pages=pages,
        page_nums=page_nums,
        per_page=per_page,
        qs=_qs,
        title="Browse Events",
    )


# ---------- DETAIL: /events/<event_id>

@public_bp.route("/events/<event_id>", methods=["GET"])
def event_profile(event_id):
    try:
        _id = ObjectId(event_id)
    except Exception:
        abort(404)

    ev = db.events.find_one({"_id": _id, "status": "published"})
    if not ev:
        abort(404)

    now = datetime.now(timezone.utc)
    starts_at = _to_dt(ev.get("starts_at"))
    ends_at = _to_dt(ev.get("ends_at")) or (starts_at + timedelta(hours=2) if starts_at else None)

    evm = {
        "id": str(ev["_id"]),
        "title": ev.get("title") or "Untitled Event",
        "description": ev.get("description") or "",
        "cover": _cover_url(ev),
        "when": _fmt_when(starts_at),
        "starts_at": starts_at,
        "ends_at": ends_at,
        "where": _where_to(ev.get("location") or {}),
        "location": ev.get("location") or {},
        "categories": ev.get("categories") or ([ev.get("category")] if ev.get("category") else []),
        "organizer_id": ev.get("organizer_id"),
        "images": [_upload_url(img) for img in (ev.get("images") or [])],
    }

    tiers_vm = []
    tiers = ev.get("tiers") or []
    for idx, t in enumerate(tiers):
        price = float(t.get("price") or 0)
        per_order_limit = int(t.get("per_order_limit") or 0)
        avail = _tier_availability(evm["id"], idx, t)
        sales_open = _tier_sales_open(t, now)
        tiers_vm.append({
            "idx": idx,
            "name": t.get("name") or f"Tier {idx+1}",
            "price": price,
            "per_order_limit": per_order_limit,
            "refundable": bool(t.get("refundable")),
            "available": avail["available"],
            "supply": avail["supply"],
            "sold": avail["sold"],
            "sales_open": sales_open,
        })

    # pick first tier that is on sale and has stock
    first_open_idx = None
    for t in tiers_vm:
        if t["sales_open"] and t["available"] > 0:
            first_open_idx = t["idx"]
            break

    # default quantity max for the first_open tier (min of per-order, available, 5)
    default_qty_max = 1
    if first_open_idx is not None:
        t = next(tt for tt in tiers_vm if tt["idx"] == first_open_idx)
        hard_cap = t["per_order_limit"] or t["available"]
        default_qty_max = max(1, min(5, hard_cap, t["available"]))

    # Calendar link
    dt_start = _ics_stamp(evm["starts_at"])
    dt_end = _ics_stamp(evm["ends_at"])
    gcal_params = {
        "action": "TEMPLATE",
        "text": evm["title"],
        "dates": f"{dt_start}/{dt_end}",
        "details": url_for("public.event_profile", event_id=evm["id"], _external=True),
        "location": evm["where"],
    }
    gcal_url = "https://calendar.google.com/calendar/render?" + urlencode(gcal_params, quote_via=quote_plus)

    return render_template(
        "events/event_profile.html",
        ev=evm,
        tiers=tiers_vm,
        gcal_url=gcal_url,
        first_open_idx=first_open_idx,
        default_qty_max=default_qty_max,
    )


# ---------- ICS: /events/<event_id>/ics

@public_bp.route("/events/<event_id>/ics", methods=["GET"])
def event_ics(event_id):
    try:
        _id = ObjectId(event_id)
    except Exception:
        abort(404)
    ev = db.events.find_one({"_id": _id, "status": "published"})
    if not ev:
        abort(404)

    title = (ev.get("title") or "Event").replace("\n", " ")
    start = _to_dt(ev.get("starts_at"))
    end = _to_dt(ev.get("ends_at")) or (start + timedelta(hours=2) if start else None)
    where = _where_to(ev.get("location") or {})
    if not start or not end:
        abort(400)

    uid = f"{event_id}@akwaaba"
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//AkwaabaTickets//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{_ics_stamp(datetime.utcnow())}\r\n"
        f"DTSTART:{_ics_stamp(start)}\r\n"
        f"DTEND:{_ics_stamp(end)}\r\n"
        f"SUMMARY:{title}\r\n"
        f"LOCATION:{where}\r\n"
        f"DESCRIPTION:{(ev.get('description') or '').replace('\\n',' ')}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    resp = make_response(ics, 200)
    resp.headers["Content-Type"] = "text/calendar; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="{title.replace(" ", "_")}.ics"'
    return resp


# ---------- BUY: /events/<event_id>/buy (POST)

@public_bp.route("/events/<event_id>/buy", methods=["POST"])
def buy_tickets(event_id):
    # Require login
    if "uid" not in session:
        flash("Please log in to buy tickets.", "warning")
        return redirect(url_for("login.login"))

    try:
        _id = ObjectId(event_id)
    except Exception:
        abort(404)

    ev = db.events.find_one({"_id": _id, "status": "published"})
    if not ev:
        abort(404)

    # Parse form
    try:
        tier_idx = int(request.form.get("tier_idx") or -1)
        qty = int(request.form.get("quantity") or 0)
    except Exception:
        flash("Invalid selection.", "danger")
        return redirect(url_for("public.event_profile", event_id=event_id))

    tiers = ev.get("tiers") or []
    if tier_idx < 0 or tier_idx >= len(tiers):
        flash("Please select a valid ticket tier.", "danger")
        return redirect(url_for("public.event_profile", event_id=event_id))
    tier = tiers[tier_idx]

    # Validate sales window and availability (same as checkout does)
    now = datetime.now(timezone.utc)
    s = _parse_sales_bound(tier.get("sales_start"), "start")
    e = _parse_sales_bound(tier.get("sales_end"), "end")
    if (s and now < s) or (e and now > e):
        flash("Sales window closed for this tier.", "danger")
        return redirect(url_for("public.event_profile", event_id=event_id))

    supply = int(tier.get("supply") or 0)
    sold = 0
    if "tickets" in db.list_collection_names():
        sold = db.tickets.count_documents({"event_id": str(ev["_id"]), "tier_index": int(tier_idx)})
    avail = max(0, supply - sold) if supply else 0

    # Allow up to per_order_limit if set, else up to availability
    per_order = int(tier.get("per_order_limit") or 0) or avail

    if qty <= 0 or qty > per_order or qty > avail:
        flash(f"Quantity must be between 1 and {min(per_order, avail)}.", "danger")
        return redirect(url_for("public.event_profile", event_id=event_id))

    # unified: go to checkout.start with the same params
    return redirect(url_for("checkout.start", event_id=event_id, tier_idx=tier_idx, quantity=qty))
