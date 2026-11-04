"""eBay Flip Bot scanner.

Every ~10 minutes (GitHub Actions) it runs the staged filter from the playbook:
  1. Smart search   - exact AND broad queries per category (eBay Browse API, UK, used, Buy It Now)
  2. Price gate     - drop anything above the category's best possible target
  3. Hard reject    - junk words + known wrong models (titles normalised first)
  4. Two lanes      - EXACT (model in title) or VAGUE (brand/family only = possible hidden gem)
  5. Confirm        - one detail lookup per survivor: item specifics, photos, returns, seller
  6. Alert          - rich Discord card with verdict, rating and plain-English notes

Secrets (GitHub Actions): EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, SUPABASE_URL,
SUPABASE_SERVICE_KEY, DISCORD_WEBHOOK_URL.  DRY_RUN=1 prints instead of saving/alerting.
"""
import base64
import json
import os
import re
import sys
import time
from datetime import timedelta
from datetime import datetime, timezone
from pathlib import Path

import requests

CONFIG = json.loads((Path(__file__).parent / "categories.json").read_text())
DRY_RUN = os.getenv("DRY_RUN") == "1"
EBAY_API = "https://api.ebay.com"
TABLE = "flip_listings"
PACK = CONFIG["packaging_cost"]


# ---------- text helpers ----------

def norm(text: str) -> str:
    """Lowercase and tidy so 'WH-1000 XM 6', 'wh1000xm6' and 'XM6' all contain 'xm6'."""
    t = (text or "").lower().replace("-", " ").replace("_", " ")
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


# ---------- profit rules ----------

def required_profit(all_in: float) -> int:
    """£50 minimum; anything bought for £400 or more needs £100."""
    return 100 if all_in >= 400 else 50


def buy_target(strong: float, post_out: float) -> float:
    net = strong - post_out - PACK
    if net - 50 < 400:
        return round(net - 50, 2)
    return round(net - 100, 2) if net - 100 >= 400 else 399.99


def model_for(cat: dict, text: str) -> dict | None:
    return next((m for m in cat["models"] if re.search(m["match"], text)), None)


# ---------- stages 2-4: cheap checks on the search result ----------

def triage(cat: dict, item: dict) -> dict | None:
    """Return a candidate dict (lane EXACT or VAGUE) or None if rejected."""
    title = item.get("title", "")
    t = norm(title)
    if item.get("itemLocation", {}).get("country", "GB") != "GB":
        return None

    price = float(item["price"]["value"])
    ship_opts = item.get("shippingOptions") or [{}]
    ship = float(ship_opts[0].get("shippingCost", {}).get("value", 0) or 0)
    all_in = round(price + ship, 2)

    best_target = max(buy_target(m["strong"], cat["post_out"]) for m in cat["models"])
    if all_in > best_target * (1 + CONFIG["offer_zone_pct"]):           # stage 2: price gate
        return None

    junk = CONFIG["junk"] + (CONFIG["phone_rules"]["junk"] if cat.get("phone") else [])
    if junk_word(junk, t) or any_match(cat["wrong"], t):                # stage 3: hard reject
        return None
    if cat.get("require") and not all(w in t for w in cat["require"]):
        return None

    hints = cat.get("category_hint")
    names = " ".join(c.get("categoryName", "") for c in item.get("categories", [])).lower()
    if hints and names and not any(h in names for h in hints):
        return None

    model = model_for(cat, t)                                            # stage 4: lanes
    if model:
        lane = "EXACT"
    elif re.search(cat["family"], t):
        lane = "VAGUE"
    else:
        return None

    seller = item.get("seller", {})
    return {
        "item_id": item["itemId"], "category": cat["id"], "title": title[:200],
        "url": item.get("itemWebUrl"), "image_url": (item.get("image") or {}).get("imageUrl"),
        "price": price, "shipping": ship, "all_in": all_in,
        "best_offer": "BEST_OFFER" in item.get("buyingOptions", []),
        "seller": seller.get("username"),
        "seller_feedback": int(seller.get("feedbackScore", 0) or 0),
        "seller_pct": float(seller.get("feedbackPercentage", 0) or 0),
        "listed_at": item.get("itemCreationDate"),
        "lane": lane, "model": model["name"] if model else None,
        "condition": item.get("condition"),
    }


# ---------- stage 5: confirm with item details, then score ----------

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

    lane = cand["lane"]
    model = next((m for m in cat["models"] if m["name"] == cand["model"]), None)
    if lane == "VAGUE":
        model = model_for(cat, aspects) if aspects else None
        if model:
            lane = "GEM"
            notes.append("Vague title, but the listing details confirm the model. Fewer buyers will spot this.")
        else:
            lane = "UNCONFIRMED"
            model = min(cat["models"], key=lambda m: m["strong"])          # price as the cheapest model
            notes.append("Model not confirmed. Check the photos before buying.")

    target = buy_target(model["strong"], cat["post_out"])
    all_in, ship = cand["all_in"], cand["shipping"]
    suggested_offer = None
    if lane == "UNCONFIRMED":
        if all_in > target - CONFIG["unconfirmed_extra_margin"]:
            return None
        verdict = "DEAL"
    elif all_in <= target:
        verdict = "DEAL"
    elif cand["best_offer"] and all_in <= target * (1 + CONFIG["offer_zone_pct"]):
        verdict = "OFFER"
        suggested_offer = int(target - ship)
    else:
        return None

    pay = suggested_offer + ship if suggested_offer else all_in
    profit = round(model["strong"] - cat["post_out"] - PACK - pay, 2)
    need = required_profit(pay)

    # ---- plain-English notes ----
    fb, pct = cand["seller_feedback"], cand["seller_pct"]
    trusted = fb >= 10 and pct >= 97
    if cat.get("phone") and (fb < CONFIG["phone_rules"]["min_feedback"] or pct < CONFIG["phone_rules"]["min_pct"]):
        return None                                           # phones: strict sellers only
    if fb < 10:
        notes.append(f"Seller has very little feedback ({fb}). Higher risk.")
    elif pct < 97:
        notes.append(f"Seller feedback is only {pct}%.")
    if photos is not None and photos < 3:
        notes.append(f"Only {photos} photo(s). Ask for more before buying.")
    if returns_ok is False:
        notes.append("No returns accepted.")
    if all_in < model["strong"] * 0.5:
        notes.append("Unusually cheap. Rule out fake, wrong model or hidden fault.")
    if (cand.get("condition") or "").lower().startswith(("new", "open", "brand new")):
        notes.append(f"Listed as '{cand['condition']}'. Unused stock at a used price is a strong sign, but confirm it's genuine.")
    if model.get("note"):
        notes.append(model["note"])
    if cat.get("phone"):
        notes.append("Phone: run a free IMEI check (blacklist and network lock) before paying.")
    if details is None:
        notes.append("Details not checked this run (lookup limit reached).")
    if verdict == "OFFER":
        notes.append(f"Listed above target. Offer £{suggested_offer}; walk away if they won't go below target.")

    # ---- rating ----
    fast_seller = (cat.get("weekly_sales") or 0) >= 10
    if lane in ("EXACT", "GEM") and trusted and profit >= need * CONFIG["hot_ratio"] and verdict == "DEAL":
        rating = "HOT"
        notes.insert(0, "Big margin, trusted seller" + (", fast-selling model." if fast_seller else "."))
    elif lane == "UNCONFIRMED" or not trusted:
        rating = "CHECK"
    else:
        rating = "SOLID"

    return {**{k: v for k, v in cand.items() if k not in ("best_offer", "model", "condition")},
            "variant": model["name"], "lane": lane, "verdict": verdict, "rating": rating,
            "buy_target": target, "expected_sell": model["strong"], "profit": profit,
            "required_profit": need, "suggested_offer": suggested_offer,
            "notes": "\n".join(notes) or None, "flags": None}


# ---------- eBay ----------

def ebay_token() -> str:
    creds = f"{os.environ['EBAY_CLIENT_ID']}:{os.environ['EBAY_CLIENT_SECRET']}"
    r = requests.post(
        f"{EBAY_API}/identity/v1/oauth2/token",
        headers={"Authorization": "Basic " + base64.b64encode(creds.encode()).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def ebay_headers(token):
    return {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"}


def ebay_search(token: str, cat: dict, query: str) -> list[dict]:
    filters = ",".join(["buyingOptions:{FIXED_PRICE}", "conditions:{NEW|USED}", "itemLocationCountry:GB",
                        f"price:[{cat['price_min']}..{cat['price_max']}]", "priceCurrency:GBP"])
    r = requests.get(f"{EBAY_API}/buy/browse/v1/item_summary/search", headers=ebay_headers(token),
                     params={"q": query, "filter": filters, "sort": "newlyListed", "limit": 50}, timeout=20)
    r.raise_for_status()
    return r.json().get("itemSummaries", [])


def ebay_item(token: str, item_id: str) -> dict | None:
    r = requests.get(f"{EBAY_API}/buy/browse/v1/item/{item_id}", headers=ebay_headers(token), timeout=20)
    return r.json() if r.ok else None


# ---------- Supabase ----------

def sb_headers():
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return {"apikey": key, "Content-Type": "application/json"}


def already_seen(ids: list[str]) -> dict[str, float]:
    """{item_id: all_in price when we last looked} for listings we've already handled."""
    if not ids or DRY_RUN:
        return {}
    quoted = ",".join('"' + i.replace('"', "") + '"' for i in ids)
    r = requests.get(f"{os.environ['SUPABASE_URL']}/rest/v1/{TABLE}",
                     headers=sb_headers(), params={"select": "item_id,all_in", "item_id": f"in.({quoted})"}, timeout=20)
    r.raise_for_status()
    return {row["item_id"]: float(row["all_in"] or 0) for row in r.json()}


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


# ---------- Discord ----------

STYLE = {
    "HOT":         ("🔥 HOT DEAL: buy fast", 0xE74C3C),
    "GEM":         ("💎 Hidden gem", 0x9B59B6),
    "DEAL":        ("🟢 Deal", 0x2ECC71),
    "OFFER":       ("🟡 Send an offer", 0xF1C40F),
    "UNCONFIRMED": ("❓ Unconfirmed: check photos", 0x95A5A6),
}


def post_discord(payload: dict, daily: bool = False) -> None:
    url = (os.getenv("DISCORD_DAILY_WEBHOOK_URL") if daily else None) or os.environ["DISCORD_WEBHOOK_URL"]
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code == 429:                                  # Discord rate limit: wait and retry once
        time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
        r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    time.sleep(0.6)


def discord_alert(row: dict) -> None:
    key = ("HOT" if row["rating"] == "HOT" else
           "UNCONFIRMED" if row["lane"] == "UNCONFIRMED" else
           "GEM" if row["lane"] == "GEM" else row["verdict"])
    label, colour = STYLE[key]
    price_line = f"£{row['all_in']:.2f} all-in (target £{row['buy_target']:.0f})"
    if row.get("suggested_offer"):
        price_line += f"\n**Offer £{row['suggested_offer']}** + postage"
    fields = [
        {"name": "💷 Price", "value": price_line, "inline": True},
        {"name": "💰 Est. profit", "value": f"~£{row['profit']:.0f} (need £{row['required_profit']})\nresell ~£{row['expected_sell']}", "inline": True},
        {"name": "👤 Seller", "value": f"{row['seller']}\n{row['seller_feedback']} fb · {row['seller_pct']}%", "inline": True},
    ]
    if row.get("notes"):
        fields.append({"name": "📝 Notes", "value": "\n".join(f"• {n}" for n in row["notes"].split("\n"))[:1000], "inline": False})
    payload = {
        "content": "@here 🔥 Hot one, act fast" if key == "HOT" else None,
        "allowed_mentions": {"parse": ["everyone"]} if key == "HOT" else {"parse": []},
        "embeds": [{
            "author": {"name": f"{label} · {row['variant']}"},
            "title": row["title"][:250], "url": row["url"], "color": colour, "fields": fields,
            "thumbnail": {"url": row["image_url"]} if row.get("image_url") else None,
            "footer": {"text": f"{row['category']} · {row['lane'].lower()} · rating {row['rating'].lower()}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    post_discord(payload)


# ---------- main ----------

def main() -> int:
    token = ebay_token()
    cats = [c for c in CONFIG["categories"] if c.get("enabled") and c["models"]]
    cands = []
    for cat in cats:
        seen_ids = set()
        for q in cat["queries"]:
            try:
                items = ebay_search(token, cat, q)
            except requests.HTTPError as e:
                print(f"[{cat['id']}] '{q}' failed: {e}", file=sys.stderr)
                continue
            for it in items:
                if it["itemId"] in seen_ids:
                    continue
                seen_ids.add(it["itemId"])
                c = triage(cat, it)
                if c:
                    cands.append((cat, c))
        print(f"[{cat['id']}] {len(seen_ids)} listings, {sum(1 for x in cands if x[0] is cat)} past the cheap filters")

    old = already_seen([c["item_id"] for _, c in cands])
    # new listings, plus old ones whose price has dropped by £5+ since we last looked
    fresh = [(cat, c) for cat, c in cands if c["item_id"] not in old or c["all_in"] <= old[c["item_id"]] - 5]
    fresh.sort(key=lambda x: (x[1]["lane"] != "EXACT", x[1]["all_in"]))          # exact first, then cheapest

    results, lookups = [], 0
    for cat, c in fresh:
        details = None
        if lookups < CONFIG["max_detail_lookups"]:
            details = ebay_item(token, c["item_id"])
            lookups += 1
        elif c["lane"] == "VAGUE":
            continue                                    # vague ones need confirming; try next run
        row = score(cat, c, details)
        if row:
            results.append(row)
        elif details is not None:                       # remember rejects so we don't re-check them
            results.append({**{k: v for k, v in c.items() if k not in ("best_offer", "model", "condition")},
                            "verdict": "REJECT", "status": "rejected"})
    print(f"{len(fresh)} new candidates, {lookups} detail lookups, "
          f"{sum(r['verdict'] != 'REJECT' for r in results)} alerts")

    if DRY_RUN:
        for r in results:
            print(json.dumps(r, indent=2, default=str))
        return 0
    first_run = not has_history()
    saved = save_new(results)
    if first_run:
        print(f"First run: remembered {len(saved)} existing listings without alerting.")
        return 0
    for row in saved:
        if row["verdict"] != "REJECT":
            discord_alert(row)
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
                f"~£{float(r['profit']):.0f} profit · {r['seller']} ({r['seller_feedback']} fb)")

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
    return 0


def send_test() -> int:
    """One fake HOT alert through Supabase + Discord to prove the pipeline works (no eBay needed)."""
    row = {
        "item_id": f"TEST-{int(datetime.now(timezone.utc).timestamp())}", "category": "quest3",
        "variant": "Quest 3 512GB", "title": "TEST ALERT: example of a hot deal card (not a real listing)",
        "url": "https://www.ebay.co.uk", "image_url": None, "price": 255, "shipping": 0, "all_in": 255,
        "buy_target": 286, "expected_sell": 345, "profit": 81, "required_profit": 50,
        "verdict": "DEAL", "suggested_offer": None, "lane": "EXACT", "rating": "HOT",
        "seller": "test-seller", "seller_feedback": 250, "seller_pct": 100.0, "listed_at": None,
        "notes": "Big margin, trusted seller, fast-selling model.\nThis is only a test.", "status": "test",
    }
    saved = save_new([row])
    discord_alert(saved[0] if saved else row)
    print("Test alert saved to Supabase and sent to Discord.")
    return 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(send_test())
    sys.exit(daily_digest() if "--digest" in sys.argv else main())
