# templates used: templates/attendee/transactions.html
# URL: /attendee/transactions  (filters & pagination)
from datetime import datetime
from bson import ObjectId
from flask import Blueprint, render_template, request, session, redirect, url_for, flash, Response
from db import db
import csv, io, math

NETS_BY_CHAIN = {
    8453:  {"name": "Base",         "explorer": "https://basescan.org"},
    84532: {"name": "Base Sepolia", "explorer": "https://sepolia.basescan.org"},
}

attendee_tx_bp = Blueprint("attendee_tx", __name__, url_prefix="/attendee")

def _require_login():
    if "uid" not in session:
        flash("Please log in to continue.", "warning")
        return False
    return True

def _to_oid(v):
    try: return ObjectId(str(v))
    except: return None

def _basescan_tx_url(chain_id: int, tx_hash: str | None) -> str | None:
    if not tx_hash: return None
    base = NETS_BY_CHAIN.get(int(chain_id or 0), {}).get("explorer")
    return f"{base}/tx/{tx_hash}" if base else None

def _clean_int(v, default=0, minimum=None, maximum=None):
    try: n = int(v)
    except: n = default
    if minimum is not None: n = max(minimum, n)
    if maximum is not None: n = min(maximum, n)
    return n

@attendee_tx_bp.route("/transactions", methods=["GET"])
def transactions():
    if not _require_login():
        return redirect(url_for("login.login"))

    attendee_id = session["uid"]
    q         = (request.args.get("q") or "").strip()
    status_f  = (request.args.get("status") or "").strip().lower()
    kind_f    = (request.args.get("kind") or "").strip()
    chain_f   = _clean_int(request.args.get("chain_id"), default=0)
    date_from = (request.args.get("from") or "").strip()
    date_to   = (request.args.get("to") or "").strip()
    page      = _clean_int(request.args.get("page"), default=1, minimum=1)
    per_page  = _clean_int(request.args.get("per"), default=20, minimum=5, maximum=100)

    where = {"attendee_id": attendee_id}
    if status_f:
        where["$or"] = [{"base_status": status_f}, {"status": status_f}]
    if kind_f:
        where["kind"] = kind_f
    if chain_f:
        where["chain_id"] = chain_f

    created_range = {}
    def _parse_date(s, end=False):
        try:
            y, m, d = [int(x) for x in s.split("-")]
            return datetime(y, m, d, 23, 59, 59, 999000) if end else datetime(y, m, d)
        except: return None
    if date_from:
        dt = _parse_date(date_from, end=False)
        if dt: created_range["$gte"] = dt
    if date_to:
        dt = _parse_date(date_to, end=True)
        if dt: created_range["$lte"] = dt
    if created_range:
        where["created_at"] = created_range

    if q:
        regex = {"$regex": q, "$options": "i"}
        where["$and"] = where.get("$and", []) + [{"$or": [
            {"tx_hash": regex},
            {"base_payment_id": regex},
            {"event_title": regex},
        ]}]

    total = db.transactions.count_documents(where)
    rows = list(db.transactions.find(where)
                .sort("created_at", -1)
                .skip((page - 1) * per_page)
                .limit(per_page))

    event_ids = {r.get("event_id") for r in rows if r.get("event_id")}
    event_map = {}
    if event_ids:
        evs = db.events.find({"_id": {"$in": [_to_oid(e) for e in event_ids if _to_oid(e)]}}, {"title": 1})
        for e in evs:
            event_map[str(e["_id"])] = e.get("title") or "Untitled Event"

    enriched, total_sum = [], 0.0
    for r in rows:
        amount = float(r.get("amount") or 0.0); total_sum += amount
        chain_id = int(r.get("chain_id") or 0)
        enriched.append({
            "id": str(r.get("_id")),
            "kind": r.get("kind") or "ticket_purchase",
            "event_id": r.get("event_id"),
            "event_title": r.get("event_title") or event_map.get(r.get("event_id") or "", "Untitled Event"),
            "tier_index": r.get("tier_index"),
            "quantity": int(r.get("quantity") or 0),
            "amount": amount,
            "currency": r.get("currency") or "USDC",
            "chain_id": chain_id,
            "chain_name": NETS_BY_CHAIN.get(chain_id, {}).get("name") or str(chain_id),
            "to": r.get("to"),
            "from": r.get("from"),
            "tx_hash": r.get("tx_hash"),
            "tx_link": _basescan_tx_url(chain_id, r.get("tx_hash")),
            "base_payment_id": r.get("base_payment_id"),
            "base_status": (r.get("base_status") or r.get("status") or "").lower(),
            "created_at": r.get("created_at"),
        })

    pages = max(1, math.ceil(total / per_page))
    vm = {
        "filters": {"q": q, "status": status_f, "kind": kind_f, "chain_id": chain_f,
                    "from": date_from, "to": date_to, "page": page, "per": per_page},
        "rows": enriched, "total": total, "sum_amount": round(total_sum, 2),
        "page": page, "pages": pages, "per": per_page,
        "net_opts": NETS_BY_CHAIN,
    }
    return render_template("attendee/transactions.html", vm=vm)

@attendee_tx_bp.route("/transactions/export.csv", methods=["GET"])
def export_csv():
    if not _require_login():
        return redirect(url_for("login.login"))

    attendee_id = session["uid"]
    where = {"attendee_id": attendee_id}
    status_f = (request.args.get("status") or "").strip().lower()
    if status_f:
        where["$or"] = [{"base_status": status_f}, {"status": status_f}]
    kind_f = (request.args.get("kind") or "").strip()
    if kind_f: where["kind"] = kind_f
    try:
        chain_f = request.args.get("chain_id")
        if chain_f: where["chain_id"] = int(chain_f)
    except: pass

    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["Date","Kind","Event","Qty","Amount","Currency","Chain","Tx Hash","Base Payment ID","Status"])
    cur = db.transactions.find(where).sort("created_at", -1)
    for r in cur:
        w.writerow([
            (r.get("created_at") or "").strftime("%Y-%m-%d %H:%M") if r.get("created_at") else "",
            r.get("kind") or "",
            r.get("event_title") or r.get("event_id") or "",
            int(r.get("quantity") or 0),
            r.get("amount") or 0,
            r.get("currency") or "USDC",
            NETS_BY_CHAIN.get(int(r.get("chain_id") or 0), {}).get("name") or (r.get("chain_id") or ""),
            r.get("tx_hash") or "",
            r.get("base_payment_id") or "",
            r.get("base_status") or r.get("status") or "",
        ])
    return Response(
        buf.getvalue().encode("utf-8"),
        headers={
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": 'attachment; filename="transactions.csv"',
        },
    )
