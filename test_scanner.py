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
check("target never above £500", s.buy_target(985, 6), 500)

r = run("quest3", item("Meta Quest 3 512GB with controllers", 240, fb=250), details())
check("quest 512 exact + big margin = HOT", (r["lane"], r["verdict"], r["rating"], r["variant"]), ("EXACT", "DEAL", "HOT", "Quest 3 · 512GB"))
check("quest 3S rejected", run("quest3", item("Meta Quest 3S 128GB", 150), details()), None)
check("quest 3 standalone not mistaken for 3S", s.triage(CATS["quest3"], item("Meta Quest 3 Standalone VR Headset", 150))["lane"], "EXACT")

r = run("quest3", item("Meta VR headset with controllers white", 150), details({"Model": "Meta Quest 3", "Storage Capacity": "128 GB"}))
check("vague quest confirmed by specifics = GEM", (r["lane"], r["verdict"], r["variant"]), ("GEM", "DEAL", "Quest 3 · 128GB or storage not stated"))
r = run("quest3", item("Meta VR headset with controllers white", 150), details({}))
check("vague + no specifics + very cheap = UNCONFIRMED", (r["lane"], r["rating"]), ("UNCONFIRMED", "CHECK"))
check("vague + no specifics + not cheap enough = dropped", run("quest3", item("Meta VR headset with controllers", 200), details({})), None)
check("vague confirmed as 3S in specifics = rejected", run("quest3", item("Meta VR headset", 150), details({"Model": "Meta Quest 3S"})), None)

check("Bose 'QC Ultra' that is really earbuds (specifics) = rejected",
      run("bose", item("Bose QuietComfort Ultra Noise Cancelling Bluetooth Wireless 1st Gen- Moonstone", 115.10, 4.52, fb=1023, pct=99.3),
          details({"Type": "Earbud (In Ear)", "Model": "Bose QuietComfort Ultra Earbuds"})), None)
r = run("bose", item("Bose QuietComfort Ultra Headphones (2nd Gen)", 188), details({"Type": "Over-Ear"}))
check("bose gen 2 headphones deal", (r["variant"], r["verdict"]), ("Bose QC Ultra Gen 2", "DEAL"))

check("sony XM4 rejected", run("sony", item("Sony WH-1000XM4 black", 70), details()), None)
check("sony earbuds rejected", run("sony", item("Sony WF-1000XM5 earbuds", 60), details()), None)
r = run("sony", item("Sony WH-1000XM6 black", 175, offer=False), details())
check("sony xm6 deal under £183", (r["variant"], r["verdict"]), ("Sony WH-1000XM6", "DEAL"))
r = run("sony", item("Sony WH-1000XM6", 195, offer=True), details())
check("sony xm6 offer zone", (r["verdict"], r["suggested_offer"]), ("OFFER", 174))
check("sony xm5 too dear for xm5 target", run("sony", item("Sony WH-1000XM5", 95), details()), None)

r = run("steamdeck", item("Valve Steam Deck 512GB handheld", 380), details({"Screen Type": "OLED"}))
check("vague steam deck confirmed OLED = GEM", (r["lane"], r["variant"]), ("GEM", "Steam Deck OLED 512GB"))
check("steam deck LCD rejected", run("steamdeck", item("Steam Deck LCD 512GB", 300), details()), None)
r = run("steamdeck", item("Steam Deck OLED 512GB", 400, 5), details())
check("steam deck £420 all-in needs £100", (r["required_profit"], r["profit"] >= 100), (100, True))

check("wrong eBay category rejected", s.triage(CATS["sony"], item("Sony WH-1000XM6", 150, cat_name="Mobile Phone Cases")), None)
check("foreign seller rejected", s.triage(CATS["sony"], item("Sony WH-1000XM6", 150, country="CN")), None)
check("brand-new seller (0 feedback) rejected", s.triage(CATS["sony"], item("Sony WH-1000XM6", 150, fb=0)), None)
check("seller under 97% rejected", s.triage(CATS["sony"], item("Sony WH-1000XM6", 150, fb=400, pct=95.0)), None)
r = run("sony", item("Sony WH-1000XM6", 150, fb=3), details(photos=1, returns=False))
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
r = run("samsung", item("Samsung Galaxy S25 Ultra 512GB unlocked", 440, fb=300), details())
check2("S25 Ultra 512 deal needs £100", (r["variant"], r["required_profit"], r["profit"] >= 100), ("Galaxy S25 Ultra 512GB", 100, True))
check2("Galaxy FE rejected", s.triage(CATS["samsung"], item("Samsung Galaxy S25 FE", 300, fb=300)), None)
r = run("oneplus", item("OnePlus 15 256GB", 470, fb=200), details())
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

# ---- third review: real false positives from the first live run (5 Nov) ----
R4 = []
def check4(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R4.append(ok)

for cat, title, price in [
    ("momentum4", "Dekoni Choice Series Leather Headphone Pads for Sennheiser Momentum 4", 50),
    ("quest3", "10000mAh Battery Head Strap Replacement for Meta Quest 3/3S VR 18W Fast Charging", 181.5),
    ("sony", "Sony wh-1000xm5 - working but snapped", 73.54),
    ("sony", "Sony WH-1000XM5 Wireless Noise Cancelling Headphones Blue - see notes", 78),
    ("sony", "SONY WH-1000XM4C Wireless Bluetooth Noise-Cancelling Headphones", 150),
    ("supersonic", "x4 Dyson Supersonic Hair Dryer Accessories - Free & Fast Dispatch", 39.99),
    ("supersonic", "Dyson Supersonic HD11 Professional Hair Dryer Nickel For hair salons Etc - READ", 48.64),
    ("supersonic", "Dyson Supersonic Hair Dryer - Nickel/Fuchsia HD03", 49.98),
    ("bose", "Bose QuietComfort 45 SE Headphones - Black", 102.88),
    ("bose", "Bose QuietComfort Wireless Over-Ear Headphones - White Smoke", 119.02),
]:
    check4(f"rejected for free: {title[:45]}", s.triage(CATS[cat], item(title, price)), None)
check4("Chinese-model phone rejected", s.triage(CATS["oneplus"], item("OnePlus 13 512GB Very Good Chinese Model", 390, fb=205)), None)
check4("pink line screen rejected", s.triage(CATS["samsung"], item("Samsung Galaxy S26 Ultra 256GB Unlocked PINK LINE", 586, fb=1559)), None)
check4("Bose earbuds caught by details",
       run("bose", item("Bose QuietComfort Ultra Noise Cancelling Bluetooth Wireless 1st Gen- Moonstone", 119.62, fb=1023),
           details({"Type": "Earbud (In Ear)"})), None)
check4("'with accessories' is still allowed", s.triage(CATS["supersonic"], item("Dyson Supersonic Hair Dryer Anthracite/Fuchsia With Accessories", 60)) is not None, True)
check4("over-£500 phone ignored (spending cap)",
       s.triage(CATS["oneplus"], item("OnePlus 15 CPH2747 512GB 16GB RAM OxygenOS White", 625.7, offer=True, fb=2170)), None)
r = run("oneplus", item("OnePlus 15 512GB OxygenOS White", 530, offer=True, fb=2170), details())
check4("offer never suggests paying over £500", (r["verdict"], r["suggested_offer"] <= 500), ("OFFER", True))
r = run("supersonic", item("Dyson Supersonic Hair Dryer Anthracite/Fuchsia With Accessories", 62, fb=929), details())
check4("thin margin (~£8 at average) is CHECK, not SOLID", r["rating"], "CHECK")
print(f"third review: {sum(R4)}/{len(R4)} passed")

# ---- new categories (5 Nov market research) ----
R5 = []
def check5(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R5.append(ok)

r = run("quest3s", item("Meta Quest 3S 128GB with controllers", 135, fb=200), details())
check5("Quest 3S deal found (was rejected before)", (r["variant"], r["verdict"]), ("Quest 3S 128GB", "DEAL"))
check5("Quest 3 is not mistaken for a 3S", s.triage(CATS["quest3s"], item("Meta Quest 3 128GB", 140)), None)
r = run("switcholed", item("Nintendo Switch OLED White console with dock and joycons", 115, fb=200), details())
check5("Switch OLED deal found", (r["variant"], r["verdict"]), ("Switch OLED", "DEAL"))
check5("Switch OLED tablet only rejected", s.triage(CATS["switcholed"], item("Nintendo Switch OLED console only", 90)), None)
check5("Switch Lite rejected", s.triage(CATS["switcholed"], item("Nintendo Switch Lite OLED screen", 60)), None)
r = run("switch2", item("Nintendo Switch 2 Console Boxed", 270, fb=200), details())
check5("Switch 2 deal found", r["verdict"], "DEAL")
check5("case for Switch 2 rejected", s.triage(CATS["switch2"], item("Hard case for Nintendo Switch 2", 20)), None)
r = run("osmo3", item("DJI Osmo Pocket 3 Creator Combo", 250, fb=200), details())
check5("Osmo Pocket 3 Creator Combo priced as combo", r["variant"], "Osmo Pocket 3 Creator Combo")
check5("filters for Osmo rejected", s.triage(CATS["osmo3"], item("ND filters for DJI Osmo Pocket 3", 25)), None)
r = run("quest3", item("Meta Quest 3 512GB", 315, offer=True, fb=200), details())
check5("20% offer zone catches a listing ~15% over target", r["verdict"], "OFFER")
print(f"new categories: {sum(R5)}/{len(R5)} passed")

# ---- research round 2 (5 Nov): consoles, buyer fee, seller rules ----
R6 = []
def check6(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R6.append(ok)

check6("buyer fee on £300 = £12.70", s.buyer_fee(300), 12.7)
check6("buyer fee on £100 = £4.70", s.buyer_fee(100), 4.7)
c = s.triage(CATS["ps5"], item("Sony PS5 Slim Disc Edition 1TB console boxed", 330, fb=40))
check6("PS5 slim disc found, all-in includes buyer fee", (c["model"], c["all_in"]), ("PS5 Slim Disc", round(330 + s.buyer_fee(330), 2)))
check6("PS5 slim digital tagged correctly", s.triage(CATS["ps5"], item("PlayStation 5 Slim Digital Edition", 290, fb=40))["model"], "PS5 Slim Digital")
check6("PS5 Pro rejected", s.triage(CATS["ps5"], item("PS5 Pro 2TB console", 300, fb=40)), None)
check6("PS5 controller only rejected", s.triage(CATS["ps5"], item("PS5 DualSense controller only", 40)), None)
check6("PS5 original disc priced as original", s.triage(CATS["ps5"], item("Sony PlayStation 5 Disc Console CFI-1216A", 320, fb=40))["model"], "PS5 Disc (original)")
c = s.triage(CATS["xboxsx"], item("Xbox Series X 1TB console with one controller", 340, fb=20))
check6("Xbox Series X with one controller is fine", c and c["model"], "Series X (disc)")
check6("Xbox Series S rejected", s.triage(CATS["xboxsx"], item("Xbox Series S 512GB console", 150)), None)
check6("Xbox Series X digital tagged", s.triage(CATS["xboxsx"], item("Xbox Series X Digital Edition 1TB white", 320, fb=20))["model"], "Series X Digital")
it = item("Xbox Series X 1TB console", 380, fb=20)
cand = s.triage(CATS["xboxsx"], it); det = details(); det["seller"] = {"sellerAccountType": "BUSINESS"}
check6("business seller: buyer fee removed", s.score(CATS["xboxsx"], cand, det)["all_in"], 380.0)
r = run("xboxsx", item("Xbox Series X 1TB console", 330, fb=4), details())
check6("4-feedback seller allowed but CHECK", r["rating"], "CHECK")
check6("phones and Switch OLED switched off", [CATS[k]["enabled"] for k in ("oneplus", "samsung", "switcholed")], [False, False, False])
r = run("quest3", item("Meta Quest 3 VR headset with controllers", 205, fb=200), details())
check6("Quest 3 without storage priced safely + tagged", r["variant"], "Quest 3 · 128GB or storage not stated")
print(f"research round 2: {sum(R6)}/{len(R6)} passed")
