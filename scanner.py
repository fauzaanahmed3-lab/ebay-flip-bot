"""eBay Flip Bot scanner.

Runs every ~10 minutes (GitHub Actions). For each category in categories.json it:
  1. searches eBay UK for newly listed, used, Buy It Now items in a price window
  2. throws out junk (blacklisted words, wrong models, non-UK)
  3. works out the profit using the playbook rules (eBay fees = £0 for private sellers)
  4. saves anything worth a look to Supabase (so nothing is alerted twice)
  5. sends new finds to Discord

Secrets come from environment variables (set them as GitHub Actions secrets):
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, SUPABASE_URL, SUPABASE_SERVICE_KEY, DISCORD_WEBHOOK_URL
Run with DRY_RUN=1 to print results instead of saving/alerting.
"""
import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

CONFIG = json.loads((Path(__file__).parent / "categories.json").read_text())
DRY_RUN = os.getenv("DRY_RUN") == "1"
EBAY_API = "https://api.ebay.com"
TABLE = "flip_listings"


# ---------- profit rules (from the playbook) ----------

def required_profit(all_in: float) -> int:
    """£100 when buying for £400–£500, otherwise £50."""
    return 100 if 400 <= all_in <= 500 else 50


def buy_target(strong: float, post_out: float) -> float:
    """Highest all-in price that still clears the profit rule."""
    net = strong - post_out - CONFIG["packaging_cost"]
    if net - 50 < 400:
        return round(net - 50, 2)
    # Buying in the £400–£500 band needs £100 profit.
    # If even that lands in the band, use it; otherwise stay just under £400 (where £50 applies).
    return round(net - 100, 2) if net - 100 >= 400 else 399.99


def pick_variant(category: dict, title: str) -> dict:
    for v in category["variants"]:
        if v["match"] and re.search(v["match"], title, re.I):
            return v
    return next(v for v in category["variants"] if not v["match"])


def is_junk(category: dict, title: str) -> str | None:
    t = title.lower()
    for word in CONFIG["global_blacklist"] + category["exclude"]:
        if word in t:
            return word
    return None


def evaluate(category: dict, item: dict) -> dict | None:
    """Turn one eBay search result into a scored candidate, or None if it's not worth a look."""
    title = item.get("title", "")
    if is_junk(category, title):
        return None
    if item.get("itemLocation", {}).get("country", "GB") != "GB":
        return None

    price = float(item["price"]["value"])
    ship_opts = item.get("shippingOptions") or [{}]
    ship = float(ship_opts[0].get("shippingCost", {}).get("value", 0) or 0)
    all_in = round(price + ship, 2)

    variant = pick_variant(category, title)
    strong = variant["strong"]
    post_out = category["post_out"]
    target = buy_target(strong, post_out)
    best_offer = "BEST_OFFER" in item.get("buyingOptions", [])

    if all_in <= target:
        verdict = "DEAL"
        suggested_offer = None
    elif best_offer and all_in <= target * (1 + CONFIG["offer_zone_pct"]):
        verdict = "OFFER"
        suggested_offer = int(target - ship)  # offer is on the item price, delivery on top
    else:
        return None

    pay = suggested_offer + ship if suggested_offer else all_in
    profit = round(strong - post_out - CONFIG["packaging_cost"] - pay, 2)

    seller = item.get("seller", {})
    fb_score = int(seller.get("feedbackScore", 0) or 0)
    fb_pct = float(seller.get("feedbackPercentage", 0) or 0)
    flags = []
    if fb_score < 3:
        flags.append("new/low-feedback seller")
    if fb_score >= 100 and fb_pct < 97:
        flags.append(f"feedback {fb_pct}%")
    if all_in < strong * 0.5:
        flags.append("suspiciously cheap")
    if variant.get("note"):
        flags.append(variant["note"])

    return {
        "item_id": item["itemId"],
        "category": category["id"],
        "variant": variant["name"],
        "title": title[:200],
        "url": item.get("itemWebUrl"),
        "image_url": (item.get("image") or {}).get("imageUrl"),
        "price": price,
        "shipping": ship,
        "all_in": all_in,
        "buy_target": target,
        "expected_sell": strong,
        "profit": profit,
        "required_profit": required_profit(pay),
        "verdict": verdict,
        "suggested_offer": suggested_offer,
        "seller": seller.get("username"),
        "seller_feedback": fb_score,
        "seller_pct": fb_pct,
        "flags": ", ".join(flags) or None,
        "listed_at": item.get("itemCreationDate"),
    }


# ---------- eBay ----------

def ebay_token() -> str:
    creds = f"{os.environ['EBAY_CLIENT_ID']}:{os.environ['EBAY_CLIENT_SECRET']}"
    r = requests.post(
        f"{EBAY_API}/identity/v1/oauth2/token",
        headers={
            "Authorization": "Basic " + base64.b64encode(creds.encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def ebay_search(token: str, category: dict) -> list[dict]:
    filters = ",".join([
        "buyingOptions:{FIXED_PRICE}",
        "conditions:{USED}",
        "itemLocationCountry:GB",
        f"price:[{category['price_min']}..{category['price_max']}]",
        "priceCurrency:GBP",
    ])
    r = requests.get(
        f"{EBAY_API}/buy/browse/v1/item_summary/search",
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"},
        params={"q": category["query"], "filter": filters, "sort": "newlyListed", "limit": 50},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("itemSummaries", [])


# ---------- Supabase + Discord ----------

def save_new(rows: list[dict]) -> list[dict]:
    """Insert rows; returns only the ones that weren't already saved (no duplicate alerts)."""
    if not rows:
        return []
    r = requests.post(
        f"{os.environ['SUPABASE_URL']}/rest/v1/{TABLE}?on_conflict=item_id",
        headers={
            "apikey": os.environ["SUPABASE_SERVICE_KEY"],
            "Content-Type": "application/json",
            "Prefer": "resolution=ignore-duplicates,return=representation",
        },
        json=rows,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def discord_alert(row: dict) -> None:
    labels = {c["id"]: c["label"] for c in CONFIG["categories"]}
    if row["verdict"] == "DEAL":
        headline = f"🟢 DEAL — £{row['all_in']:.2f} all-in"
    else:
        headline = f"🟡 OFFER £{row['suggested_offer']} (listed £{row['all_in']:.2f} all-in)"
    lines = [
        f"**{labels.get(row['category'], row['category'])} {row['variant']}**",
        headline,
        f"Sell ~£{row['expected_sell']} → profit ~£{row['profit']:.0f} (need £{row['required_profit']})",
        f"Seller: {row['seller']} ({row['seller_feedback']} fb, {row['seller_pct']}%)",
    ]
    if row.get("flags"):
        lines.append(f"⚠️ {row['flags']}")
    payload = {"embeds": [{
        "title": row["title"][:250],
        "url": row["url"],
        "description": "\n".join(lines),
        "color": 0x2ECC71 if row["verdict"] == "DEAL" else 0xF1C40F,
        "thumbnail": {"url": row["image_url"]} if row.get("image_url") else None,
    }]}
    requests.post(os.environ["DISCORD_WEBHOOK_URL"], json=payload, timeout=20).raise_for_status()


# ---------- main ----------

def main() -> int:
    token = ebay_token()
    found = []
    for cat in CONFIG["categories"]:
        try:
            items = ebay_search(token, cat)
        except requests.HTTPError as e:
            print(f"[{cat['id']}] search failed: {e}", file=sys.stderr)
            continue
        hits = [row for row in (evaluate(cat, it) for it in items) if row]
        print(f"[{cat['id']}] {len(items)} listings checked, {len(hits)} worth a look")
        found.extend(hits)

    if DRY_RUN:
        for row in found:
            print(json.dumps(row, indent=2))
        return 0

    new_rows = save_new(found)
    for row in new_rows:
        discord_alert(row)
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC — {len(new_rows)} new alerts sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
