# attendee_tickets.py
from datetime import datetime, timedelta, timezone
from bson import ObjectId
from flask import Blueprint, render_template, session, redirect, url_for, flash, request, current_app
from db import db

attendee_bp = Blueprint("attendee", __name__, url_prefix="/attendee")  # reuse if you already have it

# --- tiny helpers (aligned with your public.py) ---
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

def _fmt_when(dt):
    dt = _to_dt(dt)
    if not dt:
        return ""
    return dt.astimezone(timezone.utc).strftime("%a, %b %d · %H:%M")

def _upload_url(name: str) -> str:
    if not name:
        return ""
    s = str(name)
    if s.startswith(("http://", "https://", "/")):
        return s
    # served by public.uploads
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

def _where_to(location):
    if not location:
        return "Unknown"
    t = (location.get("type") or "").lower()
    if t == "online":
        return "Online"
    venue = (location.get("venue_name") or "").strip()
    city  = (location.get("city") or "").strip()
    if venue and city:
        return f"{venue} — {city}"
    return venue or city or "Venue TBA"

@attendee_bp.route("/tickets", methods=["GET"])
def my_tickets():
    # auth
    if "uid" not in session:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("login.login"))

    uid = session["uid"]
    q = (request.args.get("q") or "").strip().lower()

    # fetch all tickets for user
    tickets = list(db.tickets.find({"attendee_id": uid}).sort([("purchased_at", -1)]))
    if not tickets:
        return render_template("attendee/tickets.html",
                               name=session.get("name") or "Attendee",
                               upcoming=[],
                               past=[],
                               q=q)

    # gather events
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

        # match search (title or city/venue)
        hay = " ".join([
            str(ev.get("title") or ""),
            str((ev.get("location") or {}).get("venue_name") or ""),
            str((ev.get("location") or {}).get("city") or ""),
        ]).lower()
        if q and q not in hay:
            continue

        # tier name/price (from event model)
        tier_idx = int(t.get("tier_index") or 0)
        etiers = ev.get("tiers") or []
        tier = etiers[tier_idx] if 0 <= tier_idx < len(etiers) else {}
        tier_name = tier.get("name") or f"Tier {tier_idx+1}"
        unit_price = float(t.get("price") or tier.get("price") or 0.0)

        vm = {
            "ticket_id": str(t.get("_id")),
            "payment_id": t.get("payment_id"),
            "status": t.get("status") or "valid",
            "qty": 1,  # one ticket per row in your issuer loop
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

        # split
        if ends_at and ends_at >= now:
            upcoming.append(vm)
        else:
            past.append(vm)

    # sort: upcoming by start asc, past by end desc
    upcoming.sort(key=lambda x: (x["event"]["starts_at"] or now))
    past.sort(key=lambda x: (x["event"]["ends_at"] or now), reverse=True)

    return render_template("attendee/tickets.html",
                           name=session.get("name") or "Attendee",
                           upcoming=upcoming,
                           past=past,
                           q=q)
