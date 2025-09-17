from datetime import datetime, timedelta
from collections import defaultdict
from bson import ObjectId
from flask import request, render_template, session, abort, jsonify, flash
from db import db
from organizer import organizer_bp  # existing blueprint

def _require_login() -> bool:
    if "uid" not in session:
        flash("Please log in to continue.", "warning")
        return False
    return True

def _to_oid(maybe_id):
    try:
        return ObjectId(str(maybe_id))
    except Exception:
        return None

def _safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

@organizer_bp.route("/attendees", methods=["GET"])
def attendees_page():
    if not _require_login():
        from flask import redirect, url_for
        return redirect(url_for("login.login"))

    organizer_id = session.get("uid")
    if not organizer_id:
        abort(401)

    query = {"organizer_id": organizer_id}
    if "events" in db.list_collection_names():
        events = list(db.events.find(
            query,
            {"_id": 1, "title": 1, "status": 1, "starts_at": 1, "tiers": 1}
        ).sort([("starts_at", -1), ("_id", -1)]))
    else:
        events = []

    selected_event_id = request.args.get("event_id") or (str(events[0]["_id"]) if events else "")
    return render_template("organizer/attendees.html", events=events, selected_event_id=selected_event_id)

@organizer_bp.route("/attendees/data", methods=["GET"])
def attendees_data():
    """
    Returns:
      totals: tickets_sold, unique_attendees, revenue_usdc
      by_tier: [{index,name,price,sold,supply}]
      timeseries: [{date,sold}]  # last 14 calendar days, zero-filled
      recent: [{
        attendee_id, full_name, email, phone,
        tier_index, tier_name, price, purchased_at
      }]
    """
    if "uid" not in session:
        return jsonify({"ok": False, "error": "not_logged_in"}), 401

    organizer_id = session.get("uid")
    eid = request.args.get("event_id") or ""
    oid = _to_oid(eid)
    if not oid:
        return jsonify({"ok": False, "error": "bad_event"}), 400

    ev = db.events.find_one({"_id": oid, "organizer_id": organizer_id}) or {}
    if not ev:
        return jsonify({"ok": False, "error": "not_found"}), 404

    tiers = ev.get("tiers") or []
    tier_name_map = {i: (t.get("name") or f"Tier {i+1}") for i, t in enumerate(tiers)}

    # ---- Tickets aggregate ----
    tickets_query = {"event_id": str(oid)}
    tickets_sold = 0
    unique_attendees = set()
    by_tier_count = defaultdict(int)
    timeseries_map = defaultdict(int)
    recent_rows = []

    if "tickets" in db.list_collection_names():
        cursor = db.tickets.find(
            tickets_query,
            {"attendee_id": 1, "tier_index": 1, "purchased_at": 1, "price": 1}
        ).sort([("purchased_at", -1)])

        for t in cursor:
            tickets_sold += 1
            aid = t.get("attendee_id")
            if aid:
                unique_attendees.add(aid)

            ti = _safe_int(t.get("tier_index"), -1)
            by_tier_count[ti] += 1

            dt = t.get("purchased_at")
            if isinstance(dt, datetime):
                timeseries_map[dt.date().isoformat()] += 1

            recent_rows.append({
                "attendee_id": aid,
                "tier_index": ti,
                "purchased_at": t.get("purchased_at"),
                "price": float(t.get("price") or 0.0),
            })

    # ---- Revenue (USDC) ----
    revenue_usdc = 0.0
    if "payments" in db.list_collection_names():
        for p in db.payments.find(
            {"event_id": str(oid), "currency": "USDC", "status": {"$in": ["paid", "onchain_confirmed"]}},
            {"amount": 1}
        ):
            try:
                revenue_usdc += float(p.get("amount") or 0.0)
            except Exception:
                pass

    # ---- by_tier ----
    by_tier = []
    for idx, tier in enumerate(tiers):
        by_tier.append({
            "index": idx,
            "name": tier_name_map[idx],
            "price": float(tier.get("price") or 0.0),
            "sold": by_tier_count.get(idx, 0),
            "supply": _safe_int(tier.get("supply") or 0),
        })
    if by_tier_count.get(-1, 0):
        by_tier.append({"index": -1, "name": "Unknown Tier", "price": 0.0, "sold": by_tier_count[-1], "supply": 0})

    # ---- timeseries (fixed last 14 days, zero-filled, ascending) ----
    days = 14
    today = datetime.utcnow().date()
    start_day = today - timedelta(days=days-1)
    timeseries = []
    for i in range(days):
        d = (start_day + timedelta(days=i)).isoformat()
        timeseries.append({"date": d, "sold": int(timeseries_map.get(d, 0))})

    # ---- user enrichment (name/email/phone) ----
    if recent_rows and "users" in db.list_collection_names():
        recent_rows = recent_rows[:100]  # limit
        ids = [r["attendee_id"] for r in recent_rows if r.get("attendee_id")]
        ids = list({i for i in ids if i})
        users_map = {}
        if ids:
            oids = [_to_oid(i) for i in ids if _to_oid(i)]
            if oids:
                users_cur = db.users.find(
                    {"_id": {"$in": oids}},
                    {"full_name": 1, "email": 1, "phone": 1}
                )
                for u in users_cur:
                    users_map[str(u["_id"])] = {
                        "full_name": (u.get("full_name") or "").strip(),
                        "email": (u.get("email") or "").strip(),
                        "phone": (u.get("phone") or "").strip(),
                    }

        for r in recent_rows:
            u = users_map.get(str(r.get("attendee_id"))) or {}
            r["full_name"] = u.get("full_name") or "Attendee"
            r["email"] = u.get("email") or ""
            r["phone"] = u.get("phone") or ""
            r["tier_name"] = tier_name_map.get(r.get("tier_index"), "Unknown Tier")

    payload = {
        "ok": True,
        "event": {
            "id": str(ev["_id"]),
            "title": ev.get("title") or "Untitled Event",
            "status": ev.get("status") or "draft",
        },
        "totals": {
            "tickets_sold": tickets_sold,
            "unique_attendees": len(unique_attendees),
            "revenue_usdc": round(revenue_usdc, 6),
        },
        "by_tier": by_tier,
        "timeseries": timeseries,   # always 14 points, zero-filled
        "recent": [
            {
                "attendee_id": r.get("attendee_id"),
                "full_name": r.get("full_name"),
                "email": r.get("email"),
                "phone": r.get("phone"),
                "tier_index": r.get("tier_index"),
                "tier_name": r.get("tier_name"),
                "price": r.get("price"),
                "purchased_at": (
                    r.get("purchased_at").isoformat()
                    if isinstance(r.get("purchased_at"), datetime) else ""
                ),
            } for r in recent_rows
        ]
    }
    return jsonify(payload)
