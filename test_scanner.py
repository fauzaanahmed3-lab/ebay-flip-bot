"""Offline tests for the filter pipeline (no eBay/Supabase/Discord needed). Run: python test_scanner.py"""
import scanner as s

CATS = {c["id"]: c for c in s.CONFIG["categories"]}

def item(title, price, ship=0, offer=False, fb=50, pct=100.0, country="GB", cat_name=None):
    it = {"itemId": "v1|" + title[:12], "title": title, "price": {"value": str(price)},
          "shippingOptions": [{"shippingCost": {"value": str(ship)}}],
          "buyingOptions": ["FIXED_PRICE"] + (["BEST_OFFER"] if offer else []),
          "seller": {"username": "x", "feedbackScore": fb, "feedbackPercentage": str(pct)},
          "itemLocation": {"country": country}, "itemWebUrl": "https://ebay.co.uk/itm/1"}
    if cat_name:
        it["categories"] = [{"categoryName": cat_name}]
    return it

def details(aspects=None, photos=6, returns=True, cond=""):
    return {"localizedAspects": [{"name": k, "value": v} for k, v in (aspects or {}).items()],
            "additionalImages": [{}] * (photos - 1), "returnTerms": {"returnsAccepted": returns},
            "conditionDescription": cond}

def run(cat, it, det=None):
    c = s.triage(CATS[cat], it)
    return c and s.score(CATS[cat], c, det)

R = []
def check(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R.append(ok)

check("normalise 'WH-1000 XM 6'", "xm6" in s.norm("Sony WH-1000 XM 6"), True)
check("steam deck target uses £100 rule", s.buy_target(530, 9), 420)

r = run("quest3", item("Meta Quest 3 512GB with controllers", 255, fb=250), details())
check("quest 512 exact + big margin = HOT", (r["lane"], r["verdict"], r["rating"], r["variant"]), ("EXACT", "DEAL", "HOT", "Quest 3 512GB"))
check("quest 3S rejected", run("quest3", item("Meta Quest 3S 128GB", 150), details()), None)
check("quest 3 standalone not mistaken for 3S", s.triage(CATS["quest3"], item("Meta Quest 3 Standalone VR Headset", 150))["lane"], "EXACT")

r = run("quest3", item("Meta VR headset with controllers white", 150), details({"Model": "Meta Quest 3", "Storage Capacity": "128 GB"}))
check("vague quest confirmed by specifics = GEM", (r["lane"], r["verdict"], r["variant"]), ("GEM", "DEAL", "Quest 3 128GB"))
r = run("quest3", item("Meta VR headset with controllers white", 150), details({}))
check("vague + no specifics + very cheap = UNCONFIRMED", (r["lane"], r["rating"]), ("UNCONFIRMED", "CHECK"))
check("vague + no specifics + not cheap enough = dropped", run("quest3", item("Meta VR headset with controllers", 200), details({})), None)
check("vague confirmed as 3S in specifics = rejected", run("quest3", item("Meta VR headset", 150), details({"Model": "Meta Quest 3S"})), None)

check("Bose 'QC Ultra' that is really earbuds (specifics) = rejected",
      run("bose", item("Bose QuietComfort Ultra Noise Cancelling Bluetooth Wireless 1st Gen- Moonstone", 115.10, 4.52, fb=1023, pct=99.3),
          details({"Type": "Earbud (In Ear)", "Model": "Bose QuietComfort Ultra Earbuds"})), None)
r = run("bose", item("Bose QuietComfort Ultra Headphones (2nd Gen)", 195), details({"Type": "Over-Ear"}))
check("bose gen 2 headphones deal", (r["variant"], r["verdict"]), ("Bose QC Ultra Gen 2", "DEAL"))

check("sony XM4 rejected", run("sony", item("Sony WH-1000XM4 black", 70), details()), None)
check("sony earbuds rejected", run("sony", item("Sony WF-1000XM5 earbuds", 60), details()), None)
r = run("sony", item("Sony WH-1000XM6 black", 175, offer=False), details())
check("sony xm6 deal under £183", (r["variant"], r["verdict"]), ("Sony WH-1000XM6", "DEAL"))
r = run("sony", item("Sony WH-1000XM6", 195, offer=True), details())
check("sony xm6 offer zone", (r["verdict"], r["suggested_offer"]), ("OFFER", 183))
check("sony xm5 too dear for xm5 target", run("sony", item("Sony WH-1000XM5", 95), details()), None)

r = run("steamdeck", item("Valve Steam Deck 512GB handheld", 380), details({"Screen Type": "OLED"}))
check("vague steam deck confirmed OLED = GEM", (r["lane"], r["variant"]), ("GEM", "Steam Deck OLED 512GB"))
check("steam deck LCD rejected", run("steamdeck", item("Steam Deck LCD 512GB", 300), details()), None)
r = run("steamdeck", item("Steam Deck OLED 512GB", 415, 5), details())
check("steam deck £420 all-in needs £100", (r["required_profit"], r["profit"] >= 100), (100, True))

check("wrong eBay category rejected", s.triage(CATS["sony"], item("Sony WH-1000XM6", 150, cat_name="Mobile Phone Cases")), None)
check("foreign seller rejected", s.triage(CATS["sony"], item("Sony WH-1000XM6", 150, country="CN")), None)
r = run("sony", item("Sony WH-1000XM6", 150, fb=0), details(photos=1, returns=False))
check("risky listing gets notes + CHECK rating",
      (r["rating"], "little feedback" in r["notes"], "photo" in r["notes"], "No returns" in r["notes"]), ("CHECK", True, True, True))
check("damage in condition notes rejected", run("sony", item("Sony WH-1000XM6", 150), details(cond="small crack on hinge, works fine")), None)

phone = {"id": "p", "phone": True, "post_out": 5, "family": "oneplus", "wrong": [], "wrong_specs": [], "price_min": 1,
         "price_max": 999, "queries": [], "category_hint": None, "models": [{"name": "OnePlus 13", "match": "oneplus 13", "strong": 450}]}
c = s.triage(phone, item("OnePlus 13 256GB", 300, fb=20))
check("phone: low-feedback seller rejected", s.score(phone, c, details()), None)
check("phone: FRP lock rejected", s.triage(phone, item("OnePlus 13 FRP locked", 200, fb=500)), None)

print(f"\n{sum(R)}/{len(R)} passed")
