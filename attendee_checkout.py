# attendee_checkout.py
import os
from datetime import datetime, timezone
from bson import ObjectId
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, abort, jsonify
from db import db

checkout_bp = Blueprint("checkout", __name__, url_prefix="/checkout")

# ================================
# HARD NETWORK CONFIG (NO ENVs)
# ================================
ACTIVE_NETWORK = "mainnet"   # "mainnet" or "sepolia"

NETS = {
    "mainnet": {
        "chain_id": 8453,
        "chain_id_hex": "0x2105",
        "chain_name": "Base",
        "rpc_urls": ["https://mainnet.base.org"],
        "explorer": "https://basescan.org",
        # Official USDC on Base mainnet (FYI, the Base SDK quotes USDC from USD)
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    },
    "sepolia": {
        "chain_id": 84532,
        "chain_id_hex": "0x14A34",
        "chain_name": "Base Sepolia",
        "rpc_urls": ["https://sepolia.base.org"],
        "explorer": "https://sepolia.basescan.org",
        # For testnet, the Base Account SDK can handle quoting—no custom token needed here
        "usdc": "0x0000000000000000000000000000000000000000",
    },
}

# Optional global fallback (only used if not found on event/organizer/user)
DEFAULT_PAYOUT_ADDRESS = "0x98f869c3d188740ef666bbd97436f89c929826f7"
USDC_DECIMALS = 6

# ================================
# Helpers (datetime/availability)
# ================================
from datetime import datetime, timezone

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

def _parse_sales_bound(val, kind: str):
    if not val:
        return None
    if isinstance(val, datetime):
        dt = val
    elif isinstance(val, str):
        s = val.strip()
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            try:
                y, m, d = map(int, s.split("-"))
                dt = datetime(y, m, d, 23, 59, 59, 999000) if kind == "end" else datetime(y, m, d, 0, 0, 0, 0)
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

def _tier_sales_open(tier, now_utc):
    s = _parse_sales_bound(tier.get("sales_start"), "start")
    e = _parse_sales_bound(tier.get("sales_end"), "end")
    if s and now_utc < s:
        return False
    if e and now_utc > e:
        return False
    return True

def _tier_availability(event_id_str, tier_idx, tier):
    supply = int(tier.get("supply") or 0)
    sold = 0
    if "tickets" in db.list_collection_names():
        sold = db.tickets.count_documents({"event_id": event_id_str, "tier_index": int(tier_idx)})
    available = max(0, supply - sold) if supply else 0
    return {"supply": supply, "sold": sold, "available": available}

# ================================
# Payout helpers (event -> organizers -> users -> default)
# ================================
def _norm_addr(addr: str) -> str:
    addr = (addr or "").strip()
    return addr if (isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42) else ""

def _pick(*vals) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def _to_oid(maybe_id):
    if isinstance(maybe_id, ObjectId):
        return maybe_id
    try:
        return ObjectId(str(maybe_id))
    except Exception:
        return None

def _find_payout_address(ev) -> str:
    # 1) Event-level fields
    cand = _pick(
        ev.get("payout_address"),
        ev.get("organizer_payout_address"),
        ev.get("treasury_address"),
        ev.get("usdc_payout"),
        ev.get("wallet_address"),
    )
    if _norm_addr(cand):
        return cand

    # Figure out organizer id
    org_id = ev.get("organizer_id") or ev.get("organizer_user_id") or ev.get("organizer")
    oid = _to_oid(org_id)

    # 2) organizers collection (if present)
    if oid and "organizers" in db.list_collection_names():
        org = db.organizers.find_one(
            {"_id": oid},
            {
                "wallet_address": 1,
                "payout_address": 1,
                "treasury_wallet": 1,
                "usdc_wallet": 1,
                "billing.payout_address": 1,
                "settings.payout_address": 1,
            },
        ) or {}
        for k in ("payout_address", "wallet_address", "treasury_wallet", "usdc_wallet"):
            cand = _norm_addr(org.get(k))
            if cand:
                return cand
        billing = org.get("billing") or {}
        cand = _norm_addr(billing.get("payout_address"))
        if cand:
            return cand
        settings = org.get("settings") or {}
        cand = _norm_addr(settings.get("payout_address"))
        if cand:
            return cand

    # 3) users collection (ROLE=organizer) — where your sample lives
    if oid and "users" in db.list_collection_names():
        user = db.users.find_one(
            {"_id": oid},
            {
                "wallet_address": 1,
                "payout_address": 1,
                "treasury_wallet": 1,
                "usdc_wallet": 1,
                "settings.payout_address": 1,
                "billing.payout_address": 1,
                "role": 1,
            },
        ) or {}
        for k in ("payout_address", "wallet_address", "treasury_wallet", "usdc_wallet"):
            cand = _norm_addr(user.get(k))
            if cand:
                return cand
        billing = (user.get("billing") or {})
        cand = _norm_addr(billing.get("payout_address"))
        if cand:
            return cand
        settings = (user.get("settings") or {})
        cand = _norm_addr(settings.get("payout_address"))
        if cand:
            return cand

    # 4) Hardcoded fallback
    return _norm_addr(DEFAULT_PAYOUT_ADDRESS)

# ================================
# Routes
# ================================
@checkout_bp.route("/start", methods=["GET"])
def start():
    """
    ?event_id=<id>&tier_idx=<int>&quantity=<int>
    """
    if "uid" not in session:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("login.login"))

    event_id = request.args.get("event_id") or ""
    try:
        tier_idx = int(request.args.get("tier_idx") or -1)
        qty_req = int(request.args.get("quantity") or 0)
    except Exception:
        flash("Invalid selection.", "danger")
        return redirect(url_for("public.browse_events"))

    oid = _to_oid(event_id)
    if not oid:
        abort(404)

    ev = db.events.find_one({"_id": oid, "status": "published"})
    if not ev:
        abort(404)

    tiers = ev.get("tiers") or []
    if tier_idx < 0 or tier_idx >= len(tiers):
        flash("Please select a valid ticket tier.", "danger")
        return redirect(url_for("public.event_profile", event_id=event_id))
    tier = tiers[tier_idx]

    now = datetime.now(timezone.utc)
    if not _tier_sales_open(tier, now):
        flash("Sales window closed for this tier.", "danger")
        return redirect(url_for("public.event_profile", event_id=event_id))

    avail = _tier_availability(str(ev["_id"]), tier_idx, tier)
    if avail["available"] <= 0:
        flash("This tier is sold out.", "danger")
        return redirect(url_for("public.event_profile", event_id=event_id))

    per_order_limit = int(tier.get("per_order_limit") or 0)
    max_allowed = per_order_limit if per_order_limit > 0 else avail["available"]

    qty = qty_req or 1
    if qty > max_allowed:
        qty = max_allowed
    if qty < 1:
        qty = 1

    price = float(tier.get("price") or 0.0)
    amount = round(price * qty, USDC_DECIMALS)  # internal precision

    payout = _find_payout_address(ev)
    if not payout:
        flash("Organizer payout wallet not configured. Add a 0x… address on the event or organizer user.", "danger")
        return redirect(url_for("public.event_profile", event_id=event_id))

    # Hard-configured network
    net = NETS[ACTIVE_NETWORK]
    chain_id = net["chain_id"]
    chain_id_hex = net["chain_id_hex"]
    chain_name = net["chain_name"]
    rpc_urls = net["rpc_urls"]
    explorer = net["explorer"]
    usdc_addr = net["usdc"]
    is_testnet = (ACTIVE_NETWORK != "mainnet")

    # Stash (optional)
    session["checkout_current"] = {
        "event_id": event_id,
        "tier_idx": tier_idx,
        "quantity": qty,
        "unit_price": price,
        "amount": amount,
        "max_allowed": max_allowed,
        "payout": payout,
        "chain_id": chain_id,
        "usdc": usdc_addr,
    }

    vm = {
        "event": {
            "id": str(ev["_id"]),
            "title": ev.get("title") or "Untitled Event",
            "organizer_id": ev.get("organizer_id"),
        },
        "tier": {
            "name": tier.get("name") or f"Tier {tier_idx+1}",
            "price": price,
        },
        "qty": qty,
        "max_qty": max_allowed,
        "amount": amount,
        # Everything the front-end needs for Base SDK
        "web3": {
            "isTestnet": is_testnet,
            "chainId": chain_id,
            "chainIdHex": chain_id_hex,
            "chainName": chain_name,
            "rpcUrls": rpc_urls,
            "explorer": explorer,
            "payout": payout,
            "decimals": USDC_DECIMALS,
            "completeUrl": url_for("checkout.complete", _external=True),
            "eventId": str(ev["_id"]),
            "tierIdx": tier_idx,
            "baseApp": {
                "appName": "AkwaabaTickets",
                "appLogoUrl": "https://base.org/logo.png",
            },
        },
    }
    return render_template("attendee/checkout.html", vm=vm)

@checkout_bp.route("/complete", methods=["POST"])
def complete():
    """
    Accepts Base Account SDK result:
      - basePaymentId (result.id)
      - baseStatus ('succeeded' / etc.)
      - optionally txHash (if available)
    Also keeps our previous fields for compatibility.
    """
    if "uid" not in session:
        return jsonify({"ok": False, "error": "not_logged_in"}), 401

    data = request.get_json(silent=True) or {}
    event_id = data.get("eventId")
    tier_idx = int(data.get("tierIdx", -1))
    qty      = int(data.get("quantity", 0))
    payer    = (data.get("from") or "").strip()
    paid_to  = (data.get("to") or "").strip()
    chain_id = int(data.get("chainId") or 0)
    amount_u = str(data.get("amountUSDC") or "0")

    # New from Base Account SDK
    base_payment_id = (data.get("basePaymentId") or "").strip()
    base_status     = (data.get("baseStatus") or "").strip()
    base_status_raw = data.get("baseStatusRaw")  # optional blob
    tx_hash         = (data.get("txHash") or "").strip()  # may be absent with Base SDK

    if not (event_id and qty > 0 and chain_id and paid_to):
        return jsonify({"ok": False, "error": "invalid_payload"}), 400

    oid = _to_oid(event_id)
    if not oid:
        return jsonify({"ok": False, "error": "bad_event"}), 400

    ev = db.events.find_one({"_id": oid, "status": "published"})
    if not ev:
        return jsonify({"ok": False, "error": "not_found"}), 404

    tiers = ev.get("tiers") or []
    if tier_idx < 0 or tier_idx >= len(tiers):
        return jsonify({"ok": False, "error": "bad_tier"}), 400
    tier = tiers[tier_idx]

    avail = _tier_availability(str(ev["_id"]), tier_idx, tier)
    if qty > avail["available"]:
        return jsonify({"ok": False, "error": "sold_out"}), 409

    price = float(tier.get("price") or 0.0)
    expected_amount = round(price * qty, USDC_DECIMALS)

    try:
        paid_amount = round(float(amount_u), USDC_DECIMALS)
    except Exception:
        paid_amount = -1

    # For production: verify result.id with Base API and confirm payment status.
    # Here we just validate client math.
    if expected_amount > 0 and abs(paid_amount - expected_amount) > 1e-6:
        return jsonify({"ok": False, "error": "amount_mismatch"}), 400

    now_dt = datetime.utcnow()
    payment_method = "base_pay"
    payment_status = "paid" if base_status else "onchain_confirmed"

    pay_doc = {
        "attendee_id": session["uid"],
        "organizer_id": ev.get("organizer_id"),
        "event_id": str(ev["_id"]),
        "tier_index": tier_idx,
        "quantity": qty,
        "unit_price": price,
        "amount": expected_amount,
        "currency": "USDC",
        "status": payment_status,
        "method": payment_method,
        "tx_hash": tx_hash or None,
        "payer": payer,
        "pay_to": paid_to,
        "chain_id": chain_id,
        "base_payment_id": base_payment_id or None,
        "base_status": base_status or None,
        "base_status_raw": base_status_raw or None,
        "created_at": now_dt,
    }
    res = db.payments.insert_one(pay_doc)
    payment_id = str(res.inserted_id)

    # Also record to a 'transactions' collection as requested
    db.transactions.insert_one({
        "kind": "ticket_purchase",
        "attendee_id": session["uid"],
        "organizer_id": ev.get("organizer_id"),
        "event_id": str(ev["_id"]),
        "tier_index": tier_idx,
        "quantity": qty,
        "amount": expected_amount,
        "currency": "USDC",
        "to": paid_to,
        "from": payer or None,
        "tx_hash": tx_hash or None,
        "base_payment_id": base_payment_id or None,
        "base_status": base_status or None,
        "chain_id": chain_id,
        "created_at": now_dt,
        "payment_id": payment_id,
    })

    # Issue tickets
    for _ in range(qty):
        db.tickets.insert_one({
            "event_id": str(ev["_id"]),
            "tier_index": tier_idx,
            "attendee_id": session["uid"],
            "price": price,
            "purchased_at": now_dt,
            "payment_id": payment_id,
            "status": "valid",
        })

    return jsonify({
        "ok": True,
        "redirect": url_for("public.event_profile", event_id=str(ev["_id"])),
        "payment_id": payment_id,
        "base_payment_id": base_payment_id or None
    })
