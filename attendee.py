# attendee.py
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, session, redirect, url_for, flash, request
from bson import ObjectId
from db import db

attendee_bp = Blueprint("attendee", __name__, url_prefix="/attendee")

# ---------- helpers ----------
def _to_utc(dt):
    if not dt:
        return None
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(dt))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _fmt_when(dt):
    d = _to_utc(dt)
    if not d:
        return ""
    return d.astimezone(timezone.utc).strftime("%a, %b %d · %H:%M")

def _where_to(loc):
    if not loc:
        return "Unknown"
    t = (loc.get("type") or "").lower()
    if t == "online":
        return "Online"
    venue = (loc.get("venue_name") or "").strip()
    city = (loc.get("city") or "").strip()
    if venue and city:
        return f"{venue} — {city}"
    return venue or city or "Venue TBA"

def _to_dt(value):
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

# ============================================
# Dashboard
# ============================================
@attendee_bp.route("/dashboard", methods=["GET"])
def attendee_dashboard():
    # Auth guard
    if "uid" not in session:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("login.login"))

    uid = session["uid"]                     # NOTE: stored as STRING in tickets/payments
    name = session.get("name") or "Attendee"
    now = datetime.now(timezone.utc)

    # ---- Tickets for this attendee (FIX: attendee_id, not user_id)
    tickets = list(
        db.tickets.find({"attendee_id": uid}).sort([("purchased_at", -1)]).limit(500)
    )

    # ---- Events referenced by tickets
    event_ids = []
    for t in tickets:
        e = t.get("event_id")
        if not e:
            continue
        try:
            event_ids.append(ObjectId(str(e)))
        except Exception:
            pass
    events = {}
    if event_ids:
        for ev in db.events.find({"_id": {"$in": list(set(event_ids))}}):
            events[str(ev["_id"])] = ev

    # ---- Total spent from payments (more accurate than summing tickets)
    total_spent = 0.0
    for p in db.payments.find({"attendee_id": uid}):
        try:
            total_spent += float(p.get("amount") or 0.0)
        except Exception:
            pass

    # ---- Build table rows and counts
    vm_rows = []
    upcoming_count, past_count = 0, 0
    for t in tickets:
        eid_str = str(t.get("event_id") or "")
        ev = events.get(eid_str)
        if not ev:
            continue

        starts_at = _to_dt(ev.get("starts_at"))
        ends_at = _to_dt(ev.get("ends_at")) or (starts_at + timedelta(hours=2) if starts_at else None)

        # tier label/price
        tier_idx = int(t.get("tier_index") or 0)
        etiers = ev.get("tiers") or []
        tier = etiers[tier_idx] if 0 <= tier_idx < len(etiers) else {}
        tier_name = tier.get("name") or f"Tier {tier_idx+1}"

        # each ticket doc represents ONE ticket
        qty = 1
        price = float(t.get("price") or tier.get("price") or 0.0)
        status = (t.get("status") or "valid").title()

        is_upcoming = bool(ends_at and ends_at >= now)
        if is_upcoming:
            upcoming_count += 1
        else:
            past_count += 1

        vm_rows.append({
            "title": ev.get("title") or "Untitled Event",
            "when": _fmt_when(starts_at),
            "where": _where_to(ev.get("location") or {}),
            "tier": tier_name,
            "qty": qty,
            "total": f"{price:,.2f}",
            "status": status,
            "is_upcoming": is_upcoming,
            "url": url_for("public.event_profile", event_id=eid_str),
        })

    # ---- Recent attendance (check-ins). Support either user_id or attendee_id in your docs.
    recent_attendance = []
    if "checkins" in db.list_collection_names():
        checks = db.checkins.find({
            "$or": [{"attendee_id": uid}, {"user_id": uid}]
        }).sort([("scanned_at", -1)]).limit(5)
        for c in checks:
            ev_id = str(c.get("event_id") or "")
            ev = events.get(ev_id) or db.events.find_one({"_id": ObjectId(ev_id)}) if ev_id else None
            recent_attendance.append({
                "title": (ev.get("title") if ev else "Event"),
                "when": _fmt_when(ev.get("starts_at") if ev else None),
                "where": _where_to(ev.get("location") or {}) if ev else "Unknown",
                "url": url_for("public.event_profile", event_id=ev_id) if ev_id else "#",
                "scanned_at": _fmt_when(c.get("scanned_at")),
            })

    last_attended_label = recent_attendance[0]["title"] if recent_attendance else "—"

    return render_template(
        "attendee/dashboard.html",
        name=name,
        tickets=vm_rows,
        upcoming_count=upcoming_count,
        past_count=past_count,
        total_spent=f"{total_spent:,.2f}",
        last_attended_label=last_attended_label,
        recent_attendance=recent_attendance,
        title="Your Tickets",
    )

# ============================================
# Tickets listing (unchanged from your working version, but aligned)
# ============================================
def _upload_url(name: str) -> str:
    if not name:
        return ""
    s = str(name)
    if s.startswith(("http://", "https://", "/")):
        return s
    return url_for("public.uploads", filename=s)

def _cover_url(ev):
    if ev.get("cover_url"):
        return _upload_url(ev["cover_url"])
    imgs = ev.get("images") or []
    if imgs:
        return _upload_url(imgs[0])
    for t in (ev.get("tiers") or []):
        if t.get("cover_image"):
            return _upload_url(t["cover_image"])
    return "https://images.unsplash.com/photo-1472653816316-3ad6f10a6592?q=80&w=1600&auto=format&fit=crop"

@attendee_bp.route("/tickets", methods=["GET"])
def my_tickets():
    if "uid" not in session:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("login.login"))

    uid = session["uid"]
    q = (request.args.get("q") or "").strip().lower()

    tickets = list(db.tickets.find({"attendee_id": uid}).sort([("purchased_at", -1)]))
    if not tickets:
        return render_template("attendee/tickets.html",
                               name=session.get("name") or "Attendee",
                               upcoming=[],
                               past=[],
                               q=q)

    ev_ids = list({ObjectId(t["event_id"]) for t in tickets if t.get("event_id")})
    events = { str(e["_id"]): e for e in db.events.find({"_id": {"$in": ev_ids}}) }

    now = datetime.now(timezone.utc)
    upcoming, past = [], []

    for t in tickets:
        ev = events.get(t.get("event_id"))
        if not ev:
            continue

        starts_at = _to_dt(ev.get("starts_at"))
        ends_at = _to_dt(ev.get("ends_at")) or (starts_at + timedelta(hours=2) if starts_at else None)

        hay = " ".join([
            str(ev.get("title") or ""),
            str((ev.get("location") or {}).get("venue_name") or ""),
            str((ev.get("location") or {}).get("city") or ""),
        ]).lower()
        if q and q not in hay:
            continue

        tier_idx = int(t.get("tier_index") or 0)
        etiers = ev.get("tiers") or []
        tier = etiers[tier_idx] if 0 <= tier_idx < len(etiers) else {}
        tier_name = tier.get("name") or f"Tier {tier_idx+1}"
        unit_price = float(t.get("price") or tier.get("price") or 0.0)

        vm = {
            "ticket_id": str(t.get("_id")),
            "payment_id": t.get("payment_id"),
            "status": t.get("status") or "valid",
            "qty": 1,
            "purchased_at": t.get("purchased_at"),
            "event": {
                "id": str(ev["_id"]),
                "title": ev.get("title") or "Untitled Event",
                "cover": _cover_url(ev),
                "where": _where_to(ev.get("location") or {}),
                "when": _fmt_when(starts_at),
                "starts_at": starts_at,
                "ends_at": ends_at,
                "url": url_for("public.event_profile", event_id=str(ev["_id"])),
                "ics_url": url_for("public.event_ics", event_id=str(ev["_id"])) if starts_at and ends_at else None,
            },
            "tier": {
                "name": tier_name,
                "price": unit_price,
                "index": tier_idx,
            }
        }

        (upcoming if (ends_at and ends_at >= now) else past).append(vm)

    upcoming.sort(key=lambda x: (x["event"]["starts_at"] or now))
    past.sort(key=lambda x: (x["event"]["ends_at"] or now), reverse=True)

    return render_template("attendee/tickets.html",
                           name=session.get("name") or "Attendee",
                           upcoming=upcoming,
                           past=past,
                           q=q)
