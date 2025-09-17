# seed_poolpart_existing_attendee.py
from datetime import datetime
from bson import ObjectId
from db import db

USDC_DECIMALS = 6
def round_usdc(x: float) -> float:
    return round(float(x), USDC_DECIMALS)

# ---- IDs you provided (existing docs) ----
ORGANIZER_ID = ObjectId("68a85a8e5912f230d6cdd038")  # Evans Nakisan
EVENT_ID     = ObjectId("68a9252b922451518430a216")  # Pool Part
ATTENDEE_ID  = ObjectId("68a8509c9e8bdd8aa3064e59")  # Nakisan Emmanuel

def main():
    now = datetime.utcnow()

    # Fetch event to read current tier + price
    ev = db.events.find_one({"_id": EVENT_ID})
    if not ev:
        print("❌ Event not found:", EVENT_ID)
        return

    # Use first tier (index 0) like a typical selection
    tier_idx = 0
    tiers = ev.get("tiers") or []
    if not tiers or tier_idx >= len(tiers):
        print("❌ Event has no tiers at index 0.")
        return
    tier = tiers[tier_idx]

    qty = 2  # buy 2
    unit_price = float(tier.get("price") or 0.0)
    expected_amount = round_usdc(unit_price * qty)

    # Mock chain/payment data (Base mainnet style)
    chain_id = 8453
    payer = "0x1111111111111111111111111111111111111111"
    pay_to = "0x98f869c3d188740ef666bbd97436f89c929826f7"
    tx_hash = "0x" + "cd"*32
    base_payment_id = "basepay_demo_poolpart_existing"
    base_status = "succeeded"
    base_status_raw = {"demo": True}

    # ---- payments (matches /checkout/complete) ----
    pay_doc = {
        "attendee_id": str(ATTENDEE_ID),
        "organizer_id": ORGANIZER_ID,      # tie explicitly to your manager
        "event_id": str(EVENT_ID),
        "tier_index": tier_idx,
        "quantity": qty,
        "unit_price": unit_price,
        "amount": expected_amount,
        "currency": "USDC",
        "status": "paid",                   # base_status present => "paid"
        "method": "base_pay",
        "tx_hash": tx_hash,
        "payer": payer,
        "pay_to": pay_to,
        "chain_id": chain_id,
        "base_payment_id": base_payment_id,
        "base_status": base_status,
        "base_status_raw": base_status_raw,
        "created_at": now,
    }
    pay_res = db.payments.insert_one(pay_doc)
    payment_id = str(pay_res.inserted_id)

    # ---- transactions (audit trail) ----
    db.transactions.insert_one({
        "kind": "ticket_purchase",
        "attendee_id": str(ATTENDEE_ID),
        "organizer_id": ORGANIZER_ID,
        "event_id": str(EVENT_ID),
        "tier_index": tier_idx,
        "quantity": qty,
        "amount": expected_amount,
        "currency": "USDC",
        "to": pay_to,
        "from": payer,
        "tx_hash": tx_hash,
        "base_payment_id": base_payment_id,
        "base_status": base_status,
        "chain_id": chain_id,
        "created_at": now,
        "payment_id": payment_id,
    })

    # ---- tickets (one per quantity) ----
    for _ in range(qty):
        db.tickets.insert_one({
            "event_id": str(EVENT_ID),
            "tier_index": tier_idx,
            "attendee_id": str(ATTENDEE_ID),
            "price": unit_price,
            "purchased_at": now,
            "payment_id": payment_id,
            "status": "valid",
        })

    print("✅ Seeded for existing attendee/manager/event")
    print("Event:", ev.get("title"))
    print("Attendee _id:", ATTENDEE_ID)
    print("Payment _id:", payment_id)

if __name__ == "__main__":
    main()
