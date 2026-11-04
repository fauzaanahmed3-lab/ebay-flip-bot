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

r = run("quest3", item("Meta Quest 3 512GB with controllers", 240, fb=250), details())
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
check("phone: low-feedback seller rejected before any lookup", s.triage(phone, item("OnePlus 13 256GB", 300, fb=20)), None)
check("phone: FRP lock rejected", s.triage(phone, item("OnePlus 13 FRP locked", 200, fb=500)), None)

print(f"\n{sum(R)}/{len(R)} passed")

# ---- review fixes ----
R2 = []
def check2(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R2.append(ok)

check2("£100 rule applies above £500 too", s.required_profit(650), 100)
check2("'nothing missing' is not junk", s.junk_word(["missing"], "boxed, nothing missing"), None)
check2("'no scratches' is not junk", s.junk_word(["scratched", "scratch"], "no scratches at all"), None)
check2("'missing charger' is junk", s.junk_word(["missing"], "missing charger"), "missing")
check2("quest with bobovr headband no longer rejected", s.triage(CATS["quest3"], item("Meta Quest 3 128GB with bobovr headband", 200))["lane"], "EXACT")
check2("sony headband-only part rejected", s.triage(CATS["sony"], item("Sony WH-1000XM6 headband only", 60)), None)
r = run("samsung", item("Samsung Galaxy S26 Ultra 512GB unlocked", 690, fb=300), details())
check2("S26 Ultra 512 deal needs £100", (r["variant"], r["required_profit"], r["profit"] >= 100), ("Galaxy S26 Ultra 512GB", 100, True))
check2("Galaxy FE rejected", s.triage(CATS["samsung"], item("Samsung Galaxy S25 FE", 300, fb=300)), None)
r = run("oneplus", item("OnePlus 15 256GB", 500, fb=200), details())
check2("OnePlus 15 untagged = 256 + IMEI note", (r["variant"], "IMEI" in r["notes"]), ("OnePlus 15 256GB", True))
check2("OnePlus 13R rejected", s.triage(CATS["oneplus"], item("OnePlus 13R 256GB", 300, fb=200)), None)
check2("phone from 20-feedback seller rejected", run("oneplus", item("OnePlus 15 256GB", 450, fb=20), details()), None)
it = item("Sony WH-1000XM6 sealed", 160); it["condition"] = "New"
check2("unused item gets a note", "Listed as 'New'" in run("sony", it, details())["notes"], True)
print(f"review fixes: {sum(R2)}/{len(R2)} passed")

# ---- second review ----
R3 = []
def check3(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R3.append(ok)

check3("XM5 priced like an XM6 is dropped before a lookup", s.triage(CATS["sony"], item("Sony WH-1000XM5", 150)), None)
r = run("quest3", item("Meta Quest 3 512GB", 255, fb=250), details())
check3("not HOT if it only breaks even at an average price", (r["rating"], r["safe_profit"] < 50), ("SOLID", True))
check3("vague listing whose specifics name another model is rejected",
       run("sony", item("Sony noise cancelling headphones black", 90), details({"Model": "Sony WH-CH720N"})), None)
check3("vague Galaxy that is really an S25+ is rejected",
       run("samsung", item("Samsung Galaxy smartphone 256GB", 380, fb=200), details({"Model": "Samsung Galaxy S25+"})), None)
check3("Quest headset only (no controllers) rejected", s.triage(CATS["quest3"], item("Meta Quest 3 128GB headset only", 150)), None)
it = item("Sony WH-1000XM6", 150); it["shippingOptions"] = []
r = run("sony", it, details())
check3("no postage info = note + not HOT", ("Postage not stated" in r["notes"], r["rating"] != "HOT"), (True, True))
print(f"second review: {sum(R3)}/{len(R3)} passed")
