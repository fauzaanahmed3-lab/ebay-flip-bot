"""eBay Flip Bot scanner.

Every ~10 minutes (GitHub Actions) it runs the staged filter from the playbook:
  1. Smart search   - exact AND broad queries per category (eBay Browse API, UK, new+used, Buy It Now)
  2. Price gate     - drop anything above the category's best possible target
  3. Hard reject    - junk words + known wrong models (titles normalised first)
  4. Two lanes      - EXACT (model in title) or VAGUE (brand/family only = possible hidden gem)
  5. Confirm        - one detail lookup per survivor: item specifics, photos, returns, seller
  6. Alert          - rich Discord card with verdict, rating and plain-English notes

Modes: (default) scan · --digest daily picks · --test one fake alert.
Secrets (GitHub Actions): EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, SUPABASE_URL, SUPABASE_SERVICE_KEY,
DISCORD_WEBHOOK_URL, DISCORD_DAILY_WEBHOOK_URL.  DRY_RUN=1 prints instead of saving/alerting.
"""
import base64
import json
import os
import re
import sys
import time
from datetime import timedelta
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections import Counter
from pathlib import Path

import requests

CONFIG = json.loads((Path(__file__).parent / "categories.json").read_text())
DRY_RUN = os.getenv("DRY_RUN") == "1"
EBAY_API = "https://api.ebay.com"
TABLE = "flip_listings"
WHY = Counter()          # (category, reason) -> how many listings were dropped for it this run
LOG: list[str] = []      # lines for the #bot-logs channel
USAGE = Counter()        # API calls made this run, by 'ebay' or Gemini model name
LIMITED = Counter()      # Gemini 'free allowance used up' (429) replies this run


def _no(cat_id: str, reason: str):
    WHY[(cat_id, reason)] += 1
    return None


SCREENED_IN: dict[str, str] = {}     # item_id -> category it was AI-screened out of
AI_OK: dict[str, str] = {}           # item_id -> model: AI-approved listings still waiting for a detail lookup
SOFT: list[tuple[dict, dict]] = []   # (category, candidate) dropped only by word filters: sent to the AI screen
REJECT_WHY: dict[str, str] = {}      # item_id -> why score() rejected it after reading the listing (for #bot-logs)


def _soft(cat: dict, cand: dict, reason: str):
    WHY[(cat["id"], f"{reason} → AI")] += 1
    SOFT.append((cat, {**cand, "soft_reason": reason}))
    return None
PACK = CONFIG["packaging_cost"]


# ---------- text helpers ----------

def norm(text: str) -> str:
    """Lowercase and tidy so 'WH-1000 XM 6', 'wh1000xm6' and 'XM6' all contain 'xm6'."""
    t = (text or "").lower().replace("-", " ").replace("_", " ")
    t = t.replace("quietcomfort", "qc")
    t = re.sub(r"\b(xm|gen|qc)\s+(\d)", r"\1\2", t)          # "xm 6" -> "xm6", "gen 2" -> "gen2"
    t = re.sub(r"(\d)\s+(gb|tb)\b", r"\1\2", t)               # "512 gb" -> "512gb"
    return re.sub(r"\s+", " ", t).strip()


def any_match(patterns, text) -> str | None:
    for p in patterns:
        if re.search(p, text):
            return p
    return None


NEGATION = re.compile(r"\b(no|not|without|never|nothing|zero)\s+(\w+\s+)?$")


def junk_word(words, text) -> str | None:
    """First junk word in the text, ignoring negated ones ('no scratches', 'nothing missing')."""
    for w in words:
        for m in re.finditer(re.escape(w), text):
            if not NEGATION.search(text[max(0, m.start() - 20):m.start()]):
                return w
    return None


DAMAGE = re.compile(r"(?<!no )(?<!without )(?<!not )\b(crack|broken|faulty|not working|doesn.?t work|"
                    r"drift|burn ?in|dead pixel|water damage|won.?t charge|no sound)")


def damage_in(text: str) -> bool:
    """Damage words in the seller's condition notes, ignoring 'no cracks' / 'without drift' etc."""
    return bool(DAMAGE.search(norm(text)))


# Strong "this unit is broken" phrases in the full description (23 Nov: a PS5 card was posted whose description said
# it was dead, while the AI check was down). Only clear statements, so shop boilerplate like "if faulty, contact us"
# or "fully working, not faulty" doesn't trip it.
DEAD = re.compile(
    r"\b(is|it'?s|its|console is|unit is|headset is|deck is|totally|completely|now) (dead|faulty|broken)\b"
    r"|\bstopped working\b|\bdead (console|unit|ps5|xbox|switch|headset|deck)\b"
    r"|\b(won'?t|wont|doesn'?t|does not|will not|can'?t|cannot|no longer) (turn|switch|power|boot)(s|ing)? ?(on|up)\b"
    r"|\b(won'?t|wont|doesn'?t|does not|will not) (start|boot|load)\b|\bno power\b|\bnot powering( on| up)?\b"
    r"|\bblue light of death\b|\bblod\b|\bno (display|picture|signal|video)( output)?\b(?! (issues?|problems?|faults?))"
    r"|\bhdmi (port )?(is )?(broken|damaged|faulty|not working)\b"
    r"|\b(disc|disk) drive (is )?(not working|broken|faulty|doesn'?t work)\b|\bsold as (faulty|spares)\b"
    r"|\bfor (spares|parts)( (or|and|/) repairs?)?\b|\bspares (or|and|/) repairs?\b|\b(console|account) (is )?banned\b"
    r"|\bneeds (a )?(new )?(repair|fixing|motherboard|apu|hdmi port)\b")
# shop/returns boilerplate ("if the item is faulty, contact us") and denials ("not faulty", "never been banned")
DEAD_CONTEXT_OK = re.compile(r"(\bif\b|in case|should|\bnot\b|\bnever\b|\bno\b|\bisn'?t\b|return|refund|contact)[^.!|]{0,30}$")


def listing_text(details: dict) -> str:
    """Seller notes + short description + full description, as plain text (what a buyer would read)."""
    d = details or {}
    parts = [d.get("conditionDescription") or "", _strip_html(d.get("shortDescription") or ""),
             _strip_html(d.get("description") or "")[:6000]]
    return " | ".join(p for p in parts if p)


def dead_in(details: dict) -> str | None:
    """The phrase that says the unit is broken, or None. Checked on every looked-up listing, AI or not."""
    text = norm(listing_text(details)).replace("\u2019", "'")
    for m in DEAD.finditer(text):
        if DEAD_CONTEXT_OK.search(text[max(0, m.start() - 40):m.start()]):
            continue
        return m.group(0)
    return None


# ---------- profit rules ----------

PROFIT_TIERS = [(0, 300, 50), (300, 400, 75), (400, 10**6, 100)]   # (from £, below £, profit needed)


def required_profit(all_in: float) -> int:
    """£50 under £300, £75 from £300, £100 from £400 (all-in purchase price)."""
    return next(need for lo, hi, need in PROFIT_TIERS if lo <= all_in < hi)


def buy_target(strong: float, post_out: float, max_buy: float | None = None) -> float:
    cap = max_buy if max_buy is not None else CONFIG.get("max_buy", 10**6)
    return min(_buy_target(strong, post_out), cap)   # never spend more than the cap


def cat_target(cat: dict, model: dict) -> float:
    """Buy target for one model. Categories with min_safe_profit must clear that profit even at a typical sale price."""
    t = buy_target(model["strong"], cat["post_out"], cat.get("max_buy"))
    msp = cat.get("min_safe_profit")
    if msp:
        t = min(t, round(net_sale(model.get("typical", model["strong"])) - cat["post_out"] - PACK - msp, 2))
    return t


def offer_target(cat: dict, model: dict) -> float | None:
    """#send-an-offer lane: highest all-in price that still leaves the lane's minimum profit (e.g. £50) at a
    TYPICAL resale price. None for categories without the lane."""
    lane = cat.get("offer_channel")
    if not lane:
        return None
    cap = cat.get("max_buy", CONFIG.get("max_buy", 10**6))
    return round(min(cap, net_sale(model.get("typical", model["strong"])) - cat["post_out"] - PACK - lane["min_safe_profit"]), 2)


def in_offer_lane(cat: dict, model: dict, all_in: float, best_offer: bool) -> bool:
    """At or under the lane's target, or a little over it (offer_zone_pct) when the seller accepts offers."""
    ot = offer_target(cat, model)
    return bool(ot) and (all_in <= ot or (best_offer and all_in <= ot * (1 + CONFIG["offer_zone_pct"])))


def offer_zone(cat: dict) -> float:
    """How far over target we still flag as 'send an offer'. 0 for hot-deals-only categories."""
    return 0 if cat.get("no_offers") else CONFIG["offer_zone_pct"]


NO_PAD = re.compile(r"console only\b(?! (used|been|had|played|ever|a few|once|twice))|\bno (controllers?|pads?)\b|"
                    r"without (a |the |any )?(controllers?|pads?)\b|(controllers?|pads?) (is |are )?not included|"
                    r"(doesn'?t|does not) come with (a |the |any )?(controllers?|pads?)")


def net_sale(price: float) -> float:
    """What we really receive for a resale. The sold prices we track are what buyers paid, and for private
    sellers that already includes eBay's buyer protection fee, so we keep the price minus that fee."""
    return round(price - buyer_fee(price), 2)


def _buy_target(strong: float, post_out: float) -> float:
    """Highest price we can pay and still make that price band's required profit."""
    net = net_sale(strong) - post_out - PACK
    best = 0.0
    for lo, hi, need in PROFIT_TIERS:
        t = min(net - need, hi - 0.01)
        if t >= lo:
            best = max(best, t)
    return round(best, 2)


def buyer_fee(price: float) -> float:
    """eBay UK Buyer Protection Fee paid on private-seller items: 10p + 7% to £20, 4% to £300, 2% to £4,000."""
    p = max(price, 0)
    return round(0.10 + 0.07 * min(p, 20) + 0.04 * max(0, min(p, 300) - 20) + 0.02 * max(0, min(p, 4000) - 300), 2)


def model_for(cat: dict, text: str) -> dict | None:
    return next((m for m in cat["models"] if re.search(m["match"], text)), None)


# ---------- stages 2-4: cheap checks on the search result ----------

def triage(cat: dict, item: dict) -> dict | None:
    """Return a candidate dict (lane EXACT or VAGUE) or None.
    Hard rejects (price, faulty/parts, weak seller, abroad) are final. Soft rejects (word filters that can be
    fooled by a lazy title, e.g. 'PS5 Pro with 2 controllers') go to SOFT for a batch AI screen instead."""
    title = item.get("title", "")
    t = norm(title)
    if item.get("itemLocation", {}).get("country", "GB") != "GB":
        return _no(cat["id"], "not UK")

    price = float(item["price"]["value"])
    ship_opts = item.get("shippingOptions") or []
    ship_known = bool(ship_opts and "shippingCost" in ship_opts[0])
    ship = float(ship_opts[0]["shippingCost"].get("value", 0) or 0) if ship_known else 0.0
    fee = buyer_fee(price)                       # assume a private seller until the details say business
    all_in = round(price + ship + fee, 2)

    best_target = max(cat_target(cat, m) for m in cat["models"])
    gate = best_target * (1 + offer_zone(cat))
    if cat.get("offer_channel"):                                       # the #send-an-offer lane can reach a bit higher
        gate = max(gate, max(offer_target(cat, m) for m in cat["models"]) * (1 + CONFIG["offer_zone_pct"]))
    if all_in > gate:                                                  # stage 2: price gate (maths, final)
        return _no(cat["id"], "too pricey")

    junk = CONFIG["junk"] + (CONFIG["phone_rules"]["junk"] if cat.get("phone") else [])
    if junk_word(junk, t):                                               # faulty / parts / box only: final
        return _no(cat["id"], "faulty or parts")

    seller = item.get("seller", {})
    fb = int(seller.get("feedbackScore", 0) or 0)
    pct = float(seller.get("feedbackPercentage", 0) or 0)
    if fb < CONFIG["seller_min_feedback"] or pct < CONFIG["seller_min_pct"]:
        return _no(cat["id"], "weak seller")                             # brand-new or poorly rated seller
    if cat.get("phone") and (fb < CONFIG["phone_rules"]["min_feedback"] or pct < CONFIG["phone_rules"]["min_pct"]):
        return _no(cat["id"], "weak seller")

    best_offer = "BEST_OFFER" in item.get("buyingOptions", [])
    cand = {
        "item_id": item["itemId"], "category": cat["id"], "title": title[:200],
        "url": item.get("itemWebUrl"), "image_url": (item.get("image") or {}).get("imageUrl"),
        "price": price, "shipping": ship, "all_in": all_in, "fee": fee,
        "best_offer": best_offer, "ship_known": ship_known,
        "seller": None, "seller_feedback": fb, "seller_pct": pct,
        "listed_at": item.get("itemCreationDate"),
        "lane": None, "model": None, "condition": item.get("condition"),
        "item_location": item.get("itemLocation") or {},
    }

    if cat.get("must_have") and not re.search(cat["must_have"], t):       # e.g. no "xbox" anywhere: eBay's loose
        return _no(cat["id"], "not this product")                        # search padding (car parts), final, no AI
    if (cat.get("must_not") and re.search(cat["must_not"], t)            # e.g. "PlayStation 4 Slim" in the PS5 check:
            and not re.search(cat.get("must_not_unless", r"(?!)"), t)):  # older model named, ours not: final, no AI
        return _no(cat["id"], "older/other model")

    # stage 3: word filters. A hit here is a "maybe": the AI screen gets the final say.
    if junk_word(CONFIG.get("junk_soft", []), t) or any_match(cat["wrong"] + CONFIG.get("junk_patterns", []), t):
        return _soft(cat, cand, "junk/wrong item")
    if cat.get("require") and not all(w in t for w in cat["require"]):
        return _soft(cat, cand, "missing key word")
    if cat.get("require_any") and not re.search(cat["require_any"], t):
        return _soft(cat, cand, "not the main product")
    hints = cat.get("category_hint")
    names = " ".join(c.get("categoryName", "") for c in item.get("categories", [])).lower()
    if hints and names and not any(h in names for h in hints):
        return _soft(cat, cand, "wrong eBay category")

    model = model_for(cat, t)                                            # stage 4: lanes
    if model:
        target = cat_target(cat, model)
        if (all_in > target and not (best_offer and all_in <= target * (1 + offer_zone(cat)))
                and not in_offer_lane(cat, model, all_in, best_offer)):
            return _no(cat["id"], "too pricey")                          # e.g. an XM5 priced like an XM6
        return {**cand, "lane": "EXACT", "model": model["name"]}
    if re.search(cat["family"], t):
        return {**cand, "lane": "VAGUE"}
    return _soft(cat, cand, "model not recognised")


# ---------- stage 5: confirm with item details, then score ----------

DROP = ("best_offer", "model", "condition", "ship_known", "fee", "soft_reason", "rescued", "item_location")          # working fields, not stored

def too_cheap_to_be_true(cat: dict, model: dict, all_in: float, condition: str) -> str | None:
    """B3: a price so low it smells like a scam. True if under 75% of the model's typical resale price, or if it is
    listed as new/sealed but below the category's 'new_shop_price' (what a shop charges new). Returns the reason."""
    typical = model.get("typical", model["strong"])
    if all_in < 0.75 * typical:
        return f"£{all_in:.0f} is under 75% of the usual £{typical:.0f}."
    shop = cat.get("new_shop_price")
    if shop and condition.lower().startswith(("new", "brand new", "sealed")) and all_in < shop:
        return f"Listed as new at £{all_in:.0f}, well under the ~£{shop} shop price."
    return None


def delivery_and_location(cand: dict, details: dict | None) -> tuple[str, str | None]:
    """('free' | '£4.99' | 'collection', 'Leeds · LS6') for the top line of the card.
    Collection = no postage option on the listing (search result and full details)."""
    d = details or {}
    ship_known, ship, extra = cand.get("ship_known", True), float(cand.get("shipping") or 0), False
    if not ship_known:
        opts = d.get("shippingOptions") or []
        costs = [o.get("shippingCost") for o in opts if o.get("shippingCost")]
        if costs:                                      # only the full details show postage: not in the all-in price
            ship_known, ship, extra = True, float(costs[0].get("value") or 0), True
    delivery = "collection" if not ship_known else ("free" if ship == 0 else f"£{ship:.2f}" + (" extra" if extra else ""))
    loc = d.get("itemLocation") or cand.get("item_location") or {}
    town = (loc.get("city") or "").strip().title()
    pc = re.sub(r"\*+$", "", (loc.get("postalCode") or "").strip()).upper()
    pc = pc.split()[0] if pc else ""
    where = " · ".join(x for x in (town, pc) if x) or None
    return delivery, where


def delivery_badge(row: dict) -> str:
    """Bold first line of a card: how you get it and where it is."""
    dl, loc = row.get("delivery"), row.get("location")
    where = f"📍 {loc}" if loc else ""
    if dl == "collection":
        return f"⚠️ **COLLECTION ONLY** (no postage on the listing){' · ' + where if where else ''}"
    if dl:
        head = ("🚚 **FREE P&P**" if dl == "free" else f"🚚 **P&P {dl[:-6]}** (add it to the price)" if dl.endswith(" extra")
                else f"🚚 **P&P {dl}** (in the all-in price)")
        return head + (f" · {where}" if where else "")
    return where


def score(cat: dict, cand: dict, details: dict | None) -> dict | None:
    notes = []
    aspects = ""
    photos = None
    returns_ok = None
    if details:
        aspects = norm(" ".join(f"{a.get('name')} {a.get('value')}" for a in details.get("localizedAspects", [])))
        photos = 1 + len(details.get("additionalImages") or [])
        returns_ok = (details.get("returnTerms") or {}).get("returnsAccepted")
        if any_match(cat.get("wrong_specs", []), aspects) or damage_in(details.get("conditionDescription", "")):
            return None                                       # specifics reveal wrong model / damage
        dead = dead_in(details)
        if dead:
            REJECT_WHY[cand["item_id"]] = f"description says \"{dead}\""
            return None                                       # the description says it's broken (title looked fine)
        if cat.get("needs_controller"):
            blob = norm(" ".join([details.get("conditionDescription") or "", _strip_html(details.get("shortDescription") or ""),
                                  _strip_html(details.get("description") or "")[:4000]]))
            if NO_PAD.search(blob):
                return None                                   # seller says no controller included

    if details and (details.get("seller") or {}).get("sellerAccountType") == "BUSINESS" and cand.get("fee"):
        cand = {**cand, "all_in": round(cand["all_in"] - cand["fee"], 2), "fee": 0}   # business sellers: no buyer fee
    lane = cand["lane"]
    model = next((m for m in cat["models"] if m["name"] == cand["model"]), None)
    if lane == "VAGUE":
        model = model_for(cat, aspects) if aspects else None
        if model:
            lane = "GEM"
            notes.append("💎 Vague title, but the listing details confirm the model (fewer buyers will spot it).")
        elif re.search(r"\bmodel\b", aspects):
            return None                                   # specifics name a model, and it isn't one we want
        else:
            lane = "UNCONFIRMED"
            model = min(cat["models"], key=lambda m: m["strong"])          # price as the cheapest model
            notes.append("❓ Model not confirmed: check the photos before buying.")

    target = cat_target(cat, model)
    all_in, ship = cand["all_in"], cand["shipping"]
    suggested_offer, offer_lane = None, False
    if lane == "UNCONFIRMED":
        if all_in > target - CONFIG["unconfirmed_extra_margin"]:
            return None
        verdict = "DEAL"
    elif all_in <= target:
        verdict = "DEAL"
    elif cand["best_offer"] and all_in <= target * (1 + offer_zone(cat)):
        verdict = "OFFER"
        fee_rate = 1 if cand.get("fee") else 0
        ot = offer_target(cat, model)                          # offer lane categories: never suggest more than the lane allows
        aim = min(target, ot) if ot else target
        suggested_offer = int(aim - ship - fee_rate * buyer_fee(aim - ship))   # offer + postage + fee = aim
    elif lane in ("EXACT", "GEM") and in_offer_lane(cat, model, all_in, cand["best_offer"]):
        verdict, offer_lane = "OFFER", True                    # not good enough for #deals, fine for #send-an-offer
        ot = offer_target(cat, model)
        fee_rate = 1 if cand.get("fee") else 0
        lane_offer = int(ot - ship - fee_rate * buyer_fee(ot - ship))
        suggested_offer = lane_offer if lane_offer < cand["price"] else None
    else:
        return None

    pay = (suggested_offer + ship + (buyer_fee(suggested_offer) if cand.get("fee") else 0)) if suggested_offer else all_in
    profit = round(net_sale(model["strong"]) - cat["post_out"] - PACK - pay, 2)
    safe_profit = round(net_sale(model.get("typical", model["strong"])) - cat["post_out"] - PACK - pay, 2)
    need = max(required_profit(pay), cat.get("min_safe_profit") or 0)
    if offer_lane:
        need = cat["offer_channel"]["min_safe_profit"]

    # ---- notes: short CAPS tags for small facts (one line), full sentences only for things that need action ----
    # (25 Nov, testers' feedback: notes were too long. Dropped: the buyer-fee line (it's already in the all-in price),
    # "No returns accepted" and "Listed above target, offer £x" (the offer is already shown under the price).)
    tags = []
    fb, pct = cand["seller_feedback"], cand["seller_pct"]
    trusted = fb >= CONFIG["trusted_min_feedback"] and pct >= CONFIG["trusted_min_pct"]
    if fb < 10:
        tags.append(f"LOW FB ({fb})")
    elif pct < CONFIG["trusted_min_pct"]:
        tags.append(f"FB {pct:g}%")
    if photos is not None and photos < 3:
        tags.append(f"{photos} PIC{'S' if photos != 1 else ''}")
    cond = (cand.get("condition") or "").lower()
    if cond.startswith(("new", "open", "brand new")):
        tags.append("NEW/OPEN BOX")
    if cat.get("phone"):
        tags.append("IMEI CHECK")
    if details is None:
        tags.append("DETAILS NOT CHECKED")
    too_cheap = too_cheap_to_be_true(cat, model, all_in, cond)
    if too_cheap:
        notes.append(f"⚠️ Too cheap to be true? {too_cheap} Check for a scam or a hijacked account before paying.")
    if model.get("note"):
        notes.append(model["note"])
    if offer_lane and suggested_offer:
        notes.append(f"Offer £{suggested_offer} → ~£{need} profit at an average price.")
    elif offer_lane:
        notes.append(f"~£{need}+ profit at an average price as listed; a lower offer is a bonus.")

    # B6: rough profit if we sold as a business instead (approximate eBay UK business final value fee)
    # A business seller's buyer pays no buyer protection fee, so the business gets the full price, minus eBay's fee.
    strong_gross = model["strong"]
    business_profit = round(profit + (strong_gross - net_sale(strong_gross))
                            - (strong_gross * CONFIG.get("business_fee_pct", 0.075) + CONFIG.get("business_fee_fixed", 0.30)), 2)
    tags.append(f"BIZ ~£{business_profit:.0f}")

    # ---- rating ----
    fast_seller = (cat.get("weekly_sales") or 0) >= 10
    if (lane in ("EXACT", "GEM") and trusted and verdict == "DEAL" and cand.get("ship_known", True)
            and profit >= need * CONFIG["hot_ratio"] and safe_profit >= need):
        rating = "HOT"
        notes.insert(0, "Profitable even at an average price · trusted seller" + (" · fast seller." if fast_seller else "."))
    elif lane == "UNCONFIRMED" or not trusted or safe_profit < need / 2:
        rating = "CHECK"
        if safe_profit < need / 2 and trusted and lane != "UNCONFIRMED":
            tags.append(f"THIN MARGIN (~£{safe_profit:.0f} avg)")
    else:
        rating = "SOLID"
    if too_cheap and rating == "HOT":
        rating = "CHECK"                                   # a price this low is never a "buy fast" until a human checks it
    if tags:
        notes.append("🏷️ " + " · ".join(tags))

    delivery, location = delivery_and_location(cand, details)
    return {**{k: v for k, v in cand.items() if k not in DROP},
            "safe_profit": safe_profit, "variant": model["name"], "lane": lane, "verdict": verdict, "rating": rating,
            "buy_target": target, "expected_sell": model["strong"], "profit": profit,
            "required_profit": need, "suggested_offer": suggested_offer, "delivery": delivery, "location": location,
            "notes": "\n".join(notes) or None, "flags": None}


# ---------- eBay ----------

TOKEN_KEY = "ebay_app_token"     # bot_state row holding the cached eBay application token + its expiry


def ebay_token(force: bool = False) -> str:
    """Reuse the eBay application token (valid ~2 hours) instead of asking eBay for a new one every run.
    force=True (the hourly 'token' job) always fetches a new one, so the Supabase scanner always has 1+ hour left."""
    base = os.getenv("SUPABASE_URL")
    if base and not DRY_RUN and not force:
        try:
            st = requests.get(f"{base}/rest/v1/bot_state", headers=sb_headers(), timeout=15,
                              params={"select": "value", "key": f"eq.{TOKEN_KEY}"}).json()
            if st:
                saved = json.loads(st[0]["value"])
                if datetime.fromisoformat(saved["exp"]) > datetime.now(timezone.utc) + timedelta(minutes=10):
                    LOG.append("🔑 eBay token: reused")
                    return saved["t"]
        except (requests.RequestException, KeyError, ValueError, TypeError, IndexError) as e:
            print(f"Token cache read skipped: {e}", file=sys.stderr)
    token, life = _new_ebay_token()
    LOG.append("🔑 eBay token: new")
    if base and not DRY_RUN:
        exp = (datetime.now(timezone.utc) + timedelta(seconds=int(life))).isoformat()
        try:
            requests.post(f"{base}/rest/v1/bot_state", timeout=15,
                          headers={**sb_headers(), "Prefer": "resolution=merge-duplicates"},
                          json={"key": TOKEN_KEY, "value": json.dumps({"t": token, "exp": exp})})
        except requests.RequestException as e:
            print(f"Token cache save skipped: {e}", file=sys.stderr)
    return token


def forget_ebay_token() -> None:
    """eBay said the token is no good (401): drop the cached one so the next run fetches a fresh token."""
    base = os.getenv("SUPABASE_URL")
    if base and not DRY_RUN:
        try:
            requests.delete(f"{base}/rest/v1/bot_state", headers=sb_headers(), timeout=15,
                            params={"key": f"eq.{TOKEN_KEY}"})
        except requests.RequestException:
            pass


def _new_ebay_token() -> tuple[str, int]:
    creds = f"{os.environ['EBAY_CLIENT_ID'].strip()}:{os.environ['EBAY_CLIENT_SECRET'].strip()}"
    r = requests.post(
        f"{EBAY_API}/identity/v1/oauth2/token",
        headers={"Authorization": "Basic " + base64.b64encode(creds.encode()).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=20,
    )
    if not r.ok:
        print(f"eBay token error {r.status_code}: {r.text[:300]}")
        print("Key check (values hidden):")
        for name in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"):
            v = os.environ[name].strip()
            env = "PRODUCTION" if "PRD" in v else "SANDBOX" if "SBX" in v else "UNKNOWN"
            if v.startswith(("PRD-", "SBX-")):
                kind = "Cert ID"
            elif env != "UNKNOWN" and v.count("-") >= 3:
                kind = "App ID"
            elif len(v) == 36 and v.count("-") == 4:
                kind = "looks like Dev ID (wrong one)"
            else:
                kind = "unrecognised"
            print(f"  {name}: length {len(v)}, {env}, {kind}")
    r.raise_for_status()
    j = r.json()
    return j["access_token"], int(j.get("expires_in") or 7200)


def ebay_headers(token):
    return {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"}


def ebay_search(token: str, cat: dict, query: str | None, category_ids: str | None = None,
                limit: int = 50) -> list[dict]:
    filters = ",".join(["buyingOptions:{FIXED_PRICE}", "conditions:{NEW|USED}", "itemLocationCountry:GB",
                        f"price:[{cat['price_min']}..{cat['price_max']}]", "priceCurrency:GBP"])
    params = {"filter": filters, "sort": "newlyListed", "limit": limit}
    if query:
        params["q"] = query
    if category_ids:
        params["category_ids"] = category_ids              # sweep: a whole eBay category, no keyword
    USAGE["ebay"] += 1
    r = requests.get(f"{EBAY_API}/buy/browse/v1/item_summary/search", headers=ebay_headers(token),
                     params=params, timeout=20)
    if r.status_code == 401:
        forget_ebay_token()                                   # cached token rejected: next run gets a new one
    r.raise_for_status()
    return r.json().get("itemSummaries", [])


GONE: set[str] = set()   # A2: listings eBay says no longer exist (404) when we look them up


def ebay_item(token: str, item_id: str) -> dict | None:
    USAGE["ebay"] += 1
    r = requests.get(f"{EBAY_API}/buy/browse/v1/item/{item_id}", headers=ebay_headers(token), timeout=20)
    if r.status_code == 404:
        GONE.add(item_id)
    return r.json() if r.ok else None


def ended(details: dict, now: datetime | None = None) -> bool:
    """A2: the listing details say it has sold out or already ended, so there's nothing left to buy."""
    if any(a.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK" for a in details.get("estimatedAvailabilities") or []):
        return True
    end = details.get("itemEndDate")
    if end:
        try:
            return datetime.fromisoformat(end.replace("Z", "+00:00")) <= (now or datetime.now(timezone.utc))
        except ValueError:
            return False
    return False


SURVIVAL_MIN = [1, 2, 3, 5, 10, 20]      # A1: re-check each posted deal about this many minutes after posting


def mark_posted(item_id: str, msg_id: str | None) -> None:
    """A1: remember when a deal was posted to Discord and which message it was."""
    if DRY_RUN:
        return
    requests.patch(f"{os.environ['SUPABASE_URL']}/rest/v1/{TABLE}", headers=sb_headers(), timeout=20,
                   params={"item_id": f"eq.{item_id}"},
                   json={"posted_at": datetime.now(timezone.utc).isoformat(), "discord_msg_id": msg_id, "live_checks": 0})


def survival_due(posted_at: datetime, checks_done: int, now: datetime) -> bool:
    """Is the next survival check for this alert due yet?"""
    return checks_done < len(SURVIVAL_MIN) and now >= posted_at + timedelta(minutes=SURVIVAL_MIN[checks_done])


def mark_card_gone(msg_id: str | None, posted_at: str | None, now: datetime) -> None:
    """A3: edit the deal's Discord card to show it has gone (grey, '❌ Gone after X min'). Editing never pings anyone."""
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if msg_id and msg_id.startswith("offers:"):                # card lives in #send-an-offer
        url, msg_id = os.getenv("DISCORD_OFFERS_WEBHOOK_URL"), msg_id.split(":", 1)[1]
    if not msg_id or not url or DRY_RUN:
        return
    mins = None
    if posted_at:
        mins = round((now - datetime.fromisoformat(posted_at.replace("Z", "+00:00"))).total_seconds() / 60)
    when = f"Gone after {mins} min" if mins is not None and mins < 120 else "Gone"
    try:
        r = requests.get(f"{url}/messages/{msg_id}", timeout=15)
        if not r.ok:
            return
        embeds = r.json().get("embeds") or []
        if embeds:
            e = embeds[0]
            e["color"] = 0x808080
            if not str(e.get("title", "")).startswith("❌"):
                e["title"] = ("❌ GONE · " + str(e.get("title", "")))[:256]
            e["fields"] = [f for f in e.get("fields") or [] if f.get("name") != "❌ Status"] + [
                {"name": "❌ Status", "value": f"{when} (sold or ended on eBay)", "inline": False}]
        requests.patch(f"{url}/messages/{msg_id}", timeout=15,
                       json={"content": f"❌ {when}", "embeds": embeds, "allowed_mentions": {"parse": []}})
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        print(f"Card edit skipped: {e}", file=sys.stderr)


def survival_checks(token: str) -> None:
    """A1: for deals posted in the last ~25 minutes, check at ~1, 2, 3, 5, 10 and 20 minutes whether they can
    still be bought, and record when they went. About 6 eBay calls per posted deal."""
    if DRY_RUN:
        return
    now = datetime.now(timezone.utc)
    rows = sb_get({"select": "item_id,title,all_in,posted_at,live_checks,discord_msg_id", "posted_at": f"gte.{(now - timedelta(minutes=25)).isoformat()}",
                   "gone_at": "is.null", "live_checks": f"lt.{len(SURVIVAL_MIN)}", "limit": "20"})
    for row in rows:
        posted = datetime.fromisoformat(row["posted_at"].replace("Z", "+00:00"))
        done = int(row.get("live_checks") or 0)
        if not survival_due(posted, done, now):
            continue
        USAGE["ebay"] += 1
        r = requests.get(f"{EBAY_API}/buy/browse/v1/item/{row['item_id']}", headers=ebay_headers(token), timeout=20)
        mins = round((now - posted).total_seconds() / 60, 1)
        if r.status_code == 404 or (r.ok and any(a.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK"
                                                  for a in r.json().get("estimatedAvailabilities") or [])):
            patch = {"gone_at": now.isoformat(), "outcome": "gone", "live_checks": done + 1, "last_checked": now.isoformat()}
            LOG.append(f"⏱️ {short(row)}: gone {mins} min after posting")
            mark_card_gone(row.get("discord_msg_id"), row["posted_at"], now)
        elif r.ok:
            patch = {"last_live_at": now.isoformat(), "live_checks": done + 1, "last_checked": now.isoformat()}
            LOG.append(f"⏱️ {short(row)}: still for sale {mins} min after posting")
        else:
            continue                                            # eBay hiccup: try again next run
        requests.patch(f"{os.environ['SUPABASE_URL']}/rest/v1/{TABLE}", headers=sb_headers(), timeout=20,
                       params={"item_id": f"eq.{row['item_id']}"}, json=patch)


def track_outcomes(token: str) -> None:
    """Feedback loop: re-check a few earlier alerts each run and record when they disappear (sold or ended)."""
    n = CONFIG.get("track_per_run", 5)
    if not n or DRY_RUN:
        return
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    hour_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    rows = sb_get({"select": "item_id,first_seen,posted_at,discord_msg_id", "status": "neq.rejected", "verdict": "in.(DEAL,OFFER)",
                   "outcome": "is.null", "and": f"(first_seen.gte.{week_ago},first_seen.lte.{hour_ago})",
                   "order": "last_checked.asc.nullsfirst", "limit": str(n)})
    now = datetime.now(timezone.utc).isoformat()
    if rows:
        LOG.append(f"📦 Tracker: re-checked {len(rows)} earlier alert(s)")
    for row in rows:
        USAGE["ebay"] += 1
        r = requests.get(f"{EBAY_API}/buy/browse/v1/item/{row['item_id']}", headers=ebay_headers(token), timeout=20)
        patch = {"last_checked": now}
        if r.status_code == 404:
            patch.update(outcome="gone", gone_at=now)          # sold or ended since the last check
            LOG.append(f"📦 Tracker: {row['item_id']} has gone (sold or ended)")
            mark_card_gone(row.get("discord_msg_id"), row.get("posted_at"), datetime.now(timezone.utc))
        elif r.ok:
            d = r.json()
            if any(a.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK" for a in d.get("estimatedAvailabilities") or []):
                patch.update(outcome="gone", gone_at=now)
                LOG.append(f"📦 Tracker: {row['item_id']} sold out")
                mark_card_gone(row.get("discord_msg_id"), row.get("posted_at"), datetime.now(timezone.utc))
            else:
                patch["current_price"] = float((d.get("price") or {}).get("value") or 0) or None
                if row["first_seen"] < (datetime.now(timezone.utc) - timedelta(days=6, hours=23)).isoformat():
                    patch["outcome"] = "unsold_7d"
        else:
            continue
        requests.patch(f"{os.environ['SUPABASE_URL']}/rest/v1/{TABLE}", headers=sb_headers(),
                       params={"item_id": f"eq.{row['item_id']}"}, json=patch, timeout=20)


# ---------- AI calls (shared) ----------
# Free-tier daily allowances run out (25 Nov: both Flash-Lite models were used up by lunchtime, so the photo check
# was skipped and a "dead" PS5 got posted). So: models whose DAILY allowance is used up are remembered in
# bot_state 'ai_exhausted' (shared with the deal engine) and skipped until Google's reset; busy models (503) get
# one more try, and the bulk title screen stops once only `ai_reserve_for_checks` calls are left today, so the
# photo + description check on real alerts (and the deal engine) always has allowance. (Gemma 4 was tried on 25 Nov:
# 38 s per answer with its reasoning printed in the reply, too slow for a bot that runs every minute.)
EXHAUSTED: set[str] = set()
_EXH_LOADED = [False]


def _load_exhausted() -> None:
    if _EXH_LOADED[0] or DRY_RUN or not os.getenv("SUPABASE_URL"):
        return
    _EXH_LOADED[0] = True
    try:
        st = requests.get(f"{os.environ['SUPABASE_URL']}/rest/v1/bot_state", headers=sb_headers(), timeout=10,
                          params={"select": "value", "key": "eq.ai_exhausted"}).json()
        v = json.loads(st[0]["value"]) if st else {}
        if v.get("day") == usage_day().isoformat():
            EXHAUSTED.update(v.get("models") or [])
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        pass


def _save_exhausted(model: str) -> None:
    EXHAUSTED.add(model)
    LOG.append(f"🤖 {model}: free daily allowance used up, skipped until 08:00 UK")
    if DRY_RUN or not os.getenv("SUPABASE_URL"):
        return
    try:
        requests.post(f"{os.environ['SUPABASE_URL']}/rest/v1/bot_state", timeout=10,
                      headers={**sb_headers(), "Prefer": "resolution=merge-duplicates"},
                      json={"key": "ai_exhausted", "value": json.dumps({"day": usage_day().isoformat(), "models": sorted(EXHAUSTED)})})
    except requests.RequestException:
        pass


def ai_allowance_left() -> int | None:
    """Free AI calls left today across the models in usage_limits (both bots count into api_usage). None if unknown."""
    limits = {k: v for k, v in CONFIG.get("usage_limits", {}).items() if k != "ebay"}
    if DRY_RUN or not os.getenv("SUPABASE_URL") or not limits:
        return None
    try:
        rows = requests.get(f"{os.environ['SUPABASE_URL']}/rest/v1/api_usage", headers=sb_headers(), timeout=10,
                            params={"select": "name,calls", "day": f"eq.{usage_day().isoformat()}"}).json()
        used = {r["name"]: int(r["calls"] or 0) for r in rows}
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    _load_exhausted()
    return sum(0 if m in EXHAUSTED else max(0, lim - used.get(m, 0) - USAGE.get(m, 0)) for m, lim in limits.items())


def ai_body(model: str, parts: list, max_tokens: int) -> dict:
    """Gemma models don't support JSON mode, so they get plain text (the answer is still parsed as JSON)."""
    cfg = {"temperature": 0, "maxOutputTokens": max_tokens}
    if not model.startswith("gemma"):
        cfg["responseMimeType"] = "application/json"
    return {"contents": [{"parts": parts}], "generationConfig": cfg}


def ai_call(models: list[str], parts: list, max_tokens: int, timeout: int = 15) -> tuple[str | None, str]:
    """Try each model in order. Returns (answer text, model) or (None, "")."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key or DRY_RUN:
        return None, ""
    _load_exhausted()
    busy = []
    for attempt in (0, 1):
        todo = [m for m in models if m not in EXHAUSTED] if attempt == 0 else busy
        if attempt == 1:
            if not busy:
                break
            time.sleep(1.5)
        for model in todo:
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                  headers={"x-goog-api-key": key}, json=ai_body(model, parts, max_tokens),
                                  timeout=timeout + (10 if model.startswith("gemma") else 0))
            except requests.RequestException:
                if attempt == 0:
                    busy.append(model)
                continue
            if r.status_code == 429:
                LIMITED[model] += 1
                if re.search(r"PerDay|per day|daily", getattr(r, "text", "") or "", re.I):
                    _save_exhausted(model)
            elif r.ok:
                USAGE[model] += 1                      # only answered calls count as usage
            if not r.ok:
                print(f"AI {model}: HTTP {r.status_code}", file=sys.stderr)
                if r.status_code in (500, 503) and attempt == 0:
                    busy.append(model)
                continue
            try:
                parts_out = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts_out if not p.get("thought"))
                return text.replace("```json", "").replace("```", ""), model
            except (KeyError, IndexError, ValueError):
                continue
    return None, ""


# ---------- AI check (Gemini) ----------

AI_PROMPT = """You check eBay UK listings for a reseller. Product wanted: {product} ({model}).
Decide if this listing is a COMPLETE, WORKING {model} that can be resold as one.
Answer "no" if: it is an accessory, box, game, part, code or a different product/model; it is faulty, broken, for parts, untested, locked or banned; or the photo clearly shows a different item or serious damage.
READ THE WHOLE DESCRIPTION: sellers often hide the fault there while the title looks normal. If the seller notes or description say it is dead, won't turn on / no power, has a fault (e.g. no display/HDMI fault, disc drive not working, overheating, blue light, error codes), is for spares or repair, or the account/console is banned or locked, answer "no" with code "faulty", even if the title and photo look fine.
Answer "unsure" if key facts are missing (e.g. stock photo plus vague text, or main parts like controllers/charger missing).
{extra}Otherwise answer "yes".
If (and only if) you answer "no", also give "code", exactly one of: accessory, different_model, faulty, missing_essential_part, not_the_item, other.
Never answer "no" because the price looks low, or because of your own beliefs about model numbers or serial numbers; use "unsure" if that is your only worry.
Reply with JSON only: {{"verdict": "yes|no|unsure", "code": "reason code, only for no", "reason": "max 15 words"}}

LISTING
Title: {title}
Price: £{price} (all-in £{all_in})
Condition: {condition}
Seller notes: {cond_desc}
Item specifics: {aspects}
Description: {desc}
"""


PAD_RULE = """It MUST come with at least one working controller (as standard in the box). Answer "no" if the text or photo shows it comes without one (e.g. "console only", "no controller"). If a controller is neither mentioned nor visible, answer "unsure" with reason "controller not confirmed".
"""


def _strip_html(h: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h or "")).strip()


BLOCK_CODES = ("accessory", "different_model", "faulty", "missing_essential_part", "not_the_item")   # the only reasons allowed to block


def apply_ai_rules(verdict: str, code: str | None, reason: str) -> tuple[str, str]:
    """B2: the AI may only block (say "no") for a listed reason code. Any other "no" becomes "unsure" so a human looks."""
    if verdict == "no" and (code or "").strip().lower() not in BLOCK_CODES:
        return "unsure", ("(AI wanted to block for an unlisted reason) " + reason)[:160]
    return verdict, reason


def ai_check(cat: dict, row: dict, details: dict) -> tuple[str, str, str]:
    """Ask Gemini whether the listing is the real, complete, working item. Returns (verdict, reason, model)."""
    if not os.environ.get("GEMINI_API_KEY") or DRY_RUN:
        return "skipped", "AI check not configured", ""
    d = details or {}
    aspects = "; ".join(f"{a.get('name')}: {a.get('value')}" for a in (d.get("localizedAspects") or [])[:15])
    prompt = AI_PROMPT.format(
        product=cat.get("label", cat["id"]), model=row.get("variant") or cat.get("label"), extra=PAD_RULE if cat.get("needs_controller") else "",
        title=row.get("title"), price=row.get("price"), all_in=row.get("all_in"),
        condition=d.get("condition", "?"), cond_desc=(d.get("conditionDescription") or "-")[:400],
        aspects=aspects[:600] or "-", desc=listing_text(d)[:3000] or "-")
    parts = [{"text": prompt}]
    img = ((d.get("image") or {}).get("imageUrl")) or row.get("image_url")
    if img:
        try:
            ir = requests.get(img, timeout=5)
            if ir.ok and len(ir.content) < 4_000_000:
                parts.append({"inline_data": {"mime_type": ir.headers.get("Content-Type", "image/jpeg").split(";")[0],
                                              "data": base64.b64encode(ir.content).decode()}})
        except requests.RequestException:
            pass
    text, model = ai_call(CONFIG.get("ai_models", []), parts, 160, timeout=12)
    if text is not None:
        try:
            out = json.loads(text[text.find("{"): text.rfind("}") + 1])
            verdict = str(out.get("verdict", "unsure")).lower()
            verdict = verdict if verdict in ("yes", "no", "unsure") else "unsure"
            verdict, reason = apply_ai_rules(verdict, str(out.get("code") or ""), str(out.get("reason", ""))[:120])
            return verdict, reason, model
        except (KeyError, IndexError, ValueError, AttributeError):
            pass
    return "unavailable", "AI check unavailable right now", ""


SCREEN_PROMPT = """You screen eBay UK listings for a reseller who buys and resells working units.
For each listing, decide if it is selling the PRODUCT for its category.
"yes" = the actual unit is for sale and presumably works. Bundles with extra games, controllers or accessories are fine.
Cosmetic wear, light scratches, no original box or missing manuals are fine.
"no" = only an accessory, game, part, case, strap, controller, charger or box; a different model or product;
faulty, broken, for parts, locked or banned; a "wanted" ad; or clearly not the product.
Also "no" if an essential part is missing (e.g. VR headset without its controllers, console or handheld without
its controller/power lead), because resale prices assume a complete unit.
If "yes", set "m" to the matching model from that product's list when the title makes it clear, otherwise "".

PRODUCTS
{products}

LISTINGS (index | product | all-in price | condition | title)
{listings}

Reply with a JSON array only, one object per listing:
[{{"i": 0, "v": "yes|no", "m": "model or empty", "r": "reason, max 8 words"}}]
"""


def ai_screen(batch: list[tuple[dict, dict]]) -> dict | None:
    """Text-only batch check of listings the word filters dropped. One Gemini request per ~30 listings.
    Returns {item_id: (verdict, model_name, reason, ai_model)} or None if Gemini is unavailable."""
    if not os.environ.get("GEMINI_API_KEY") or DRY_RUN or not batch:
        return None
    out = {}
    size = CONFIG.get("ai_screen_batch", 30)
    for k in range(0, len(batch), size):
        part = batch[k:k + size]
        cats = {cat["id"]: cat for cat, _ in part}
        products = "\n".join(f"[{cid}] {c.get('ai_hint') or c.get('label', cid)} Models: "
                             + "; ".join(m["name"] for m in c["models"]) for cid, c in cats.items())
        listings = "\n".join(f"{i} | {cat['id']} | £{c['all_in']:.0f} | {c.get('condition') or '?'} | {c['title']}"
                             for i, (cat, c) in enumerate(part))
        got = None
        text, model = ai_call(CONFIG.get("ai_screen_models") or CONFIG.get("ai_models", []),
                              [{"text": SCREEN_PROMPT.format(products=products, listings=listings)}],
                              60 * len(part) + 200, timeout=30)
        if text is not None:
            try:
                got = (json.loads(text[text.find("["): text.rfind("]") + 1]), model)
            except (ValueError, AttributeError):
                got = None
        if got is None:
            return out or None                                  # whatever was screened so far still counts
        answers, model = got
        for a in answers if isinstance(answers, list) else []:
            try:
                cat, c = part[int(a.get("i"))]
            except (TypeError, ValueError, IndexError):
                continue
            v = str(a.get("v", "")).lower()
            if v not in ("yes", "no"):
                continue
            names = {m["name"] for m in cat["models"]}
            out[c["item_id"]] = (v, a.get("m") if a.get("m") in names else "", str(a.get("r", ""))[:80], model)
    return out


# ---------- Supabase ----------

def sb_headers():
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return {"apikey": key, "Content-Type": "application/json"}


def already_seen(ids: list[str]) -> dict[str, float]:
    """{item_id: all_in price when we last looked} for listings we've already handled."""
    if not ids or DRY_RUN:
        return {}
    out = {}
    ids = list(dict.fromkeys(ids))
    for k in range(0, len(ids), 150):                    # keep each URL short
        quoted = ",".join('"' + i.replace('"', "") + '"' for i in ids[k:k + 150])
        r = requests.get(f"{os.environ['SUPABASE_URL']}/rest/v1/{TABLE}", headers=sb_headers(), timeout=20,
                         params={"select": "item_id,all_in,status,variant,category", "item_id": f"in.({quoted})"})
        r.raise_for_status()
        for row in r.json():
            if row.get("status") == "ai_ok":
                AI_OK[row["item_id"]] = row.get("variant") or ""
            elif row.get("status") == "ai_screened_out":
                SCREENED_IN[row["item_id"]] = row.get("category") or ""
                out[row["item_id"]] = float(row["all_in"] or 0)
            elif row.get("status") != "retry":
                out[row["item_id"]] = float(row["all_in"] or 0)
    return out


def has_history() -> bool:
    """False on the very first real run, so old listings already on eBay don't all alert at once."""
    r = requests.get(f"{os.environ['SUPABASE_URL']}/rest/v1/{TABLE}", headers=sb_headers(),
                     params={"select": "item_id", "status": "neq.test", "limit": 1}, timeout=20)
    r.raise_for_status()
    return bool(r.json())


def save_new(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    keys = set().union(*rows)                       # bulk insert needs identical keys on every row
    rows = [{k: r.get(k) for k in keys} | {"status": r.get("status") or "new"} for r in rows]
    r = requests.post(f"{os.environ['SUPABASE_URL']}/rest/v1/{TABLE}?on_conflict=item_id",
                      headers={**sb_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
                      json=rows, timeout=20)
    r.raise_for_status()
    return r.json()


# ---------- API usage (#api-usage) ----------

PACIFIC, UK = ZoneInfo("America/Los_Angeles"), ZoneInfo("Europe/London")
USAGE_NAMES = {"gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite", "gemini-3.1-flash-lite": "Gemini 3.1 Flash-Lite",
               "gemma-4-26b-a4b-it": "Gemma 4 26B", "gemma-4-31b-it": "Gemma 4 31B", "ebay": "eBay searches + lookups"}


def usage_day(now: datetime | None = None):
    """Gemini's free daily limits reset at midnight Pacific (08:00 UK), so the bot counts days the same way."""
    return (now or datetime.now(timezone.utc)).astimezone(PACIFIC).date()


def bar(used: int, limit: int, width: int = 12) -> str:
    filled = min(width, round(width * used / limit)) if limit else 0
    return "▰" * filled + "▱" * (width - filled)


def render_usage(today: dict, week: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    reset = datetime.combine(usage_day(now) + timedelta(days=1), datetime.min.time(), PACIFIC)
    left = reset - now.astimezone(PACIFIC)
    h, m = int(left.total_seconds() // 3600), int(left.total_seconds() % 3600 // 60)
    limits = CONFIG.get("usage_limits", {})
    lines = [f"Resets {reset.astimezone(UK):%H:%M} UK (in {h}h {m}m)", ""]
    ai_left = 0
    for name, limit in limits.items():
        used, hit = today.get(name, (0, 0))
        lines.append(f"{USAGE_NAMES.get(name, name)}")
        lines.append(f"{bar(used, limit)} {used:,} / {limit:,} · {max(0, limit - used):,} left"
                     + (f" · hit limit {hit}x" if hit else ""))
        if name != "ebay":
            ai_left += max(0, limit - used)
        lines.append("")
    lines.append(f"AI checks left today: ~{ai_left:,}")
    g7 = sum(v for k, v in week.items() if k != "ebay")
    lines.append(f"Last 7 days: {g7:,} AI checks · {week.get('ebay', 0):,} eBay calls")
    return "```\n" + "\n".join(lines) + "\n```" + f"\nUpdated {now.astimezone(UK):%H:%M} UK · counted by the bot, no extra API calls"


def report_usage() -> None:
    """Add this run's calls to the daily totals and refresh the single #api-usage message (edited, never re-posted)."""
    if DRY_RUN:
        return
    base = os.environ["SUPABASE_URL"]
    day = usage_day()
    try:
        if USAGE or LIMITED:
            rows = [{"name": n, "calls": USAGE.get(n, 0), "limited": LIMITED.get(n, 0)} for n in set(USAGE) | set(LIMITED)]
            requests.post(f"{base}/rest/v1/rpc/bump_usage", headers=sb_headers(),
                          json={"p_day": day.isoformat(), "p_rows": rows}, timeout=15).raise_for_status()
        url = os.getenv("DISCORD_USAGE_WEBHOOK_URL")
        if not url:
            return
        got = requests.get(f"{base}/rest/v1/api_usage", headers=sb_headers(), timeout=15,
                           params={"select": "day,name,calls,limited", "day": f"gte.{(day - timedelta(days=6)).isoformat()}"}).json()
        today = {r["name"]: (r["calls"], r["limited"]) for r in got if r["day"] == day.isoformat()}
        week = Counter()
        for r in got:
            week[r["name"]] += r["calls"]
        payload = {"embeds": [{"title": "📊 API usage today", "description": render_usage(today, week), "color": 0x5865F2}],
                   "allowed_mentions": {"parse": []}}
        st = requests.get(f"{base}/rest/v1/bot_state", headers=sb_headers(), timeout=15,
                          params={"select": "value", "key": "eq.usage_msg_id"}).json()
        msg_id = st[0]["value"] if st else None
        if msg_id:
            r = requests.patch(f"{url}/messages/{msg_id}", json=payload, timeout=15)
            if r.ok:
                return
        r = requests.post(url, params={"wait": "true"}, json={**payload, "flags": 4096}, timeout=15)
        r.raise_for_status()
        requests.post(f"{base}/rest/v1/bot_state", timeout=15,
                      headers={**sb_headers(), "Prefer": "resolution=merge-duplicates"},
                      json={"key": "usage_msg_id", "value": r.json()["id"]})
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"Usage report skipped: {e}", file=sys.stderr)


# ---------- Discord ----------

STYLE = {
    "HOT":         ("🔥 HOT DEAL: buy fast", 0xE74C3C),
    "GEM":         ("💎 Hidden gem", 0x9B59B6),
    "DEAL":        ("🟢 Deal", 0x2ECC71),
    "OFFER":       ("🟡 Send an offer", 0xF1C40F),
    "UNCONFIRMED": ("❓ Unconfirmed: check photos", 0x95A5A6),
}


def post_discord(payload: dict, daily: bool = False, url: str | None = None) -> str | None:
    """Posts to Discord and returns the new message's id (so the card can be found again later)."""
    url = url or (os.getenv("DISCORD_DAILY_WEBHOOK_URL") if daily else None) or os.environ["DISCORD_WEBHOOK_URL"]
    r = requests.post(url, params={"wait": "true"}, json=payload, timeout=20)
    if r.status_code == 429:                                  # Discord rate limit: wait and retry once
        time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
        r = requests.post(url, params={"wait": "true"}, json=payload, timeout=20)
    r.raise_for_status()
    time.sleep(0.6)
    try:
        return str(r.json().get("id") or "") or None
    except (ValueError, AttributeError, TypeError):            # no id in the reply: the alert still went out
        return None


def post_logs() -> None:
    """Under-the-hood log for #bot-logs: silent (no pings, no notifications), costs no eBay or Gemini calls."""
    url = os.getenv("DISCORD_LOG_WEBHOOK_URL")
    if not url or not LOG or DRY_RUN:
        return
    chunks, cur = [], ""
    for line in LOG:
        line = line[:300]
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur); cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks[:5]:
        try:
            r = requests.post(url, json={"content": c, "allowed_mentions": {"parse": []}, "flags": 4096}, timeout=15)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                requests.post(url, json={"content": c, "allowed_mentions": {"parse": []}, "flags": 4096}, timeout=15)
            time.sleep(0.6)
        except requests.RequestException as e:
            print(f"Log post failed: {e}", file=sys.stderr)


def short(row: dict) -> str:
    return f"{row.get('title', '')[:60]} · £{float(row.get('all_in') or 0):.0f}"


def price_age(checked_iso: str | None, today) -> tuple[str, bool]:
    """B5: ('prices checked 11 Nov', is_it_over_14_days_old). Empty text if the category has no date."""
    try:
        d = datetime.fromisoformat(str(checked_iso)).date()
    except ValueError:
        return "", False
    return f"prices checked {d.day} {d:%b}", (today - d).days > 14


def post_ai_blocked(rows: list[dict]) -> None:
    """B2 part 2: show every deal the AI photo/text check blocked in the private #ai-blocked channel,
    so a good deal blocked by mistake can still be spotted. Silent (no pings). Needs DISCORD_AIBLOCKED_WEBHOOK_URL."""
    url = os.getenv("DISCORD_AIBLOCKED_WEBHOOK_URL")
    if not url or DRY_RUN or not rows:
        return
    for row in rows:
        payload = {"allowed_mentions": {"parse": []}, "flags": 4096, "embeds": [{
            "title": f"🚫 AI blocked: {str(row.get('title', ''))[:230]}", "url": row.get("url"), "color": 0x808080,
            "description": (f"**Why:** {row.get('ai_reason') or 'no reason given'}\n"
                            f"£{float(row.get('all_in') or 0):.2f} all-in · {row.get('verdict')} · "
                            f"~£{float(row.get('safe_profit') or 0):.0f} at an average price"),
            "footer": {"text": f"{row.get('category')} · {row.get('ai_model') or 'AI'}"}}]}
        if row.get("image_url"):
            payload["embeds"][0]["thumbnail"] = {"url": row["image_url"]}
        try:
            requests.post(url, json=payload, timeout=15)
            time.sleep(0.6)
        except requests.RequestException as e:
            print(f"AI-blocked post skipped: {e}", file=sys.stderr)


def mark_run_finished() -> None:
    """A6: record when a scan last finished, so the database's heartbeat check can warn if the bot stops."""
    base = os.getenv("SUPABASE_URL")
    if not base or DRY_RUN:
        return
    try:
        requests.post(f"{base}/rest/v1/bot_state", timeout=15,
                      headers={**sb_headers(), "Prefer": "resolution=merge-duplicates"},
                      json={"key": "last_run", "value": datetime.now(timezone.utc).isoformat()})
    except requests.RequestException as e:
        print(f"Heartbeat save skipped: {e}", file=sys.stderr)


def discord_alert(row: dict, offers: bool = False) -> str | None:
    key = ("OFFER" if offers else "HOT" if row["rating"] == "HOT" else
           "UNCONFIRMED" if row["lane"] == "UNCONFIRMED" else
           "GEM" if row["lane"] == "GEM" else row["verdict"])
    label, colour = STYLE[key]
    price_line = f"£{row['all_in']:.2f} all-in (target £{row['buy_target']:.0f})"
    if row.get("suggested_offer"):
        price_line += f"\n**Offer £{row['suggested_offer']}** + postage"
    fields = [
        {"name": "💷 Price", "value": price_line, "inline": True},
        {"name": "💰 Est. profit", "value": (f"~£{row['profit']:.0f} if it resells ~£{row['expected_sell']}\n"
                                             f"~£{row.get('safe_profit') or 0:.0f} at an average price · need £{row['required_profit']}"), "inline": True},
        {"name": "👤 Seller", "value": f"{row['seller_feedback']} feedback · {row['seller_pct']}%", "inline": True},
    ]
    checked, stale = price_age(CAT_BY_ID.get(row.get("category"), {}).get("prices_checked"), datetime.now(timezone.utc).date())
    card_notes = (row["notes"].split("\n") if row.get("notes") else []) + (
        ["⚠️ PRICES 2+ WEEKS OLD: check sold listings"] if stale else [])
    if card_notes:
        fields.append({"name": "📝 Notes", "value": "\n".join(card_notes)[:1000], "inline": False})
    embed = {
        "author": {"name": f"{label} · {row['variant']}"},
        "title": row["title"][:250], "url": row["url"], "color": colour, "fields": fields,
        "footer": {"text": f"{row['category']} · {row['lane'].lower()} · rating {row['rating'].lower()}" + (f" · {checked}" if checked else "")},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    badge = delivery_badge(row)
    if badge:
        embed["description"] = badge
    if row.get("image_url"):
        embed["thumbnail"] = {"url": row["image_url"]}
    ping = not offers and (key == "HOT" or (row["verdict"] == "DEAL" and row["rating"] == "SOLID"))   # only good deals buzz your phone
    payload = {"embeds": [embed], "allowed_mentions": {"parse": ["everyone"] if ping else []}}
    if ping:
        payload["content"] = "@everyone 🔥 Hot one, act fast" if key == "HOT" else "@everyone 🟢 Good deal"
    return post_discord(payload, url=os.environ["DISCORD_OFFERS_WEBHOOK_URL"] if offers else None)


# ---------- main ----------

CAT_BY_ID = {c["id"]: c for c in CONFIG["categories"]}


def for_offers(row: dict) -> bool:
    """Goes to #send-an-offer: consoles with the offer lane (standard PS5, Xbox Series X), offers only (and every
    alert of an offers_only category), and only with at least the lane's minimum profit at an AVERAGE resale price."""
    cat = CAT_BY_ID.get(row.get("category"), {})
    lane = cat.get("offer_channel")
    if not lane or row.get("verdict") not in ("DEAL", "OFFER") or row.get("status") in ("ai_rejected", "gone_before_post"):
        return False
    if row["verdict"] == "DEAL" and not cat.get("offers_only"):
        return False                                          # real deals for these keep going to #deals
    return float(row.get("safe_profit") or 0) >= lane["min_safe_profit"]


def for_deals(row: dict) -> bool:
    """Goes to #deals (unchanged rules), except categories that only use #send-an-offer."""
    return (row.get("verdict") == "DEAL" and not quiet(row) and row.get("status") not in ("ai_rejected", "gone_before_post")
            and not CAT_BY_ID.get(row.get("category"), {}).get("offers_only"))


def quiet(row: dict) -> bool:
    """Thin-margin 'check' items are saved (and can appear in the digest) but not posted live."""
    need = float(row.get("required_profit") or 50)
    return row.get("rating") == "CHECK" and float(row.get("safe_profit") or 0) < need / 2


def newest_age_min(items: list[dict], now: datetime | None = None) -> float | None:
    """A4: how many minutes old the newest listing in a search is, i.e. roughly eBay's delay in showing new listings."""
    times = []
    for it in items:
        try:
            times.append(datetime.fromisoformat(str(it.get("itemCreationDate")).replace("Z", "+00:00")))
        except ValueError:
            continue
    if not times:
        return None
    return round(((now or datetime.now(timezone.utc)) - max(times)).total_seconds() / 60, 1)


FULL_SCAN_KEY = "last_full_scan"     # bot_state row holding the time of the last finished full scan (ISO text)


def full_due_since(last_iso: str | None, now: datetime, every_min: float) -> bool:
    """Pure decision: has it been at least every_min minutes since the last full scan? No (or unreadable) record = due."""
    if not every_min or not last_iso:
        return True
    try:
        last = datetime.fromisoformat(str(last_iso).replace("Z", "+00:00"))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= timedelta(minutes=every_min)


def read_last_full_scan() -> tuple[bool, str | None]:
    """(readable, timestamp). readable is False when bot_state can't be read, so the caller uses the clock fallback."""
    base = os.getenv("SUPABASE_URL")
    if not base or DRY_RUN:
        return False, None
    try:
        r = requests.get(f"{base}/rest/v1/bot_state", headers=sb_headers(), timeout=15,
                         params={"select": "value", "key": f"eq.{FULL_SCAN_KEY}"})
        r.raise_for_status()
        rows = r.json()
        return True, (rows[0]["value"] if rows else None)
    except (requests.RequestException, KeyError, ValueError, TypeError, IndexError) as e:
        print(f"Last full scan read skipped: {e}", file=sys.stderr)
        return False, None


def save_last_full_scan() -> None:
    """Remember that a full scan has just finished (same upsert style as the eBay token cache)."""
    base = os.getenv("SUPABASE_URL")
    if not base or DRY_RUN:
        return
    try:
        requests.post(f"{base}/rest/v1/bot_state", timeout=15,
                      headers={**sb_headers(), "Prefer": "resolution=merge-duplicates"},
                      json={"key": FULL_SCAN_KEY, "value": datetime.now(timezone.utc).isoformat()})
    except requests.RequestException as e:
        print(f"Last full scan save skipped: {e}", file=sys.stderr)


def full_scan_due() -> bool:
    """'Fast' categories run every time; everything else once at least full_scan_every_min minutes after the last
    full scan (read from Supabase). If that can't be read, fall back to the old clock-minute rule."""
    if os.environ.get("FULL_SCAN") == "1":
        return True
    every = CONFIG.get("full_scan_every_min", 0)
    readable, last = read_last_full_scan()
    if readable:
        return full_due_since(last, datetime.now(timezone.utc), every)
    return not every or datetime.now(timezone.utc).minute % every < CONFIG.get("run_every_min", 3)


def screen_soft(old: dict, cand_ids: set, fresh: list) -> list[dict]:
    """AI screen for listings only the word filters dropped. Rescued ones join `fresh` (they still face the
    price maths, the listing-details checks and the photo check). Ones the AI rejects are returned as REJECT
    rows so they're remembered and never screened twice."""
    todo, seen = [], set()
    for cat, c in SOFT:
        if c["item_id"] in cand_ids or c["item_id"] in seen:
            continue
        if c["item_id"] in old and SCREENED_IN.get(c["item_id"], cat["id"]) == cat["id"]:
            continue                                     # already judged for this category
        seen.add(c["item_id"])
        if c["item_id"] in AI_OK:                        # approved on an earlier run, waiting for a lookup slot
            m = next((m for m in cat["models"] if m["name"] == AI_OK[c["item_id"]]), None)
            fresh.append((cat, {**c, "lane": "EXACT" if m else "VAGUE", "model": m["name"] if m else None,
                                "rescued": c["soft_reason"]}))
            continue
        todo.append((cat, c))
    if not todo:
        return []
    left = ai_allowance_left()
    if left is not None and left <= CONFIG.get("ai_reserve_for_checks", 0):
        LOG.append(f"🤖 AI screen paused: {left} AI calls left today are kept for photo checks "
                   f"({len(todo)} filtered listing(s) skipped)")
        return []
    cap = CONFIG.get("ai_screen_max", 60)
    waiting = len(todo) - cap
    todo = todo[:cap]
    verdicts = ai_screen(todo)
    if not verdicts:
        LOG.append(f"🤖 AI screen unavailable: {len(todo)} filtered listing(s) wait for the next run")
        return []
    rejects, rescued, nos = [], 0, []
    for cat, c in todo:
        if c["item_id"] not in verdicts:
            continue                                     # not answered: retried next run
        v, model_name, reason, ai_model = verdicts[c["item_id"]]
        if v == "yes":
            model = next((m for m in cat["models"] if m["name"] == model_name), None)
            if model:
                target = cat_target(cat, model)
                if c["all_in"] > target and not (c["best_offer"] and c["all_in"] <= target * (1 + offer_zone(cat))):
                    nos.append(f"{short(c)}: genuine {model_name}, but too pricey")
                    rejects.append({**{k: v2 for k, v2 in c.items() if k not in DROP}, "verdict": "REJECT",
                                    "status": "rejected", "ai_verdict": "yes", "ai_reason": "genuine but too pricey",
                                    "ai_model": ai_model})
                    continue
            fresh.append((cat, {**c, "lane": "EXACT" if model else "VAGUE", "model": model["name"] if model else None,
                                "rescued": c["soft_reason"]}))
            rescued += 1
            LOG.append(f"🤖✅ Rescued {short(c)} (filters said: {c['soft_reason']}; AI: {reason})")
        else:
            nos.append(f"{short(c)}: {reason}")
            rejects.append({**{k: v2 for k, v2 in c.items() if k not in DROP}, "verdict": "REJECT",
                            "status": "ai_screened_out", "ai_verdict": "no", "ai_reason": reason, "ai_model": ai_model})
    LOG.append(f"🤖 AI screened {len(todo)} filtered listing(s): {rescued} rescued, {len(nos)} confirmed not worth it"
               + (f" · {waiting} more next run" if waiting > 0 else ""))
    LOG.extend(f"   ✖ {n}" for n in nos[:12])
    if len(nos) > 12:
        LOG.append(f"   … and {len(nos) - 12} more")
    return rejects


def run_sweeps(token: str, full: bool, minute: int) -> dict[str, list[dict]]:
    """Category sweeps: one newest-first search over a whole eBay category (no keyword, up to 200 results)
    covers every product in it, so many products cost one eBay call. Returns {category id: items}."""
    out: dict[str, list[dict]] = {}
    enabled = {c["id"] for c in CONFIG["categories"] if c.get("enabled") and c["models"]}
    for s in CONFIG.get("sweeps", []):
        members = [cid for cid in s["cats"] if cid in enabled]
        if not members or not (full or minute % s.get("every_min", 1) == 0):
            continue
        try:
            items = ebay_search(token, s, None, category_ids=s["category_ids"], limit=s.get("limit", 200))
        except requests.HTTPError as e:
            print(f"[sweep {s['id']}] failed: {e}", file=sys.stderr)
            LOG.append(f"⚠️ sweep {s['id']} failed ({e.response.status_code if e.response is not None else 'error'}): "
                       "falling back to keyword searches")
            continue                                   # members fall back to their own keyword searches
        age = newest_age_min(items)
        LOG.append(f"🧹 sweep `{s['id']}`: {len(items)} listings for {', '.join(members)}"
                   + (f" · newest {age} min old" if age is not None else ""))
        for cid in members:
            out[cid] = items
    return out


def main() -> int:
    token = ebay_token()
    full = full_scan_due()
    minute = datetime.now(timezone.utc).minute
    swept = run_sweeps(token, full, minute)          # one search per eBay category, shared by many products
    cats = [c for c in CONFIG["categories"] if c.get("enabled") and c["models"]
            and (full or c["id"] in swept or (c.get("fast") and minute % c.get("fast_every_min", 1) == 0))]
    print("Full scan" if full else "Fast scan: " + ", ".join(c["id"] for c in cats))
    LOG.insert(0, f"**{'Full' if full else 'Fast'} scan** · {datetime.now(timezone.utc).strftime('%H:%M')} UTC")
    cands = []
    for cat in cats:
        seen_ids, ages = set(), []
        sources = [(None, swept[cat["id"]])] if cat["id"] in swept else []
        if full or cat["id"] not in swept:            # keyword searches: full scans (safety net) or unswept products
            sources += [(q, None) for q in (cat["queries"] if full else cat.get("fast_queries", cat["queries"]))]
        for q, items in sources:
            if items is None:
                try:
                    items = ebay_search(token, cat, q)
                except requests.HTTPError as e:
                    print(f"[{cat['id']}] '{q}' failed: {e}", file=sys.stderr)
                    LOG.append(f"⚠️ {cat['id']}: search failed ({e.response.status_code if e.response is not None else 'error'})")
                    continue
            age = newest_age_min(items)
            if age is not None:
                ages.append(age)
            for it in items:
                if it["itemId"] in seen_ids:
                    continue
                seen_ids.add(it["itemId"])
                c = triage(cat, it)
                if c:
                    cands.append((cat, c))
        if ages:
            LOG.append(f"🕒 `{cat['id']}` newest listing: {min(ages)} min old")
        passed = sum(1 for x in cands if x[0] is cat)
        print(f"[{cat['id']}] {len(seen_ids)} listings, {passed} past the cheap filters")
        why = sorted(((n, r) for (cid, r), n in WHY.items() if cid == cat["id"]), reverse=True)
        LOG.append(f"`{cat['id']}` {len(seen_ids)} checked → {passed} passed"
                   + (" · dropped: " + ", ".join(f"{n} {r}" for n, r in why) if why else ""))

    cand_ids = {c["item_id"] for _, c in cands}
    old = already_seen(list(cand_ids) + [c["item_id"] for _, c in SOFT])
    # new listings, plus old ones whose price has dropped by £5+ since we last looked
    fresh = [(cat, c) for cat, c in cands if c["item_id"] not in old or c["all_in"] <= old[c["item_id"]] - 5
             or SCREENED_IN.get(c["item_id"], cat["id"]) != cat["id"]]
    if cands:
        LOG.append(f"{len(cands) - len(fresh)} already seen before, {len(fresh)} new to look at")
    screened_out = screen_soft(old, cand_ids, fresh)
    fresh.sort(key=lambda x: (x[1]["lane"] != "EXACT", x[1]["all_in"]))          # exact first, then cheapest

    results, lookups = [], 0
    for cat, c in fresh:
        details = None
        if lookups >= CONFIG["max_detail_lookups"]:
            LOG.append(f"⏭️ {short(c)}: lookup limit reached, next run")
            if c.get("rescued"):                         # remember the AI's yes so it isn't screened again
                results.append({**{k: v for k, v in c.items() if k not in DROP}, "verdict": "REJECT",
                                "status": "ai_ok", "variant": c.get("model")})
            continue                                    # never alert unchecked listings; they're picked up next run
        details = ebay_item(token, c["item_id"])
        lookups += 1
        gone_now = {"status": "gone_before_post", "outcome": "gone", "gone_at": datetime.now(timezone.utc).isoformat()}
        if details is None:
            if c["item_id"] in GONE:                     # A2: sold or ended before we even saw it, so never ping it
                LOG.append(f"🫥 {short(c)}: sold or ended before we saw it, not posted")
                results.append({**{k: v for k, v in c.items() if k not in DROP}, "verdict": "REJECT", **gone_now})
            else:
                LOG.append(f"❔ {short(c)}: listing details unavailable")
            continue
        if ended(details):
            row = score(cat, c, details)
            LOG.append(f"🫥 {short(row or c)}: sold or ended before we saw it, not posted"
                       + (f" (would have been a {row['verdict']})" if row and row["verdict"] != "REJECT" else ""))
            results.append({**(row or {**{k: v for k, v in c.items() if k not in DROP}, "verdict": "REJECT"}), **gone_now})
            continue
        row = score(cat, c, details)
        if row and c.get("rescued"):
            row["notes"] = f"🤖 AI found it (filters said: {c['rescued']})" + ("\n" + row["notes"] if row.get("notes") else "")
        if row and (for_deals(row) or for_offers(row)):
            row["ai_verdict"], row["ai_reason"], row["ai_model"] = ai_check(cat, row, details)
            if row["ai_verdict"] == "no":
                row["status"] = "ai_rejected"           # saved for review, never posted
            else:
                tag = {"yes": "🤖 AI OK", "unsure": "🤖 AI UNSURE"}.get(row["ai_verdict"], "🤖 AI NOT RUN (read the description yourself)")
                why = row["ai_reason"] if row["ai_verdict"] in ("yes", "unsure") else ""
                row["notes"] = (f"{tag}: {why}" if why else tag) + ("\n" + row["notes"] if row.get("notes") else "")
        if row is None:
            LOG.append(f"❌ {short(c)}: rejected after reading the listing ("
                       + (REJECT_WHY.get(c["item_id"]) or "wrong spec, damage or price") + ")")
        elif row.get("status") == "ai_rejected":
            LOG.append(f"🤖❌ {short(row)}: AI said no ({row.get('ai_reason') or 'no reason'})")
        elif for_deals(row):
            LOG.append(f"✅ {short(row)}: DEAL, posting · AI {row.get('ai_verdict')} ({row.get('ai_model') or '-'})")
        elif for_offers(row):
            LOG.append(f"🟡 {short(row)}: posting to #send-an-offer"
                       + (f" (offer £{row['suggested_offer']})" if row.get("suggested_offer") else "")
                       + f" · AI {row.get('ai_verdict')}")
        elif row["verdict"] == "DEAL":
            LOG.append(f"🔇 {short(row)}: deal but thin margin, saved not posted")
        else:
            LOG.append(f"🟡 {short(row)}: above target (offer zone), saved not posted")
        if row:
            results.append(row)
        elif details is not None:                       # remember rejects so we don't re-check them
            results.append({**{k: v for k, v in c.items() if k not in DROP},
                            "verdict": "REJECT", "status": "rejected"})
    results += screened_out
    by_id = {}
    for r in results:                                   # one row per listing (a listing can turn up in two categories)
        if r["item_id"] not in by_id or by_id[r["item_id"]]["verdict"] == "REJECT":
            by_id[r["item_id"]] = r
    results = list(by_id.values())
    print(f"{len(fresh)} new candidates, {lookups} detail lookups, "
          f"{sum(r['verdict'] != 'REJECT' for r in results)} alerts")

    if DRY_RUN:
        for r in results:
            print(json.dumps(r, indent=2, default=str))
        print("\n".join(LOG))
        return 0
    first_run = not has_history()
    saved = save_new(results)
    if first_run:
        print(f"First run: remembered {len(saved)} existing listings without alerting.")
        if full:
            save_last_full_scan()
        mark_run_finished()
        post_logs()
        report_usage()
        return 0
    failed = []
    alerts = [r for r in saved if for_deals(r)]
    offers = [r for r in saved if for_offers(r)]
    rank = {"HOT": 0, "SOLID": 1}
    alerts.sort(key=lambda r: (rank.get(r.get("rating"), 2), r["verdict"] != "DEAL", -float(r.get("profit") or 0)))
    # drip-feed: best first, then about one a minute, all done before the next 10-minute scan
    gap = min(CONFIG.get("alert_gap_sec", 55), CONFIG.get("alert_window_sec", 480) / max(1, len(alerts)))
    for i, row in enumerate(alerts):
        if i and gap > 0:                                 # alert_gap_sec 0 = post them all straight away
            time.sleep(gap)
        try:
            msg_id = discord_alert(row)
            mark_posted(row["item_id"], msg_id)
        except requests.RequestException as e:
            print(f"Discord failed for {row['item_id']}: {e}", file=sys.stderr)
            failed.append(row["item_id"])
    if offers and not os.getenv("DISCORD_OFFERS_WEBHOOK_URL"):
        LOG.append(f"🟡 {len(offers)} offer alert(s) not posted: #send-an-offer webhook not set up yet")
    elif offers:
        for row in sorted(offers, key=lambda r: -float(r.get("safe_profit") or 0)):
            try:
                msg_id = discord_alert(row, offers=True)
                mark_posted(row["item_id"], f"offers:{msg_id}" if msg_id else None)
            except requests.RequestException as e:
                print(f"Offer alert failed for {row['item_id']}: {e}", file=sys.stderr)
    sb_set_status(failed, "retry")                    # picked up again on the next run
    try:
        survival_checks(token)
    except requests.RequestException as e:
        print(f"Survival checks skipped: {e}", file=sys.stderr)
    try:
        if full:
            track_outcomes(token)
    except requests.RequestException as e:
        print(f"Outcome tracking skipped: {e}", file=sys.stderr)
    if failed:
        LOG.append(f"⚠️ {len(failed)} Discord alert(s) failed, will retry next run")
    if not fresh:
        LOG.append("Nothing new.")
    if full:
        save_last_full_scan()                         # B4: the next full scan is due full_scan_every_min after now
    post_ai_blocked([r for r in saved if r.get("status") == "ai_rejected"])
    mark_run_finished()
    post_logs()
    report_usage()
    return 0


# ---------- daily digest (10am) ----------

def sb_get(params: dict) -> list[dict]:
    r = requests.get(f"{os.environ['SUPABASE_URL']}/rest/v1/{TABLE}", headers=sb_headers(), params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def sb_set_status(item_ids: list[str], status: str) -> None:
    if not item_ids:
        return
    quoted = ",".join('"' + i + '"' for i in item_ids)
    requests.patch(f"{os.environ['SUPABASE_URL']}/rest/v1/{TABLE}", headers=sb_headers(),
                   params={"item_id": f"in.({quoted})"}, json={"status": status}, timeout=20).raise_for_status()


def still_live(token: str, row: dict) -> bool:
    d = ebay_item(token, row["item_id"])
    if not d:
        return False
    for a in d.get("estimatedAvailabilities") or []:
        if a.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK":
            return False
    price = float((d.get("price") or {}).get("value") or row["price"])
    return price <= float(row["price"]) + 1                  # gone up in price = no longer the deal we flagged


def daily_digest() -> int:
    """1-3 main picks from the last 24h + up to 5 extras still live from earlier days."""
    token = ebay_token()
    now = datetime.now(timezone.utc)
    rows = sb_get({"select": "*", "status": "in.(new,main,extra)", "verdict": "in.(DEAL,OFFER)",
                   "first_seen": f"gte.{(now - timedelta(days=5)).isoformat()}", "order": "profit.desc", "limit": 40})
    rows = [r for r in rows if not CAT_BY_ID.get(r.get("category"), {}).get("offers_only")]   # those live in #send-an-offer
    rank = {"HOT": 0, "SOLID": 1, "CHECK": 2}
    rows.sort(key=lambda r: (rank.get(r.get("rating"), 3), -(r.get("profit") or 0)))
    live, gone = [], []
    for r in rows[:15]:                                       # cap lookups
        (live if still_live(token, r) else gone).append(r)
    sb_set_status([r["item_id"] for r in gone], "gone")

    day_ago = (now - timedelta(days=1)).isoformat()
    fresh = [r for r in live if r["first_seen"] >= day_ago and r.get("rating") != "CHECK"]
    main = fresh[:3]
    extras = [r for r in live if r not in main][:5]
    sb_set_status([r["item_id"] for r in main], "main")
    sb_set_status([r["item_id"] for r in extras], "extra")

    def line(r):
        tag = {"HOT": "🔥", "SOLID": "🟢", "CHECK": "❓"}.get(r.get("rating"), "•")
        offer = f" · offer £{r['suggested_offer']}" if r.get("suggested_offer") else ""
        return (f"{tag} **[{r['variant']}]({r['url']})**: £{float(r['all_in']):.0f} all-in{offer} → "
                f"~£{float(r['profit']):.0f} profit · seller {r['seller_feedback']} fb")

    if not main and not extras:
        desc = "No solid deals in the last 24 hours. Nothing worth buying beats a bad buy. 👍"
    else:
        desc = ("**Main picks**\n" + ("\n".join(map(line, main)) or "_None today._")
                + ("\n\n**Still available from earlier**\n" + "\n".join(map(line, extras)) if extras else ""))
    post_discord({"embeds": [{
        "title": f"📋 Daily picks · {now:%a %d %b}", "description": desc[:4000], "color": 0x3498DB,
        "footer": {"text": f"{len(gone)} earlier alerts have sold or ended · checked {now:%H:%M} UTC"},
    }]}, daily=True)
    print(f"Digest: {len(main)} main, {len(extras)} extras, {len(gone)} gone")
    report_usage()
    return 0


def send_test() -> int:
    """One fake HOT alert through Supabase + Discord to prove the pipeline works (no eBay needed)."""
    row = {
        "item_id": f"TEST-{int(datetime.now(timezone.utc).timestamp())}", "category": "quest3",
        "variant": "Quest 3 512GB", "title": "TEST ALERT: example of a hot deal card (not a real listing)",
        "url": "https://www.ebay.co.uk", "image_url": None, "price": 255, "shipping": 0, "all_in": 255,
        "buy_target": 286, "expected_sell": 345, "profit": 81, "safe_profit": 49, "required_profit": 50,
        "verdict": "DEAL", "suggested_offer": None, "lane": "EXACT", "rating": "HOT",
        "seller": "test-seller", "seller_feedback": 250, "seller_pct": 100.0, "listed_at": None,
        "notes": "Example card. Real alerts list the seller, photo, postage and returns risks here.\nThis is only a test.", "status": "test",
    }
    saved = save_new([row])
    discord_alert(saved[0] if saved else row)
    print("Test alert saved to Supabase and sent to Discord.")
    return 0


def refresh_token() -> int:
    """'token' mode: renew the eBay token for the Supabase scanner (the eBay app keys stay in GitHub secrets)."""
    ebay_token(force=True)
    print("eBay token renewed and saved to bot_state.")
    return 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(send_test())
    if "--token" in sys.argv:
        sys.exit(refresh_token())
    sys.exit(daily_digest() if "--digest" in sys.argv else main())
