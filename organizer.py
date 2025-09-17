# organizer.py
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from flask import (
    Blueprint, render_template, session, redirect, url_for,
    flash, abort, Response, current_app, send_from_directory, request
)
from db import db
import io, csv
import os

organizer_bp = Blueprint("organizer", __name__, url_prefix="/organizer")

# -------------------------
# Helpers
# -------------------------
def _to_oid(val):
    try:
        return ObjectId(str(val))
    except Exception:
        return None

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

def _event_where(ev):
    loc = (ev.get("location") or {})
    t = (loc.get("type") or "venue").lower()
    if t == "venue":
        parts = [loc.get("venue_name"), loc.get("address"), loc.get("city")]
        return ", ".join([p for p in parts if p])
    if t == "online":
        return loc.get("online_url") or "Online"
    return ""

def _looks_like_url(s: str) -> bool:
    s = (s or "").lower().strip()
    return s.startswith("http://") or s.startswith("https://") or s.startswith("//")

def _img_url(filename):
    """
    Build a URL for an uploaded image or pass-through absolute URLs.
    Expects files saved under current_app.config['UPLOAD_FOLDER'].
    """
    if not filename:
        return None
    if _looks_like_url(filename):
        return filename
    filename = str(filename).lstrip("/\\")
    return url_for("organizer.media", filename=filename)

# -------------------------
# Serve files from UPLOAD_FOLDER (safe)
# -------------------------
@organizer_bp.route("/media/<path:filename>")
def media(filename):
    """
    Serves files from UPLOAD_FOLDER set in app config.
    Example: app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'uploads')
    """
    folder = current_app.config.get("UPLOAD_FOLDER")
    if not folder:
        abort(404)
    filename = filename.lstrip("/\\")
    return send_from_directory(folder, filename, as_attachment=False)

# -------------------------
# Dashboard
# -------------------------
@organizer_bp.route("/dashboard", methods=["GET"])
def dashboard():
    if "uid" not in session:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("login.login"))

    organizer_id = session["uid"]
    oid = _to_oid(organizer_id)
    id_variants = [organizer_id] + ([oid] if oid else [])

    events_cur = db.events.find(
        {"organizer_id": {"$in": id_variants}},
        {"_id": 1, "title": 1, "status": 1, "starts_at": 1, "location": 1, "images": 1}
    )
    events = list(events_cur)
    event_ids = [str(e["_id"]) for e in events]

    now = datetime.now(timezone.utc)
    total_events = len(events)

    def _is_upcoming(ev):
        s = _to_utc(ev.get("starts_at"))
        return (ev.get("status") == "published") and s and s >= now

    upcoming_events = sum(1 for e in events if _is_upcoming(e))

    tickets_sold = 0
    if event_ids and "tickets" in db.list_collection_names():
        tickets_sold = db.tickets.count_documents({
            "event_id": {"$in": event_ids},
        })

    revenue = 0.0
    if event_ids and "tickets" in db.list_collection_names():
        rev_from_tickets_pipeline = [
            {"$match": {"event_id": {"$in": event_ids}}},
            {"$group": {"_id": None, "sum": {"$sum": "$price"}}}
        ]
        rev_tick = list(db.tickets.aggregate(rev_from_tickets_pipeline))
        revenue = float(rev_tick[0]["sum"]) if rev_tick else 0.0

    if revenue == 0.0 and "transactions" in db.list_collection_names():
        tx_match = {
            "organizer_id": {"$in": id_variants},
            "kind": "ticket_purchase",
            "currency": "USDC",
        }
        rev_pipeline = [
            {"$match": tx_match},
            {"$group": {"_id": None, "sum": {"$sum": "$amount"}}}
        ]
        rev_doc = list(db.transactions.aggregate(rev_pipeline))
        if rev_doc:
            revenue = float(rev_doc[0]["sum"]) or 0.0

    since = now - timedelta(days=13)
    labels = []
    sales_series = []
    daily_counts = {(since + timedelta(days=i)).date().isoformat(): 0 for i in range(14)}

    if event_ids and "tickets" in db.list_collection_names():
        pipeline = [
            {"$match": {"event_id": {"$in": event_ids}, "purchased_at": {"$gte": since}}},
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$purchased_at"}}, "count": {"$sum": 1}}},
            {"$sort": {"_id": 1}}
        ]
        for d in db.tickets.aggregate(pipeline):
            daily_counts[d["_id"]] = d["count"]

    for i in range(14):
        day = (since + timedelta(days=i)).date().isoformat()
        labels.append(day)
        sales_series.append(daily_counts.get(day, 0))

    recent = []
    if "transactions" in db.list_collection_names():
        tx_cur = db.transactions.find(
            {"organizer_id": {"$in": id_variants}}
        ).sort("created_at", -1).limit(8)
        for t in tx_cur:
            recent.append({
                "kind": t.get("kind") or "activity",
                "amount": t.get("amount"),
                "currency": t.get("currency") or "USDC",
                "event_id": t.get("event_id"),
                "tx_hash": t.get("tx_hash"),
                "created_at": _to_utc(t.get("created_at")),
                "base_status": t.get("base_status"),
            })

    upcoming_list = []
    for e in events:
        if _is_upcoming(e):
            upcoming_list.append({
                "id": str(e["_id"]),
                "title": e.get("title") or "Untitled",
                "starts_at": _to_utc(e.get("starts_at")),
                "status": e.get("status"),
                "location": e.get("location")
            })
    upcoming_list.sort(key=lambda x: x["starts_at"] or now)
    upcoming_list = upcoming_list[:5]

    vm = {
        "name": session.get("name") or "Organizer",
        "stats": {
            "total_events": total_events,
            "upcoming_events": upcoming_events,
            "tickets_sold": int(tickets_sold),
            "revenue": revenue
        },
        "chart": {"labels": labels, "series": sales_series},
        "recent": recent,
        "upcoming": upcoming_list
    }

    return render_template("organizer/dashboard.html", vm=vm)

# -------------------------
# Organizer -> Tickets overview
# -------------------------
@organizer_bp.route("/tickets", methods=["GET"])
def organizer_tickets():
    if "uid" not in session:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("login.login"))

    organizer_id = session["uid"]
    oid = _to_oid(organizer_id)
    id_variants = [organizer_id] + ([oid] if oid else [])

    events = list(db.events.find(
        {"organizer_id": {"$in": id_variants}},
        {"_id": 1, "title": 1, "status": 1, "starts_at": 1, "location": 1, "images": 1, "tiers": 1}
    ).sort("starts_at", 1))

    if not events:
        return render_template("organizer/tickets.html", cards=[], empty=True)

    event_ids = []
    for ev in events:
        if not isinstance(ev.get("images"), list):
            ev["images"] = []
        event_ids.append(str(ev["_id"]))

    cards = []
    if "tickets" in db.list_collection_names() and event_ids:
        pipeline = [
            {"$match": {"event_id": {"$in": event_ids}}},
            {"$group": {
                "_id": {"event_id": "$event_id", "tier_index": "$tier_index"},
                "sold": {"$sum": 1},
                "revenue": {"$sum": "$price"}
            }},
            {"$sort": {"_id.event_id": 1, "_id.tier_index": 1}}
        ]
        agg = list(db.tickets.aggregate(pipeline))
        sold_map = {(row["_id"]["event_id"], int(row["_id"]["tier_index"] or 0)): {
            "sold": int(row.get("sold") or 0),
            "revenue": float(row.get("revenue") or 0.0)
        } for row in agg}

        for ev in events:
            ev_id = str(ev["_id"])
            tiers = ev.get("tiers") or []
            for idx, t in enumerate(tiers):
                supply = int(t.get("supply") or 0)
                price = float(t.get("price") or 0.0)
                sold = sold_map.get((ev_id, idx), {}).get("sold", 0)
                revenue = sold_map.get((ev_id, idx), {}).get("revenue", 0.0)
                left = max(0, supply - sold)
                img_url = _img_url(ev["images"][0]) if ev.get("images") else None
                cards.append({
                    "event_id": ev_id,
                    "event_title": ev.get("title") or "Untitled Event",
                    "event_img": img_url,
                    "when": (_to_utc(ev.get("starts_at")).strftime("%a, %b %d · %H:%M UTC")
                             if _to_utc(ev.get("starts_at")) else "TBA"),
                    "where": _event_where(ev) or "—",
                    "tier_idx": idx,
                    "tier_name": t.get("name") or f"Tier {idx+1}",
                    "price": price,
                    "supply": supply,
                    "sold": sold,
                    "left": left,
                    "revenue": round(revenue, 6),
                })
    else:
        for ev in events:
            ev_id = str(ev["_id"])
            tiers = ev.get("tiers") or []
            for idx, t in enumerate(tiers):
                supply = int(t.get("supply") or 0)
                price = float(t.get("price") or 0.0)
                img_url = _img_url(ev["images"][0]) if ev.get("images") else None
                cards.append({
                    "event_id": ev_id,
                    "event_title": ev.get("title") or "Untitled Event",
                    "event_img": img_url,
                    "when": (_to_utc(ev.get("starts_at")).strftime("%a, %b %d · %H:%M UTC")
                             if _to_utc(ev.get("starts_at")) else "TBA"),
                    "where": _event_where(ev) or "—",
                    "tier_idx": idx,
                    "tier_name": t.get("name") or f"Tier {idx+1}",
                    "price": price,
                    "supply": supply,
                    "sold": 0,
                    "left": supply,
                    "revenue": 0.0,
                })

    def _sort_key(card):
        ev = next((e for e in events if str(e["_id"]) == card["event_id"]), None)
        s = _to_utc(ev.get("starts_at")) if ev else None
        return (s is None, s or datetime.max.replace(tzinfo=timezone.utc), card["tier_idx"])

    cards.sort(key=_sort_key)

    return render_template("organizer/tickets.html", cards=cards, empty=False)

# -------------------------
# Tier buyers page
# -------------------------
@organizer_bp.route("/tickets/<event_id>/<int:tier_idx>", methods=["GET"])
def organizer_tickets_tier(event_id, tier_idx):
    if "uid" not in session:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("login.login"))

    ev = db.events.find_one({"_id": _to_oid(event_id)})
    if not ev:
        abort(404)

    tiers = ev.get("tiers") or []
    if tier_idx < 0 or tier_idx >= len(tiers):
        abort(404)

    tier = tiers[tier_idx]
    supply = int(tier.get("supply") or 0)
    tier_name = tier.get("name") or f"Tier {tier_idx+1}"

    q = {"event_id": event_id, "tier_index": int(tier_idx)}
    fields = {"_id": 1, "attendee_id": 1, "price": 1, "purchased_at": 1, "status": 1, "payment_id": 1}
    cur = db.tickets.find(q, fields).sort("purchased_at", -1)

    rows, sold, revenue = [], 0, 0.0
    for t in cur:
        sold += 1
        p = float(t.get("price") or 0.0)
        revenue += p
        rows.append({
            "ticket_id": str(t["_id"]),
            "attendee_id": t.get("attendee_id"),
            "price": p,
            "purchased_at": _to_utc(t.get("purchased_at")),
            "status": t.get("status") or "valid",
            "payment_id": t.get("payment_id")
        })

    left = max(0, supply - sold)

    img = None
    if isinstance(ev.get("images"), list) and ev["images"]:
        img = _img_url(ev["images"][0])

    vm = {
        "event_id": event_id,
        "event_title": ev.get("title") or "Untitled Event",
        "event_img": img,
        "when": (_to_utc(ev.get("starts_at")).strftime("%a, %b %d · %H:%M UTC")
                 if _to_utc(ev.get("starts_at")) else "TBA"),
        "where": _event_where(ev) or "—",
        "tier_idx": tier_idx,
        "tier_name": tier_name,
        "supply": supply,
        "sold": sold,
        "left": left,
        "revenue": round(revenue, 6),
        "rows": rows
    }

    return render_template("organizer/tickets_tier.html", vm=vm)

# -------------------------
# CSV export for a tier
# -------------------------
@organizer_bp.route("/tickets/<event_id>/<int:tier_idx>/export.csv", methods=["GET"])
def export_tier_csv(event_id, tier_idx):
    if "uid" not in session:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("login.login"))

    ev = db.events.find_one({"_id": _to_oid(event_id)})
    if not ev:
        abort(404)

    tiers = ev.get("tiers") or []
    if tier_idx < 0 or tier_idx >= len(tiers):
        abort(404)

    q = {"event_id": event_id, "tier_index": int(tier_idx)}
    fields = {"_id": 1, "attendee_id": 1, "price": 1, "purchased_at": 1, "status": 1, "payment_id": 1}
    cur = db.tickets.find(q, fields).sort("purchased_at", -1)

    sio = io.StringIO()
    w = csv.writer(sio)
    tier_name = (tiers[tier_idx].get("name") or f"Tier {tier_idx+1}")
    w.writerow(["event_title", ev.get("title") or "Untitled Event"])
    w.writerow(["tier", tier_name])
    w.writerow([])
    w.writerow(["ticket_id", "attendee_id", "price_usdc", "purchased_at_utc", "status", "payment_id"])

    for t in cur:
        purchased_at = _to_utc(t.get("purchased_at"))
        w.writerow([
            str(t["_id"]),
            t.get("attendee_id") or "",
            float(t.get("price") or 0.0),
            purchased_at.strftime("%Y-%m-%d %H:%M:%S UTC") if purchased_at else "",
            t.get("status") or "",
            t.get("payment_id") or "",
        ])

    out = sio.getvalue()
    fname = f"{(ev.get('title') or 'event').replace(' ', '_')}_tier_{tier_idx+1}_tickets.csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Content-Type": "text/csv; charset=utf-8",
        "Cache-Control": "no-store",
    }
    return Response(out, headers=headers)

# -------------------------
# Events list with pagination
# -------------------------
