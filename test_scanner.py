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
check("steam deck target uses £100 rule", s.buy_target(530, 9), 402.7)   # B5: resale is net of eBay buyer fee (was 420)
check("target never above £500", s.buy_target(985, 6), 500)

r = run("quest3", item("Meta Quest 3 512GB with controllers", 230, fb=250), details())   # B5: was 240 (net resale is lower now)
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
r = run("bose", item("Bose QuietComfort Ultra Headphones (2nd Gen)", 175), details({"Type": "Over-Ear"}))
check("bose gen 2 headphones deal", (r["variant"], r["verdict"]), ("Bose QC Ultra Gen 2", "DEAL"))

check("sony XM4 rejected", run("sony", item("Sony WH-1000XM4 black", 70), details()), None)
check("sony earbuds rejected", run("sony", item("Sony WF-1000XM5 earbuds", 60), details()), None)
r = run("sony", item("Sony WH-1000XM6 black", 160, offer=False), details())
check("sony xm6 deal under £183", (r["variant"], r["verdict"]), ("Sony WH-1000XM6", "DEAL"))
r = run("sony", item("Sony WH-1000XM6", 185, offer=True), details())
check("sony xm6 offer zone", (r["verdict"], r["suggested_offer"]), ("OFFER", 165))   # B5: lower target after buyer fee
check("sony xm5 too dear for xm5 target", run("sony", item("Sony WH-1000XM5", 95), details()), None)

r = run("steamdeck", item("Valve Steam Deck 512GB handheld", 380), details({"Screen Type": "OLED"}))
check("vague steam deck confirmed OLED = GEM", (r["lane"], r["variant"]), ("GEM", "Steam Deck OLED 512GB"))
check("steam deck LCD rejected", run("steamdeck", item("Steam Deck LCD 512GB", 300), details()), None)
r = run("steamdeck", item("Steam Deck OLED 512GB", 382, 5), details())
check("steam deck £401 all-in needs £100", (r["required_profit"], r["profit"] >= 100), (100, True))

check("wrong eBay category rejected", s.triage(CATS["sony"], item("Sony WH-1000XM6", 150, cat_name="Mobile Phone Cases")), None)
check("foreign seller rejected", s.triage(CATS["sony"], item("Sony WH-1000XM6", 150, country="CN")), None)
check("brand-new seller (0 feedback) rejected", s.triage(CATS["sony"], item("Sony WH-1000XM6", 150, fb=0)), None)
check("seller under 97% rejected", s.triage(CATS["sony"], item("Sony WH-1000XM6", 150, fb=400, pct=95.0)), None)
r = run("sony", item("Sony WH-1000XM6", 150, fb=3), details(photos=1, returns=False))
check("risky listing gets notes + CHECK rating",
      (r["rating"], "LOW FB (3)" in r["notes"], "1 PIC" in r["notes"], "No returns" in r["notes"]), ("CHECK", True, True, False))   # 25 Nov: "no returns" dropped
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
check2("unused item gets a tag", "NEW/OPEN BOX" in run("sony", it, details())["notes"], True)
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
check3("no postage info = COLLECTION ONLY + not HOT", (r["delivery"], "COLLECTION ONLY" in s.delivery_badge(r), r["rating"] != "HOT"), ("collection", True, True))
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
check4("'with accessories' is still allowed", s.triage(CATS["supersonic"], item("Dyson Supersonic Hair Dryer Anthracite/Fuchsia With Accessories", 55)) is not None, True)
check4("over-£500 phone ignored (spending cap)",
       s.triage(CATS["oneplus"], item("OnePlus 15 CPH2747 512GB 16GB RAM OxygenOS White", 625.7, offer=True, fb=2170)), None)
r = run("oneplus", item("OnePlus 15 512GB OxygenOS White", 530, offer=True, fb=2170), details())
check4("offer never suggests paying over £500", (r["verdict"], r["suggested_offer"] <= 500), ("OFFER", True))
r = run("supersonic", item("Dyson Supersonic Hair Dryer Anthracite/Fuchsia With Accessories", 55, fb=929), details())
check4("thin margin (~£8 at average) is CHECK, not SOLID", r["rating"], "CHECK")
print(f"third review: {sum(R4)}/{len(R4)} passed")

# ---- new categories (5 Nov market research) ----
R5 = []
def check5(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R5.append(ok)

r = run("quest3s", item("Meta Quest 3S 128GB with controllers", 125, fb=200), details())
check5("Quest 3S deal found (was rejected before)", (r["variant"], r["verdict"]), ("Quest 3S 128GB", "DEAL"))
check5("Quest 3 is not mistaken for a 3S", s.triage(CATS["quest3s"], item("Meta Quest 3 128GB", 140)), None)
r = run("switcholed", item("Nintendo Switch OLED White console with dock and joycons", 105, fb=200), details())
check5("Switch OLED deal found", (r["variant"], r["verdict"]), ("Switch OLED", "DEAL"))
check5("Switch OLED tablet only rejected", s.triage(CATS["switcholed"], item("Nintendo Switch OLED console only", 90)), None)
check5("Switch Lite rejected", s.triage(CATS["switcholed"], item("Nintendo Switch Lite OLED screen", 60)), None)
r = run("switch2", item("Nintendo Switch 2 Console Boxed", 255, fb=200), details())
check5("Switch 2 deal found", r["verdict"], "DEAL")
check5("case for Switch 2 rejected", s.triage(CATS["switch2"], item("Hard case for Nintendo Switch 2", 20)), None)
r = run("osmo3", item("DJI Osmo Pocket 3 Creator Combo", 235, fb=200), details())
check5("Osmo Pocket 3 Creator Combo priced as combo", r["variant"], "Osmo Pocket 3 Creator Combo")
check5("filters for Osmo rejected", s.triage(CATS["osmo3"], item("ND filters for DJI Osmo Pocket 3", 25)), None)
r = run("quest3", item("Meta Quest 3 512GB", 282, offer=True, fb=200), details())
check5("12% offer zone catches a listing ~8% over target", r["verdict"], "OFFER")
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
check6("Xbox Series X digital tagged", s.triage(CATS["xboxsx"], item("Xbox Series X Digital Edition 1TB white", 290, fb=20))["model"], "Series X Digital")
it = item("Xbox Series X 1TB console", 360, fb=20)
cand = s.triage(CATS["xboxsx"], it); det = details(); det["seller"] = {"sellerAccountType": "BUSINESS"}
check6("business seller: buyer fee removed", s.score(CATS["xboxsx"], cand, det)["all_in"], 360.0)
r = run("xboxsx", item("Xbox Series X 1TB console", 330, fb=4), details())
check6("4-feedback seller allowed but CHECK", r["rating"], "CHECK")
check6("phones and Switch OLED switched off", [CATS[k]["enabled"] for k in ("oneplus", "samsung", "switcholed")], [False, False, False])
r = run("quest3", item("Meta Quest 3 VR headset with controllers", 205, fb=200), details())
check6("Quest 3 without storage priced safely + tagged", r["variant"], "Quest 3 · 128GB or storage not stated")
print(f"research round 2: {sum(R6)}/{len(R6)} passed")

# ---- round 3 (5 Nov): Series S, drip feed ----
R7 = []
def check7(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R7.append(ok)
r = run("xboxss", item("Xbox Series S 512GB white with controller", 150, 5, fb=30), details())
check7("Series S deal found", (r["variant"], r["verdict"]), ("Series S · 512GB or not stated", "DEAL"))
check7("Series X not mistaken for Series S", s.triage(CATS["xboxss"], item("Xbox Series X 1TB", 150, fb=30)), None)
check7("Xbox One S rejected", s.triage(CATS["xboxss"], item("Xbox One S 1TB console Series S/X controller", 140, fb=30)), None)
check7("expansion card rejected", s.triage(CATS["xboxss"], item("Seagate expansion card for Xbox Series S 1TB", 120, fb=30)), None)
check7("offer zone is 12%", s.CONFIG["offer_zone_pct"], 0.12)
check7("alerts spread over <= 5 minutes (fits the 8-minute job limit)", s.CONFIG["alert_window_sec"] <= 300, True)
print(f"round 3: {sum(R7)}/{len(R7)} passed")

# ---- real false positives from the first console run (5 Nov 01:17) ----
R8 = []
def check8(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R8.append(ok)
for cat, title, price in [
    ("xboxsx", "MLB® The Show™ 23 (Xbox Series X) [Complete & Tested]", 12),
    ("xboxsx", "SanDisk Optimus GX C50 2TB Storage Expansion Card for Xbox Series X|S", 300),
    ("xboxsx", "WD Black C50 1TB Storage Expansion Card for Xbox Series X|S", 230),
    ("xboxsx", "Halo Campaign Evolved Steelbook Steel Book Premium Xbox Series X", 30),
    ("ps5", "Sony PSVR2 PlayStation VR Headset And Controllers PS5", 200),
    ("ps5", "PS5 Vr2 headset with 2 controllers / Excellent Condition", 200),
    ("ps5", "PRE-ORDER GTA 6 VI Limited Edition PS5 Controller", 220),
    ("ps5", "PLAYSTATION 5 VR2 HEADSET AND CONTROLLER CHARGING STATION", 230),
    ("switch2", "Nintendo Switch 2 Pro Controller Zelda 40th Anniversary", 200),
]:
    check8(f"rejected: {title[:45]}", s.triage(CATS[cat], item(title, price, fb=300)), None)
for cat, title, price in [
    ("xboxsx", "Xbox Series X Black Console", 300),
    ("xboxsx", "Microsoft Xbox Series X 1TB Black", 300),
    ("ps5", "Sony PlayStation 5 Slim Disc Console boxed", 330),
    ("switch2", "Nintendo Switch 2 Console Black w/ Blue/Red Joy-Cons", 255),
    ("xboxss", "Xbox Series S 512GB with controller", 150),
]:
    check8(f"kept: {title[:45]}", s.triage(CATS[cat], item(title, price, fb=300)) is not None, True)
print(f"console false positives: {sum(R8)}/{len(R8)} passed")

# ---- 4:25am fixes (5 Nov) ----
R9 = []
def check9(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R9.append(ok)
check9("Quest 3S power bank rejected", s.triage(CATS["quest3s"], item("Meta Quest 3s Power Bank, White,  Adjustable, Usb Charger, Chargers The Vr 100%", 100, fb=300)), None)
check9("Quest 3 charging dock rejected", s.triage(CATS["quest3"], item("Charging dock station for Meta Quest 3 controllers", 40, fb=300)), None)
check9("real Quest 3S kept", s.triage(CATS["quest3s"], item("Meta Quest 3S Standalone VR Headset 128GB with Controllers White", 105, fb=300)) is not None, True)
check9("real Quest 3 kept", s.triage(CATS["quest3"], item("Meta Quest 3 512GB VR headset", 200, fb=300)) is not None, True)
check9("thin-margin CHECK is quiet", s.quiet({"rating": "CHECK", "safe_profit": 10, "required_profit": 50}), True)
check9("good CHECK (new seller) still posts", s.quiet({"rating": "CHECK", "safe_profit": 60, "required_profit": 50}), False)
check9("SOLID never quiet", s.quiet({"rating": "SOLID", "safe_profit": 5, "required_profit": 50}), False)
print(f"4:25 fixes: {sum(R9)}/{len(R9)} passed")

# ---- 8 Nov: profit tiers, deals only ----
R10 = []
def check10(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R10.append(ok)
check10("£299 needs £50", s.required_profit(299), 50)
check10("£300 needs £75", s.required_profit(300), 75)
check10("£399 needs £75", s.required_profit(399.99), 75)
check10("£400 needs £100", s.required_profit(400), 100)
check10("target in £300 band leaves £75", s._buy_target(400, 0) , 309.3)   # B5: net of buyer fee (was 324)
check10("target never lands where profit < rule", all(n - s._buy_target(n, 0) - s.PACK >= s.required_profit(s._buy_target(n, 0)) - 0.01 for n in range(120, 1200, 7)), True)
check10("per-category cap overrides £500", s.buy_target(1000, 15, 900) > 500, True)
check10("#deals only gets DEALs (offers go to #send-an-offer)", s.for_deals({"verdict": "OFFER", "category": "quest3", "rating": "SOLID", "safe_profit": 99}), False)
print(f"8 Nov tiers: {sum(R10)}/{len(R10)} passed")

# ---- PS5 Pro (8 Nov) ----
R11 = []
def check11(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R11.append(ok)
c = s.triage(CATS["ps5pro"], item("Sony PlayStation 5 Pro 2TB Console boxed", 740, 0, fb=200))
check11("PS5 Pro at £740 found (over the £500 cap)", c is not None and c["model"], "PS5 Pro 2TB")
check11("PS5 Pro at £880 too dear", s.triage(CATS["ps5pro"], item("PS5 Pro console 2TB", 880, 0, fb=200)), None)
check11("DualSense Edge rejected", s.triage(CATS["ps5pro"], item("PS5 Pro DualSense Edge controller", 150, fb=200)), None)
check11("normal PS5 category still skips Pro", s.triage(CATS["ps5"], item("PS5 Pro 2TB console", 400, fb=200)), None)
check11("other categories still capped at £500", s.buy_target(2000, 10) <= 500, True)
check11("PS5 Pro at £760 (+£20 fee = over £770 max) too dear since 11 Nov", s.triage(CATS["ps5pro"], item("PS5 Pro console 2TB boxed", 760, 0, fb=200)), None)
r = run("ps5pro", item("Sony PlayStation 5 Pro 2TB Console boxed", 740, 0, fb=200), details())
check11("PS5 Pro £740 is a DEAL needing £100", (r["verdict"], r["required_profit"]), ("DEAL", 100))
print(f"PS5 Pro: {sum(R11)}/{len(R11)} passed")

# ---- AI check + fast scans (8 Nov) ----
R12 = []
def check12(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R12.append(ok)
import os as _os, json as _json
class _R:
    def __init__(self, code, j=None, content=b""): self.status_code=code; self.ok=code==200; self._j=j; self.content=content; self.headers={"Content-Type":"image/jpeg"}
    def json(self): return self._j
_calls=[]
def _post(url, **k):
    _calls.append(url.split("/models/")[1].split(":")[0])
    if "3.5" in url: return _R(429)
    return _R(200, {"candidates":[{"content":{"parts":[{"text":_json.dumps({"verdict":"no","code":"accessory","reason":"Controller only, not a console"})}]}}]})
_old_post, _old_get = s.requests.post, s.requests.get
s.requests.post = _post; s.requests.get = lambda url, **k: _R(200, content=b"img")
_os.environ["GEMINI_API_KEY"]="test"
row = {"title":"PS5 Pro controller","price":100,"all_in":104,"variant":"PS5 Pro 2TB","image_url":"http://x/i.jpg"}
v = s.ai_check(CATS["ps5pro"], row, {"condition":"Used","localizedAspects":[{"name":"Model","value":"Pro"}]})
check12("3.5 Flash-Lite out of quota -> falls back to 3.1", (_calls, v[0], v[2]), (["gemini-3.5-flash-lite","gemini-3.1-flash-lite"], "no", "gemini-3.1-flash-lite"))
s.requests.post = lambda url, **k: _R(429)
check12("both models out -> 'unavailable', alert still allowed", s.ai_check(CATS["ps5pro"], row, {})[0], "unavailable")
del _os.environ["GEMINI_API_KEY"]
check12("no key -> skipped", s.ai_check(CATS["ps5pro"], row, {})[0], "skipped")
s.requests.post, s.requests.get = _old_post, _old_get
check12("PS5 Pro is a fast category", CATS["ps5pro"].get("fast"), True)
check12("3.5 Flash-Lite tried first", s.CONFIG["ai_models"][0], "gemini-3.5-flash-lite")
_os.environ["FULL_SCAN"]="1"; check12("manual full scan works", s.full_scan_due(), True); del _os.environ["FULL_SCAN"]
print(f"AI + fast scans: {sum(R12)}/{len(R12)} passed")

# ---- #bot-logs channel (8 Nov) ----
R13 = []
def check13(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R13.append(ok)
s.WHY.clear()
s.triage(CATS["ps5pro"], item("Sony PlayStation 5 Pro 2TB Console boxed", 2000, 0, fb=200))
s.triage(CATS["ps5pro"], item("Sony PlayStation 5 Pro 2TB Console boxed", 760, 0, fb=0))
check13("rejection reasons are counted", (s.WHY[("ps5pro", "too pricey")], s.WHY[("ps5pro", "weak seller")]), (1, 1))
_sent = []
s.requests.post = lambda url, **k: (_sent.append(k["json"]), _R(204))[1]
s.LOG[:] = ["line"] * 200
_os.environ.pop("DISCORD_LOG_WEBHOOK_URL", None); s.post_logs()
check13("no log webhook -> nothing sent", len(_sent), 0)
_os.environ["DISCORD_LOG_WEBHOOK_URL"] = "http://log"; s.post_logs()
check13("logs split into Discord-sized messages", all(len(p["content"]) <= 2000 for p in _sent) and len(_sent) >= 1, True)
check13("logs never ping (no mentions, silent)", all(p["allowed_mentions"] == {"parse": []} and p["flags"] == 4096 for p in _sent), True)
del _os.environ["DISCORD_LOG_WEBHOOK_URL"]; s.requests.post = _old_post; s.LOG.clear()
print(f"bot logs: {sum(R13)}/{len(R13)} passed")

# ---- API usage bar (8 Nov) ----
R14 = []
def check14(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R14.append(ok)
from datetime import datetime as _dt, timezone as _tz
_n = _dt(2026, 11, 8, 23, 40, tzinfo=_tz.utc)                      # 15:40 Pacific
check14("usage day follows Pacific time", str(s.usage_day(_n)), "2026-11-08")
check14("08:30 UK still counts as the new Pacific day", str(s.usage_day(_dt(2026, 11, 9, 8, 30, tzinfo=_tz.utc))), "2026-11-09")
check14("bar 250/500 is half full", s.bar(250, 500), "▰" * 6 + "▱" * 6)
check14("bar never overflows", s.bar(900, 500), "▰" * 12)
txt = s.render_usage({"gemini-3.5-flash-lite": (12, 0), "ebay": (2101, 0), "gemini-3.1-flash-lite": (500, 3)}, {"gemini-3.5-flash-lite": 80, "ebay": 20000}, _n)
check14("shows used / limit / left", "12 / 500 · 488 left" in txt, True)
check14("shows limit hits", "hit limit 3x" in txt, True)
check14("AI checks left adds the free models", "AI checks left today: ~488" in txt, True)
check14("reset shown in UK time", "Resets 08:00 UK (in 8h 20m)" in txt, True)
check14("counts AI attempts, not 429s", (s.USAGE.get("gemini-3.1-flash-lite", 0) >= 1, s.LIMITED.get("gemini-3.5-flash-lite", 0) >= 1), (True, True))
print(f"usage bar: {sum(R14)}/{len(R14)} passed")

# ---- AI screening of word-filter drops (9 Nov) ----
R15 = []
def check15(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R15.append(ok)
r = s.triage(CATS["ps5pro"], item("Sony PS5 Pro 2TB with 2 controllers", 740, 0, fb=200))
check15("'PS5 Pro with 2 controllers' is now a real candidate", (r or {}).get("lane"), "EXACT")
s.SOFT.clear()
check15("'missing original box' isn't thrown away...", s.triage(CATS["ps5"], item("PS5 Slim Disc console, missing original box", 380, 0, fb=200)), None)
check15("...it goes to the AI screen", [(c["soft_reason"]) for _, c in s.SOFT], ["junk/wrong item"])
s.SOFT.clear()
s.triage(CATS["ps5"], item("PS5 Slim bundle with VR2 headset", 380, 0, fb=200))
check15("console + VR bundle goes to the AI screen", len(s.SOFT), 1)
s.SOFT.clear()
check15("faulty stays a hard reject (no AI)", (s.triage(CATS["ps5"], item("PS5 Slim 825GB faulty", 200, 0, fb=200)), len(s.SOFT)), (None, 0))
check15("overpriced never reaches the AI", (s.triage(CATS["ps5"], item("PS5 Slim missing box", 700, 0, fb=200)), len(s.SOFT)), (None, 0))
check15("weak seller never reaches the AI", (s.triage(CATS["ps5"], item("PS5 Slim missing box", 380, 0, fb=0)), len(s.SOFT)), (None, 0))

_os.environ["GEMINI_API_KEY"] = "test"
def _screen_post(url, **k):
    if "3.5" in url: return _R(429)
    return _R(200, {"candidates": [{"content": {"parts": [{"text": _json.dumps([
        {"i": 0, "v": "yes", "m": "PS5 Slim Disc", "r": "console, box missing"},
        {"i": 1, "v": "no", "m": "", "r": "controller only"},
        {"i": 2, "v": "yes", "m": "Made-up model", "r": "real console"}])}]}}]})
s.requests.post = _screen_post
s.SOFT.clear()
s.triage(CATS["ps5"], item("PS5 Slim Disc missing box", 355, 0, fb=200))
s.triage(CATS["ps5"], item("PS5 slim controller replacement", 380, 0, fb=200))
s.triage(CATS["ps5"], item("PlayStation 5 slim damaged box", 380, 0, fb=200))
got = s.ai_screen(list(s.SOFT))
ids = [c["item_id"] for _, c in s.SOFT]
check15("screen uses 3.1 (3.5 kept for photo checks first)", got[ids[0]][3], "gemini-3.1-flash-lite")
check15("yes + model parsed", got[ids[0]][:2], ("yes", "PS5 Slim Disc"))
check15("no parsed", got[ids[1]][0], "no")
check15("invented model name is ignored", got[ids[2]][:2], ("yes", ""))

fresh = []
s.LOG.clear()
rej = s.screen_soft({}, set(), fresh)
check15("rescued items join the normal checks", [(c["lane"], c["model"], bool(c.get("rescued"))) for _, c in fresh],
        [("EXACT", "PS5 Slim Disc", True), ("VAGUE", None, True)])
check15("AI 'no' is saved so it's never screened twice", [(r["status"], r["ai_verdict"]) for r in rej], [("ai_screened_out", "no")])
check15("saved rows hold no working-only fields", any(k in rej[0] for k in s.DROP), False)
check15("logs say what happened", any("2 rescued, 1 confirmed" in l for l in s.LOG), True)
fresh2 = []
check15("already-screened listings are skipped", (s.screen_soft({i: 0 for i in ids}, set(), fresh2), fresh2), ([], []))
s.requests.post = lambda url, **k: _R(429)
fresh3 = []
check15("Gemini unavailable -> nothing saved, retried next run", (s.screen_soft({}, set(), fresh3), fresh3), ([], []))
def _pricey(url, **k):
    return _R(200, {"candidates": [{"content": {"parts": [{"text": _json.dumps([{"i": 0, "v": "yes", "m": "PS5 Slim Digital", "r": "ok"}])}]}}]})
s.requests.post = _pricey
s.SOFT.clear()
s.triage(CATS["ps5"], item("PS5 Slim missing box", 400, 0, fb=200))
fresh4 = []
rej4 = s.screen_soft({}, set(), fresh4)
check15("genuine but over that model's target -> rejected by the maths", (fresh4, rej4[0]["ai_reason"] if rej4 else None), ([], "genuine but too pricey"))
s.requests.post = _old_post; del _os.environ["GEMINI_API_KEY"]; s.SOFT.clear(); s.LOG.clear()
print(f"AI screening: {sum(R15)}/{len(R15)} passed")

# ---- AI-approved listings waiting for a lookup aren't screened twice ----
R16 = []
def check16(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R16.append(ok)
s.SOFT.clear()
s.triage(CATS["ps5"], item("PS5 Slim Disc missing box", 355, 0, fb=200))
_id = s.SOFT[0][1]["item_id"]
s.AI_OK[_id] = "PS5 Slim Disc"
s.requests.post = lambda url, **k: (_ for _ in ()).throw(AssertionError("AI should not be called"))
fr = []
check16("approved-earlier listing goes straight to checks, no AI call", (s.screen_soft({}, set(), fr), [(c["lane"], c["model"]) for _, c in fr]), ([], [("EXACT", "PS5 Slim Disc")]))
s.requests.post = _old_post; s.AI_OK.clear(); s.SOFT.clear(); s.LOG.clear()
print(f"AI pending: {sum(R16)}/{len(R16)} passed")

# ---- an AI 'no' only applies to the category it was screened in ----
R17 = []
def check17(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R17.append(ok)
s.SOFT.clear(); s.SCREENED_IN.clear()
s.triage(CATS["ps5"], item("PS5 Slim Disc missing box", 355, 0, fb=200))
_id = s.SOFT[0][1]["item_id"]
s.SCREENED_IN[_id] = "xboxsx"                       # was screened out of a different category's search
_os.environ["GEMINI_API_KEY"] = "test"
called = []
s.requests.post = lambda url, **k: (called.append(1), _R(200, {"candidates": [{"content": {"parts": [{"text": "[]"}]}}]}))[1]
s.screen_soft({_id: 393.0}, set(), [])
check17("rejected in another category -> judged again for this one", len(called), 1)
called.clear(); s.SCREENED_IN[_id] = "ps5"
s.screen_soft({_id: 393.0}, set(), [])
check17("rejected in this category -> not screened again", len(called), 0)
check17("screen prompt rejects incomplete units", "essential part is missing" in s.SCREEN_PROMPT, True)
s.requests.post = _old_post; del _os.environ["GEMINI_API_KEY"]; s.SOFT.clear(); s.SCREENED_IN.clear(); s.LOG.clear()
print(f"category-aware screening: {sum(R17)}/{len(R17)} passed")

# ---- Quest 3 fast scan uses only its main search ----
_q3 = {x["id"]: x for x in s.CONFIG["categories"]}["quest3"]
print(("PASS " if _q3.get("fast") and _q3.get("fast_queries") == ["meta quest 3"] else "FAIL ") + "Quest 3 fast scan = 1 search")
print(("PASS " if not any(x["enabled"] for x in s.CONFIG["categories"] if x["id"] in ("bose", "osmo3", "sony")) else "FAIL ") + "Bose, Osmo 3, Sony off")

# ---- 10 Nov: 3 categories, Xbox hot-deals-only, controller rule ----
R18 = []
def check18(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R18.append(ok)
_on = sorted(x["id"] for x in s.CONFIG["categories"] if x.get("enabled"))
check18("on: PS5 Pro, Quest 3, Xbox Series X + S, standard PS5 (offers), Switch 2 + Steam Deck (19 Nov, via the console sweep)", _on, ["ps5", "ps5pro", "quest3", "steamdeck", "switch2", "xboxss", "xboxsx"])
_xb = CATS["xboxsx"]
check18("Xbox disc target clears £90 at a typical sale", s.cat_target(_xb, _xb["models"][1]), 380.32)   # 18 Nov prices (typical £499); was 361.7
r = run("xboxsx", item("Xbox Series X 1TB console with controller", 340, fb=200), details())
check18("Xbox deal needs £90 and clears it", (r["required_profit"], r["safe_profit"] >= 90), (90, True))
check18("Xbox £1 over target: no offer alert in #deals (goes to #send-an-offer lane)", s.for_deals(run("xboxsx", {**item("Xbox Series X 1TB console with controller", 370, fb=200), "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"]}, details()) or {}), False)
d = details(); d["description"] = "Selling my Xbox Series X. Console only, no controller included, sorry."
check18("description says no controller -> rejected", run("xboxsx", item("Xbox Series X 1TB console", 330, fb=200), d), None)
d = details(); d["description"] = "Console only used a few times, comes with controller and leads."
check18("'console only used a few times' is not a no-controller listing", run("xboxsx", item("Xbox Series X 1TB console", 330, fb=200), d) is not None, True)
check18("PS5 Pro also needs a controller", CATS["ps5pro"].get("needs_controller"), True)
check18("AI prompt asks about the controller", "at least one working controller" in s.PAD_RULE, True)
import datetime as _dt
class _FakeDT(_dt.datetime):
    m = 1
    @classmethod
    def now(cls, tz=None): return _dt.datetime(2026, 11, 10, 22, cls.m, tzinfo=tz)
_old_dt = s.datetime; s.datetime = _FakeDT
_picked = lambda: [c["id"] for c in s.CONFIG["categories"] if c.get("enabled") and c["models"] and c.get("fast")
                   and _FakeDT.now().minute % c.get("fast_every_min", 1) == 0]
_FakeDT.m = 1; m1 = _picked(); _FakeDT.m = 2; m2 = _picked(); _FakeDT.m = 3; m3 = _picked(); _FakeDT.m = 6; m6 = _picked()
s.datetime = _old_dt
check18("19 Nov keyword speeds: Quest 3 every minute (consoles now come from the every-minute sweep instead)",
        (sorted(m1), sorted(m2), sorted(m3), sorted(m6)),
        (["ps5pro", "quest3"], ["ps5pro", "quest3", "xboxsx"], ["ps5", "ps5pro", "quest3", "xboxss"], ["ps5", "ps5pro", "quest3", "xboxss", "xboxsx"]))
print(f"3 categories + Xbox rules: {sum(R18)}/{len(R18)} passed")

# --- 19. eBay token is reused for ~2 hours instead of fetched every run (12 Nov 2026) ---
import os, json
from collections import Counter
R19 = []
def check19(name, got, want):
    ok = got == want; R19.append(ok); print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  got {got!r}, want {want!r}"))
class _R:
    def __init__(self, code=200, j=None): self.status_code = code; self.ok = code < 400; self._j = j
    def json(self): return self._j
    def raise_for_status(self):
        if not self.ok: raise s.requests.HTTPError(str(self.status_code))
_store, _calls = {}, Counter()
def _tget(url, **k):
    _calls["get"] += 1
    v = _store.get(s.TOKEN_KEY)
    return _R(200, [{"value": v}] if v else [])
def _tpost(url, **k):
    if "oauth2/token" in url:
        _calls["ebay_token"] += 1
        return _R(200, {"access_token": f"tok{_calls['ebay_token']}", "expires_in": 7200})
    _store[k["json"]["key"]] = k["json"]["value"]; return _R(201)
def _tdel(url, **k):
    _store.pop(s.TOKEN_KEY, None); return _R(204)
_saved = (s.requests.get, s.requests.post, s.requests.delete, s.DRY_RUN, dict(os.environ))
s.requests.get, s.requests.post, s.requests.delete, s.DRY_RUN = _tget, _tpost, _tdel, False
os.environ.update(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_KEY="k", EBAY_CLIENT_ID="a", EBAY_CLIENT_SECRET="b")
t1 = s.ebay_token(); t2 = s.ebay_token()
check19("first run asks eBay for a token", t1, "tok1")
check19("next run reuses it (no new eBay token call)", (t2, _calls["ebay_token"]), ("tok1", 1))
_store[s.TOKEN_KEY] = json.dumps({"t": "old", "exp": (s.datetime.now(s.timezone.utc) + s.timedelta(minutes=5)).isoformat()})
check19("token about to expire (<10 min) is replaced", s.ebay_token(), "tok2")
s.forget_ebay_token()
check19("a rejected token is forgotten, next run gets a new one", s.ebay_token(), "tok3")
_store[s.TOKEN_KEY] = "not json"
check19("broken cache row doesn't crash, just fetches a new token", s.ebay_token(), "tok4")
def _down(url, **k): raise s.requests.ConnectionError("db down")
s.requests.get = _down
check19("database down: still gets a token from eBay", s.ebay_token(), "tok5")
s.requests.get, s.requests.post, s.requests.delete, s.DRY_RUN = _saved[:4]
os.environ.clear(); os.environ.update(_saved[4])
print(f"Token reuse: {sum(R19)}/{len(R19)} passed")

# --- 20. A1 survival tracking (12 Nov 2026) ---
R20 = []
def check20(name, got, want):
    ok = got == want; R20.append(ok); print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  got {got!r}, want {want!r}"))
_t0 = s.datetime(2026, 11, 12, 10, 0, tzinfo=s.timezone.utc)
_m = lambda mins: _t0 + s.timedelta(minutes=mins)
check20("check schedule is 1, 2, 3, 5, 10, 20 minutes", s.SURVIVAL_MIN, [1, 2, 3, 5, 10, 20])
check20("first check not due at 30 s", s.survival_due(_t0, 0, _m(0.5)), False)
check20("first check due at 1 min", s.survival_due(_t0, 0, _m(1)), True)
check20("4th check (5 min) not due at 4 min", s.survival_due(_t0, 3, _m(4)), False)
check20("4th check due at 5 min", s.survival_due(_t0, 3, _m(5.2)), True)
check20("no checks after the 6th", s.survival_due(_t0, 6, _m(60)), False)
# full survival_checks pass with fake eBay + Supabase
_patches, _ebay = [], {"gone1": (404, None), "live1": (200, {"estimatedAvailabilities": [{"estimatedAvailabilityStatus": "IN_STOCK"}]}),
                       "sold1": (200, {"estimatedAvailabilities": [{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}]})}
class _RR:
    def __init__(self, code, j): self.status_code = code; self.ok = code < 400; self._j = j
    def json(self): return self._j
    def raise_for_status(self): pass
_now = s.datetime.now(s.timezone.utc)
_rows = [{"item_id": i, "title": i, "all_in": 700, "posted_at": (_now - s.timedelta(minutes=2.5)).isoformat(), "live_checks": 1} for i in _ebay]
_rows.append({"item_id": "notdue", "title": "notdue", "all_in": 700, "posted_at": (_now - s.timedelta(seconds=20)).isoformat(), "live_checks": 0})
def _sget(url, **k):
    if "/rest/v1/" in url: return _RR(200, _rows)
    return _RR(*_ebay[url.rsplit("/", 1)[1]])
_saved20 = (s.requests.get, s.requests.patch, s.DRY_RUN, dict(os.environ))
s.requests.get = _sget; s.requests.patch = lambda url, **k: _patches.append((k["params"]["item_id"][3:], k["json"])); s.DRY_RUN = False
os.environ.update(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_KEY="k")
s.survival_checks("tok")
_p = dict(_patches)
check20("removed listing recorded as gone", (_p["gone1"].get("outcome"), "gone_at" in _p["gone1"], _p["gone1"]["live_checks"]), ("gone", True, 2))
check20("sold-out listing recorded as gone", _p["sold1"].get("outcome"), "gone")
check20("live listing recorded as still for sale", ("last_live_at" in _p["live1"], "gone_at" in _p["live1"]), (True, False))
check20("alert posted 20 s ago is not checked yet", "notdue" in _p, False)
s.requests.get, s.requests.patch, s.DRY_RUN = _saved20[:3]; os.environ.clear(); os.environ.update(_saved20[3])
print(f"A1 survival tracking: {sum(R20)}/{len(R20)} passed")

# --- 21. A2: don't ping deals that have already sold or ended (12 Nov 2026) ---
R21 = []
def check21(name, got, want):
    ok = got == want; R21.append(ok); print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  got {got!r}, want {want!r}"))
_n = s.datetime(2026, 11, 12, 10, 0, tzinfo=s.timezone.utc)
check21("sold out = ended", s.ended({"estimatedAvailabilities": [{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}]}, _n), True)
check21("end date in the past = ended", s.ended({"itemEndDate": "2026-11-12T09:59:00.000Z"}, _n), True)
check21("in stock, ends next month = not ended", s.ended({"estimatedAvailabilities": [{"estimatedAvailabilityStatus": "IN_STOCK"}],
                                                        "itemEndDate": "2026-12-12T10:00:00.000Z"}, _n), False)
check21("no info = not ended", s.ended({}, _n), False)
# whole run (dry run, fake eBay): one sold-out deal, one 404, one live deal
_it = lambda iid, price: {**item("Sony PlayStation 5 Pro 2TB Console boxed", price, 0, fb=200), "itemId": iid}
_det_live = details(); _det_sold = {**details(), "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}]}
class _R404:
    status_code = 404; ok = False
    def json(self): return {}
class _R200:
    def __init__(self, j): self.status_code = 200; self.ok = True; self._j = j
    def json(self): return self._j
def _iget(url, **k):
    iid = url.rsplit("/", 1)[1]
    return _R404() if iid == "v|404|0" else _R200(_det_sold if iid == "v|sold|0" else _det_live)
_keep21 = {n: getattr(s, n) for n in ("ebay_token", "ebay_search", "already_seen", "full_scan_due", "ai_check", "screen_soft", "DRY_RUN")}
_get21 = s.requests.get
s.ebay_token = lambda: "tok"; s.full_scan_due = lambda: False; s.already_seen = lambda ids: {}
s.screen_soft = lambda old, ids, fresh: []; s.ai_check = lambda cat, row, d: ("yes", "ok", "fake")
s.ebay_search = lambda tok, cat, q, **kw: [_it("v|sold|0", 700), _it("v|404|0", 705), _it("v|live|0", 710)] if cat["id"] in ("ps5pro", "consoles") else []
s.requests.get = _iget; s.DRY_RUN = True; s.LOG.clear(); s.GONE.clear()
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    s.main()
_log = "\n".join(s.LOG)
check21("sold-out deal not posted, logged as gone", "🫥" in _log and "would have been a DEAL" in _log, True)
check21("404 deal not posted, logged as gone", _log.count("sold or ended before we saw it"), 2)
check21("live deal still posts", "✅" in _log and "DEAL, posting" in _log, True)
for n, v in _keep21.items(): setattr(s, n, v)
s.requests.get = _get21; s.LOG.clear(); s.GONE.clear()
print(f"A2 don't ping the dead: {sum(R21)}/{len(R21)} passed")

# --- 22. A3: edit the Discord card when a deal has gone (12 Nov 2026) ---
R22 = []
def check22(name, got, want):
    ok = got == want; R22.append(ok); print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  got {got!r}, want {want!r}"))
_sent22 = []
class _M:
    status_code = 200; ok = True
    def json(self): return {"id": "m1", "embeds": [{"title": "Sony PS5 Pro 2TB", "color": 0xFF0000,
                                                    "fields": [{"name": "💷 Price", "value": "£740"}]}]}
_keep22 = (s.requests.get, s.requests.patch, s.DRY_RUN, dict(os.environ))
s.requests.get = lambda url, **k: _M()
s.requests.patch = lambda url, **k: _sent22.append((url, k["json"]))
s.DRY_RUN = False; os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.test/hook"
_now22 = s.datetime(2026, 11, 12, 10, 3, tzinfo=s.timezone.utc)
s.mark_card_gone("m1", "2026-11-12T10:01:00+00:00", _now22)
_u, _j = _sent22[0]
check22("edits that exact message", _u, "https://discord.test/hook/messages/m1")
check22("card says gone after 2 min", (_j["content"], _j["embeds"][0]["title"]), ("❌ Gone after 2 min", "❌ GONE · Sony PS5 Pro 2TB"))
check22("card turns grey and keeps its price", (_j["embeds"][0]["color"], _j["embeds"][0]["fields"][0]["value"]), (0x808080, "£740"))
check22("edit never pings anyone", _j["allowed_mentions"], {"parse": []})
_sent22.clear(); s.mark_card_gone(None, None, _now22)
check22("alerts from before A1 (no message id) are left alone", _sent22, [])
s.requests.get, s.requests.patch, s.DRY_RUN = _keep22[:3]; os.environ.clear(); os.environ.update(_keep22[3])
print(f"A3 gone cards: {sum(R22)}/{len(R22)} passed")

# --- 23. A4: how old is the newest listing in a search (eBay's delay) (16 Nov 2026) ---
R23 = []
def check23(name, got, want):
    ok = got == want; R23.append(ok); print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  got {got!r}, want {want!r}"))
_n23 = s.datetime(2026, 11, 16, 22, 0, tzinfo=s.timezone.utc)
check23("newest of several listings", s.newest_age_min([{"itemCreationDate": "2026-11-16T21:50:00.000Z"},
                                                        {"itemCreationDate": "2026-11-16T21:56:30.000Z"}], _n23), 3.5)
check23("no listings = nothing to report", s.newest_age_min([], _n23), None)
check23("missing or broken dates are skipped", s.newest_age_min([{"itemCreationDate": None}, {}, {"itemCreationDate": "2026-11-16T21:58:00Z"}], _n23), 2.0)
print(f"A4 freshness: {sum(R23)}/{len(R23)} passed")

# --- 24. #send-an-offer channel: standard PS5 + Xbox Series X, £50+ at an average resale (16 Nov 2026) ---
R24 = []
def check24(name, got, want):
    ok = got == want; R24.append(ok); print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  got {got!r}, want {want!r}"))
_xb, _p5 = CATS["xboxsx"], CATS["ps5"]
_xdisc = _xb["models"][1]
_ot = s.offer_target(_xb, _xdisc)
check24("Xbox disc offer target leaves £30 at a typical sale", round(s.net_sale(_xdisc["typical"]) - _xb["post_out"] - s.PACK - _ot, 2), 30.0)
check24("PS5 Pro has no offer lane", s.offer_target(CATS["ps5pro"], CATS["ps5pro"]["models"][0]), None)
check24("Quest 3 has no offer lane", s.offer_target(CATS["quest3"], CATS["quest3"]["models"][0]), None)
_bo = lambda it: {**it, "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"]}
# Xbox above the hot-deal bar but a bit over the £50 target, seller takes offers -> #send-an-offer with an offer price
r = run("xboxsx", _bo(item("Xbox Series X 1TB console with controller", _ot + 5, fb=200)), details())
check24("Xbox over the £50 target + Best Offer -> OFFER with a suggested offer", (r["verdict"], bool(r["suggested_offer"])), ("OFFER", True))
check24("...goes to #send-an-offer, not #deals", (s.for_offers(r), s.for_deals(r)), (True, False))
check24("...£30+ at an average price if the offer is accepted", r["safe_profit"] >= 30, True)
# Xbox over the £50 target, seller does NOT take offers -> dropped
check24("Xbox over the £50 target without Best Offer is dropped", run("xboxsx", item("Xbox Series X 1TB console with controller", _ot + 5, fb=200), details()), None)
# Xbox way over (beyond 12%) -> dropped
check24("Xbox far over the target is dropped", run("xboxsx", _bo(item("Xbox Series X 1TB console with controller", _ot * 1.2, fb=200)), details()), None)
# Xbox real hot deal still goes to #deals
r = run("xboxsx", item("Xbox Series X 1TB console with controller", 330, fb=200), details())
check24("Xbox hot deal still goes to #deals only", (r["verdict"], s.for_deals(r), s.for_offers(r)), ("DEAL", True, False))
# standard PS5: everything goes to #send-an-offer, nothing to #deals
_pm = _p5["models"][1]
r = run("ps5", _bo(item("Sony PS5 Slim Disc console with controller", s.offer_target(_p5, _pm) + 5, fb=200)), details())
check24("standard PS5 offer -> #send-an-offer", (r and r["verdict"], r and s.for_offers(r), r and s.for_deals(r)), ("OFFER", True, False))
r = run("ps5", item("Sony PS5 Slim Disc console with controller", 300, fb=200), details())
check24("standard PS5 cheap deal -> #send-an-offer, never #deals", (r["verdict"], s.for_offers(r), s.for_deals(r)), ("DEAL", True, False))
check24("offer rows under £30 at an average price are never posted", s.for_offers({"category": "xboxsx", "verdict": "OFFER", "safe_profit": 29}), False)
check24("AI-blocked offers are never posted", s.for_offers({"category": "xboxsx", "verdict": "OFFER", "safe_profit": 80, "status": "ai_rejected"}), False)
# posting: no ping, goes to the offers webhook
_sent24 = []
class _P:
    status_code = 200; ok = True
    def json(self): return {"id": "o1"}
    def raise_for_status(self): pass
_keep24 = (s.requests.post, dict(os.environ))
s.requests.post = lambda url, **k: (_sent24.append((url, k["json"])), _P())[1]
os.environ.update(DISCORD_WEBHOOK_URL="https://d/deals", DISCORD_OFFERS_WEBHOOK_URL="https://d/offers")
_row = run("xboxsx", _bo(item("Xbox Series X 1TB console with controller", _ot + 5, fb=200)), details())
_mid = s.discord_alert(_row, offers=True)
check24("offer card goes to the #send-an-offer webhook", _sent24[0][0], "https://d/offers")
check24("offer card never pings", (_sent24[0][1]["allowed_mentions"], "content" in _sent24[0][1]), ({"parse": []}, False))
check24("offer card is labelled 'Send an offer'", _sent24[0][1]["embeds"][0]["author"]["name"].startswith("🟡 Send an offer"), True)
s.requests.post = _keep24[0]; os.environ.clear(); os.environ.update(_keep24[1])
print(f"#send-an-offer: {sum(R24)}/{len(R24)} passed")

# ---- Phase B (16 Nov) ----
import json as _pj, os as _pos
from datetime import datetime as _dt, timedelta as _td, timezone as _tz, date as _date

class _FR:
    def __init__(self, code, j=None, content=b""): self.status_code=code; self.ok=code==200; self._j=j; self.content=content; self.headers={"Content-Type":"image/jpeg"}
    def json(self): return self._j
    def raise_for_status(self): pass

def _mk(n):
    R_ = []
    def chk(name, got, want):
        ok = got == want
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
        R_.append(ok)
    return R_, chk

# --- 25. B1 alerts are not spaced out ---
R25, check25 = _mk(25)
check25("alert gap is 0 seconds", s.CONFIG["alert_gap_sec"], 0)
check25("drip-feed gap formula gives 0 for any number of alerts",
        [min(s.CONFIG.get("alert_gap_sec", 55), s.CONFIG.get("alert_window_sec", 480) / max(1, n)) for n in (1, 4, 9)], [0, 0, 0])
print(f"B1 alert gap: {sum(R25)}/{len(R25)} passed")

# --- 26. B2 the AI may only block for listed reasons ---
R26, check26 = _mk(26)
check26("no + listed code stays no", s.apply_ai_rules("no", "accessory", "controller only"), ("no", "controller only"))
check26("all five listed codes can block", all(s.apply_ai_rules("no", c, "x")[0] == "no" for c in
        ("accessory", "different_model", "faulty", "missing_essential_part", "not_the_item")), True)
check26("no + 'other' becomes unsure with a prefix", s.apply_ai_rules("no", "other", "serial looks odd"),
        ("unsure", "(AI wanted to block for an unlisted reason) serial looks odd"))
check26("no + missing code becomes unsure", s.apply_ai_rules("no", "", "too cheap")[0], "unsure")
check26("no + made-up code becomes unsure", s.apply_ai_rules("no", "price_too_low", "cheap")[0], "unsure")
check26("code is case-insensitive", s.apply_ai_rules("no", "FAULTY", "x")[0], "no")
check26("yes and unsure are untouched", (s.apply_ai_rules("yes", None, "ok"), s.apply_ai_rules("unsure", "other", "hm")), (("yes", "ok"), ("unsure", "hm")))
check26("prompt lists the codes and bans price/serial blocks",
        all(c in s.AI_PROMPT for c in ("accessory", "different_model", "faulty", "missing_essential_part", "not_the_item", "other"))
        and "price" in s.AI_PROMPT and "serial" in s.AI_PROMPT, True)
_k26 = (s.requests.post, s.requests.get, dict(_pos.environ))
_reply26 = {}
s.requests.get = lambda url, **k: _FR(200, content=b"img")
s.requests.post = lambda url, **k: _FR(200, {"candidates": [{"content": {"parts": [{"text": _pj.dumps(_reply26)}]}}]})
_pos.environ["GEMINI_API_KEY"] = "test"
_row26 = {"title": "PS5 Pro", "price": 100, "all_in": 104, "variant": "PS5 Pro 2TB", "image_url": "http://x/i.jpg"}
_reply26 = {"verdict": "no", "code": "other", "reason": "serial looks fake"}
_v = s.ai_check(CATS["ps5pro"], _row26, {})
check26("ai_check: 'no' for an unlisted reason -> unsure (same 3-part shape)", (_v[0], _v[1].startswith("(AI wanted to block for an unlisted reason)"), len(_v)), ("unsure", True, 3))
_reply26 = {"verdict": "no", "code": "faulty", "reason": "cracked screen"}
check26("ai_check: 'no' + faulty still blocks", s.ai_check(CATS["ps5pro"], _row26, {})[:2], ("no", "cracked screen"))
_reply26 = {"verdict": "yes", "reason": "looks fine"}
check26("ai_check: old-style reply without a code still works", s.ai_check(CATS["ps5pro"], _row26, {})[:2], ("yes", "looks fine"))
s.requests.post, s.requests.get = _k26[0], _k26[1]; _pos.environ.clear(); _pos.environ.update(_k26[2])
print(f"B2 AI reason codes: {sum(R26)}/{len(R26)} passed")

# --- 27. B3 "too cheap to be true" ---
R27, check27 = _mk(27)
_pp = CATS["ps5pro"]; _pm = _pp["models"][0]
check27("ps5pro and xboxsx have a new shop price, quest3 does not",
        (CATS["ps5pro"].get("new_shop_price"), CATS["xboxsx"].get("new_shop_price"), CATS["quest3"].get("new_shop_price")), (790, 670, None))
check27("under 75% of typical -> flagged", bool(s.too_cheap_to_be_true(_pp, _pm, 640, "used")), True)
check27("just above 75% of typical, used -> not flagged", s.too_cheap_to_be_true(_pp, _pm, 660, "used"), None)
check27("listed new and under the shop price -> flagged", bool(s.too_cheap_to_be_true(_pp, _pm, 700, "new")), True)
check27("'sealed' also counts as new", bool(s.too_cheap_to_be_true(_pp, _pm, 700, "sealed")), True)
check27("listed new but at/above the shop price -> not flagged", s.too_cheap_to_be_true(_pp, _pm, 790, "brand new"), None)
check27("category without new_shop_price skips the new rule", s.too_cheap_to_be_true(CATS["quest3"], CATS["quest3"]["models"][0], 300, "new"), None)
_it27 = item("Sony PS5 Pro 2TB console with controller", 600, fb=500)
r = run("ps5pro", {**_it27, "condition": "Used"}, details())
check27("very cheap PS5 Pro: note added and never HOT",
        (any(n.startswith("⚠️ Too cheap to be true?") for n in r["notes"].split("\n")), r["rating"] != "HOT", "scam" in r["notes"]), (True, True, True))
check27("the old 'Unusually cheap' note is gone", "Unusually cheap" in r["notes"], False)
r = run("ps5pro", {**item("Sony PS5 Pro 2TB console with controller", 680, fb=500), "condition": "New"}, details())
check27("new at £700 all-in (under £790) -> flagged via the shop-price rule", "Too cheap to be true" in r["notes"], True)
r = run("ps5pro", {**item("Sony PS5 Pro 2TB console with controller", 680, fb=500), "condition": "Used"}, details())
check27("same price listed used -> not flagged", "Too cheap to be true" in (r["notes"] or ""), False)
print(f"B3 too cheap to be true: {sum(R27)}/{len(R27)} passed")

# --- 28. B4 full scans on time ---
R28, check28 = _mk(28)
_now = _dt(2026, 11, 16, 12, 0, tzinfo=_tz.utc)
check28("11 min since last full scan, every 12 -> not due", s.full_due_since((_now - _td(minutes=11)).isoformat(), _now, 12), False)
check28("12 min since -> due", s.full_due_since((_now - _td(minutes=12)).isoformat(), _now, 12), True)
check28("40 min since -> due", s.full_due_since((_now - _td(minutes=40)).isoformat(), _now, 12), True)
check28("no record yet -> due", s.full_due_since(None, _now, 12), True)
check28("unreadable timestamp -> due", s.full_due_since("garbage", _now, 12), True)
check28("'Z' timestamps work", s.full_due_since("2026-11-16T11:55:00Z", _now, 12), False)
_k28 = (s.requests.get, s.requests.post, dict(_pos.environ), s.DRY_RUN)
_pos.environ.pop("FULL_SCAN", None); _pos.environ["SUPABASE_URL"] = "https://sb"; _pos.environ["SUPABASE_SERVICE_KEY"] = "k"
s.DRY_RUN = False
_last = {"v": (_dt.now(_tz.utc) - _td(minutes=5)).isoformat()}
s.requests.get = lambda url, **k: _FR(200, [{"value": _last["v"]}])
class _OK(_R):
    def raise_for_status(self): pass
s.requests.get = lambda url, **k: _OK(200, [{"value": _last["v"]}])
check28("reads bot_state: 5 min ago -> not due", s.full_scan_due(), False)
_last["v"] = (_dt.now(_tz.utc) - _td(minutes=13)).isoformat()
check28("reads bot_state: 13 min ago -> due", s.full_scan_due(), True)
_pos.environ["FULL_SCAN"] = "1"; _last["v"] = _dt.now(_tz.utc).isoformat()
check28("FULL_SCAN=1 still forces a full scan", s.full_scan_due(), True)
_pos.environ.pop("FULL_SCAN")
def _boom(url, **k): raise s.requests.RequestException("db down")
s.requests.get = _boom
_clock = (not s.CONFIG["full_scan_every_min"]) or _dt.now(_tz.utc).minute % s.CONFIG["full_scan_every_min"] < s.CONFIG.get("run_every_min", 3)
check28("database error -> falls back to the old clock rule", s.full_scan_due(), _clock)
s.DRY_RUN = True
check28("DRY_RUN -> falls back to the old clock rule", s.full_scan_due(), _clock)
s.DRY_RUN = False
_sent28 = []
s.requests.post = lambda url, **k: (_sent28.append((url, k)), _OK(200, {}))[1]
s.save_last_full_scan()
check28("finished full scan is saved to bot_state (merge-duplicates)",
        (_sent28[0][0], _sent28[0][1]["json"]["key"], _sent28[0][1]["headers"]["Prefer"], bool(_dt.fromisoformat(_sent28[0][1]["json"]["value"]))),
        ("https://sb/rest/v1/bot_state", "last_full_scan", "resolution=merge-duplicates", True))
s.requests.get, s.requests.post = _k28[0], _k28[1]; _pos.environ.clear(); _pos.environ.update(_k28[2]); s.DRY_RUN = _k28[3]
print(f"B4 full scan timing: {sum(R28)}/{len(R28)} passed")

# --- 29. B5 honest profit + price dates ---
R29, check29 = _mk(29)
check29("net_sale = price minus buyer fee", (s.net_sale(300), s.net_sale(100)), (287.3, 95.3))
_qm = CATS["quest3"]["models"][0]
r = run("quest3", item("Meta Quest 3 512GB with controllers", 250, fb=250), details())
check29("profit uses the net resale price",
        r["profit"], round(s.net_sale(_qm["strong"]) - CATS["quest3"]["post_out"] - s.PACK - r["all_in"], 2))
check29("safe profit uses the net typical price",
        r["safe_profit"], round(s.net_sale(_qm["typical"]) - CATS["quest3"]["post_out"] - s.PACK - r["all_in"], 2))
check29("buy target is lower than before the fee change (quest 512: was £286)", s.cat_target(CATS["quest3"], _qm) < 286, True)
check29("target still leaves the required profit", all(s.net_sale(m["strong"]) - CATS["quest3"]["post_out"] - s.PACK - s.cat_target(CATS["quest3"], m) >= s.required_profit(s.cat_target(CATS["quest3"], m)) - 0.01 for m in CATS["quest3"]["models"]), True)
check29("prices_checked set on every enabled category",
        {c["id"]: c.get("prices_checked") for c in s.CONFIG["categories"] if c.get("enabled")},
        {"quest3": "2026-11-04", "steamdeck": "2026-11-05", "switch2": "2026-11-05", "ps5": "2026-11-18", "xboxsx": "2026-11-18", "xboxss": "2026-11-18", "ps5pro": "2026-11-11"})
check29("price_age: label + fresh", s.price_age("2026-11-11", _date(2026, 11, 16)), ("prices checked 11 Nov", False))
check29("price_age: exactly 14 days is not stale, 15 is", (s.price_age("2026-11-01", _date(2026, 11, 15))[1], s.price_age("2026-11-01", _date(2026, 11, 16))[1]), (False, True))
check29("price_age: no date -> nothing", s.price_age(None, _date(2026, 11, 16)), ("", False))
_sent29 = []
_k29 = (s.requests.post, dict(_pos.environ))
s.requests.post = lambda url, **k: (_sent29.append(k["json"]), _P())[1]
_pos.environ.update(DISCORD_WEBHOOK_URL="https://d/deals")
_row29 = run("ps5pro", item("Sony PS5 Pro 2TB console with controller", 700, fb=500), details())
s.discord_alert(_row29)
_e = _sent29[0]["embeds"][0]
_d = _dt.now(_tz.utc).date(); _label = f"prices checked 11 Nov"
check29("card footer shows when prices were checked", _e["footer"]["text"].endswith(" · prices checked 11 Nov"), True)
_stale = (_d - _date(2026, 11, 11)).days > 14
check29("old-price warning appears only when over 14 days old",
        any("over 2 weeks old" in f["value"] for f in _e["fields"] if f["name"].endswith("Notes")), _stale)
_orig = CATS["ps5pro"]["prices_checked"]; CATS["ps5pro"]["prices_checked"] = "2020-01-01"
s.discord_alert(_row29)
check29("a very old price date adds the warning note", any("⚠️ PRICES 2+ WEEKS OLD" in f["value"] for f in _sent29[1]["embeds"][0]["fields"]), True)
CATS["ps5pro"]["prices_checked"] = _orig
s.requests.post = _k29[0]; _pos.environ.clear(); _pos.environ.update(_k29[1])
print(f"B5 net profit + price dates: {sum(R29)}/{len(R29)} passed")

# --- 30. B6 business-seller profit line ---
R30, check30 = _mk(30)
check30("business fee settings exist", (s.CONFIG["business_fee_pct"], s.CONFIG["business_fee_fixed"]), (0.075, 0.30))
r = run("quest3", item("Meta Quest 3 512GB with controllers", 250, fb=250), details())
_want = round(r["profit"] + (_qm["strong"] - s.net_sale(_qm["strong"])) - (_qm["strong"] * 0.075 + 0.30), 2)   # business: no buyer fee, minus eBay fee
check30("tag shows business profit (approx.)", f"BIZ ~£{_want:.0f}" in r["notes"], True)
check30("business profit is lower than normal profit", _want < r["profit"], True)
check30("no new database column: it only lives in the notes", "business_profit" in r, False)
print(f"B6 business profit line: {sum(R30)}/{len(R30)} passed")

# --- 31. #ai-blocked channel + heartbeat timestamp (18 Nov 2026) ---
R31 = []
def check31(name, got, want):
    ok = got == want; R31.append(ok); print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  got {got!r}, want {want!r}"))
_sent31 = []
_k31 = (s.requests.post, s.DRY_RUN, dict(os.environ), s.time.sleep)
s.requests.post = lambda url, **k: _sent31.append((url, k.get("json")))
s.time.sleep = lambda x: None
s.DRY_RUN = False
os.environ.pop("DISCORD_AIBLOCKED_WEBHOOK_URL", None)
s.post_ai_blocked([{"title": "PS5", "all_in": 300, "verdict": "DEAL", "status": "ai_rejected"}])
check31("no webhook set: nothing is sent", _sent31, [])
os.environ.update(DISCORD_AIBLOCKED_WEBHOOK_URL="https://d/blocked", SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_KEY="k")
s.post_ai_blocked([{"title": "Xbox Series X", "all_in": 320, "verdict": "DEAL", "ai_reason": "faulty", "safe_profit": 90, "category": "xboxsx"}])
_u, _j = _sent31[0]
check31("blocked deal goes to the #ai-blocked webhook", _u, "https://d/blocked")
check31("blocked card never pings and is silent", (_j["allowed_mentions"], _j["flags"]), ({"parse": []}, 4096))
check31("blocked card shows the AI's reason", "**Why:** faulty" in _j["embeds"][0]["description"], True)
_sent31.clear(); s.mark_run_finished()
check31("each run saves last_run to bot_state", (_sent31[0][0].endswith("/rest/v1/bot_state"), _sent31[0][1]["key"]), (True, "last_run"))
s.requests.post, s.DRY_RUN, _e, s.time.sleep = _k31[0], _k31[1], None, _k31[3]
os.environ.clear(); os.environ.update(_k31[2])
print(f"#ai-blocked + heartbeat: {sum(R31)}/{len(R31)} passed")

# --- 32. Xbox Series S: big deals only, £100+ profit at a typical sale (18 Nov 2026) ---
R32 = []
def check32(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R32.append(ok)
_ss = CATS["xboxss"]
_m1, _m5 = _ss["models"]
check32("Series S is hot-deals only: no offers lane, no offer zone", (_ss.get("offer_channel"), s.offer_zone(_ss)), (None, 0))
check32("Series S needs a controller", _ss.get("needs_controller"), True)
for _m in (_m1, _m5):
    check32(f"{_m['name']}: target leaves £100+ at a typical sale",
            round(s.net_sale(_m["typical"]) - _ss["post_out"] - s.PACK - s.cat_target(_ss, _m), 2) >= 100, True)
check32("512GB target is about £167", s.cat_target(_ss, _m5), 166.7)
check32("1TB target is about £209", s.cat_target(_ss, _m1), 208.64)
check32("search window covers the highest target", _ss["price_max"] >= s.cat_target(_ss, _m1), True)
_c = run("xboxss", item("Xbox Series S 512GB console with controller", 150, ship=5))
check32("cheap 512GB with controller is a DEAL", bool(_c) and _c["verdict"], "DEAL")
_c = run("xboxss", item("Xbox Series S 512GB console with controller", 200, ship=5))
check32("£205 512GB (only ~£60 profit) is not posted", bool(_c) and _c["verdict"] == "DEAL", False)
check32("Series X listing is not matched as Series S", bool(run("xboxss", item("Xbox Series X console with controller", 150))) and True, False)
_n = len(s.SOFT)
check32("BMW part from eBay's loose search is dropped without the AI", (run("xboxss", item("BMW 3 SERIES Door Mirror N/S 2005-2010 2 Door Convertible", 115)), len(s.SOFT) - _n), (None, 0))
check32("lazy Xbox title still reaches the AI screen", bool(run("xboxss", item("xbox series s", 150))) or len(s.SOFT) > 0, True)
check32("PS5 titles pass the brand check", bool(s.re.search(CATS["ps5"]["must_have"], s.norm("Sony PlayStation 5 Slim console"))), True)
check32("Quest titles pass the brand check", bool(s.re.search(CATS["quest3"]["must_have"], s.norm("Meta Quest 3 512GB"))), True)
print(f"Xbox Series S: {sum(R32)}/{len(R32)} passed")

# --- 33. Category sweep: one search covers every console (19 Nov 2026) ---
R33 = []
def check33(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
    R33.append(ok)
_sw = s.CONFIG["sweeps"][0]
check33("console sweep uses eBay's Video Game Consoles category", (_sw["category_ids"], _sw["limit"]), ("139971", 200))
check33("sweep price window covers every member's window",
        all(_sw["price_min"] <= CATS[c]["price_min"] and _sw["price_max"] >= CATS[c]["price_max"] for c in _sw["cats"]), True)
check33("every swept product has a brand check (no cross-product AI screening)",
        all(CATS[c].get("must_have") for c in _sw["cats"]), True)
_calls33 = []
_keep33 = s.ebay_search
s.ebay_search = lambda tok, cat, q, **kw: (_calls33.append((cat.get("id"), q, kw.get("category_ids"), kw.get("limit"))), [])[1]
s.LOG.clear()
_out = s.run_sweeps("tok", False, 7)
check33("one eBay call for all consoles", _calls33, [("consoles", None, "139971", 200)])
check33("every enabled console gets the sweep's listings",
        sorted(_out), sorted(c for c in _sw["cats"] if CATS[c].get("enabled")))
_calls33.clear(); _sw["every_min"] = 2
check33("not due on an odd minute", (s.run_sweeps("tok", False, 7), _calls33), ({}, []))
check33("full scans always sweep", bool(s.run_sweeps("tok", True, 7)), True)
_sw["every_min"] = 1
class _E(Exception): pass
def _fail(tok, cat, q, **kw):
    r = type("R", (), {"status_code": 500})()
    raise s.requests.HTTPError(response=r)
s.ebay_search = _fail; s.LOG.clear()
check33("failed sweep returns nothing, so products fall back to keyword searches", s.run_sweeps("tok", False, 7), {})
check33("failed sweep is logged", any("falling back" in l for l in s.LOG), True)
s.ebay_search = _keep33; s.LOG.clear()
_n = len(s.SOFT)
check33("PS5 listing in the Switch 2 check is dropped without the AI",
        (run("switch2", item("Sony PlayStation 5 Slim console with controller", 250)), len(s.SOFT) - _n), (None, 0))
check33("Xbox listing in the PS5 check is dropped without the AI",
        (run("ps5", item("Xbox Series X console with controller", 250)), len(s.SOFT) - _n), (None, 0))
_c = run("switch2", item("Nintendo Switch 2 console boxed", 220, ship=5))
check33("cheap Switch 2 console is a DEAL", bool(_c) and _c["verdict"], "DEAL")
check33("Steam Deck OLED title passes its brand check", bool(s.re.search(CATS["steamdeck"]["must_have"], s.norm("Valve Steam Deck OLED 512GB"))), True)
for _cid, _title, _want in [
        ("ps5", "Sony PlayStation 4 Slim 500GB Black Console+2 Dual Shock controllers", None),
        ("ps5", "Sony Playstation 2 Slim Boxed with 6 Games", None),
        ("ps5pro", "Sony PlayStation 4 Pro 1TB Console with Controller - Black", None),
        ("xboxsx", "Microsoft Xbox One X 1TB console with controller", None),
        ("switch2", "Nintendo Switch OLED console white boxed", None)]:
    _n = len(s.SOFT)
    check33(f"older model dropped without the AI: {_title[:40]}", (run(_cid, item(_title, 150)), len(s.SOFT) - _n), (_want, 0))
_n = len(s.SOFT)
_c = run("ps5", item("PS5 Slim Disc console with controller + PS4 games", 250, ship=5))
check33("PS5 bundle that mentions PS4 games is not dropped by the older-model rule", bool(_c) or len(s.SOFT) > _n, True)
print(f"Category sweep: {sum(R33)}/{len(R33)} passed")


# --- 34. 25 Nov: description check, delivery/location badge, short notes, shared AI caller ---
R34 = []
def check34(name, got, want):
    ok = got == want
    R34.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got={got!r} want={want!r}"))
for _t, _want in {
    "PS5 is dead, selling for parts": True, "Console won't turn on, otherwise mint": True,
    "Turns on but no display output": True, "Doesn't power on": True, "HDMI port is broken": True,
    "Stopped working last week": True, "Spares or repairs only": True,
    "Works perfectly. If the item is faulty please contact us": False, "Fully working, not faulty, never been banned": False,
    "Please do not turn off during updates": False, "No display issues, comes with 2 controllers": False,
    "Sold as seen, fully working": False, "Returns accepted if faulty": False,
}.items():
    check34(f"description check: {_t!r}", bool(s.dead_in({"description": f"<p>{_t}</p>"})), _want)
_c = s.triage(CATS["xboxsx"], item("Xbox Series X 1TB console with controller", 250))
if _c:
    _det = details(); _det["description"] = "<b>Great console</b> but it is dead, no power at all"
    check34("dead in description -> rejected after lookup", s.score(CATS["xboxsx"], _c, _det), None)
    check34("reason saved for #bot-logs", "dead" in s.REJECT_WHY.get(_c["item_id"], ""), True)
_it = item("Sony WH-1000XM6", 150); _it["itemLocation"] = {"country": "GB", "postalCode": "LS6****", "city": "leeds"}
_r = run("sony", _it, {**details(), "itemLocation": {"city": "Leeds", "postalCode": "LS6 2**", "country": "GB"}})
check34("location from listing details", _r["location"], "Leeds · LS6")
check34("free postage badge", s.delivery_badge({"delivery": "free", "location": "Leeds · LS6"}), "🚚 **FREE P&P** · 📍 Leeds · LS6")
check34("paid postage badge", s.delivery_badge({"delivery": "£4.99", "location": None}), "🚚 **P&P £4.99** (in the all-in price)")
check34("postage only in details badge", s.delivery_badge({"delivery": "£7.50 extra"}).startswith("🚚 **P&P £7.50** (add it"), True)
check34("collection badge", s.delivery_badge({"delivery": "collection", "location": "Leeds"}).startswith("⚠️ **COLLECTION ONLY**"), True)
check34("no buyer-fee line any more", "buyer protection fee" in (_r["notes"] or ""), False)
check34("tags are one line", sum(1 for l in (_r["notes"] or "").split("\n") if l.startswith("🏷️")), 1)
check34("delivery + location are saved columns", ("delivery" in _r, "location" in _r, "item_location" in _r), (True, True, False))
# card puts the badge at the top
_sent34 = []
_op = s.post_discord
s.post_discord = lambda payload, daily=False, url=None: _sent34.append(payload) or "1"
s.discord_alert({**_r, "rating": "SOLID", "verdict": "DEAL", "lane": "EXACT", "delivery": "collection", "location": "Leeds · LS6"})
s.post_discord = _op
check34("card description = COLLECTION ONLY badge", _sent34[0]["embeds"][0].get("description", "").startswith("⚠️ **COLLECTION ONLY**"), True)
# shared AI caller: Gemma gets no JSON mode; a 'per day' 429 marks the model used up and it is skipped next time
check34("gemma: no JSON mode", "responseMimeType" in s.ai_body("gemma-4-26b-a4b-it", [{"text": "x"}], 10)["generationConfig"], False)
check34("gemini: JSON mode", s.ai_body("gemini-3.5-flash-lite", [{"text": "x"}], 10)["generationConfig"]["responseMimeType"], "application/json")
class _R34:
    def __init__(self, code, text="", j=None): self.status_code = code; self.ok = code == 200; self.text = text; self._j = j
    def json(self): return self._j
_tried34 = []
def _p34(url, **k):
    m = url.split("/models/")[1].split(":")[0]; _tried34.append(m)
    if m == "a": return _R34(429, "Quota exceeded: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    if m == "b": return _R34(503, "high demand")
    return _R34(200, j={"candidates": [{"content": {"parts": [{"text": "```json\n{\"ok\": 1}\n```"}]}}]})
_old34 = (s.requests.post, s.DRY_RUN, os.environ.get("GEMINI_API_KEY"), s.time.sleep)
s.requests.post = _p34; s.DRY_RUN = False; os.environ["GEMINI_API_KEY"] = "k"; s.time.sleep = lambda x: None
s._EXH_LOADED[0] = True; s.EXHAUSTED.clear()
_txt, _m = s.ai_call(["a", "b", "c"], [{"text": "x"}], 10)
check34("falls through used-up + busy models", (_m, _txt.strip()), ("c", '{"ok": 1}'))
check34("used-up model remembered", "a" in s.EXHAUSTED, True)
_tried34.clear(); s.ai_call(["a", "c"], [{"text": "x"}], 10)
check34("used-up model skipped next call", _tried34, ["c"])
s.requests.post, s.DRY_RUN = _old34[0], _old34[1]; s.time.sleep = _old34[3]
if _old34[2] is None: os.environ.pop("GEMINI_API_KEY", None)
else: os.environ["GEMINI_API_KEY"] = _old34[2]
s.EXHAUSTED.clear()
check34("screens use 3.1 first (photo checks use 3.5 first)", (s.CONFIG["ai_screen_models"][0], s.CONFIG["ai_models"][0]), ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite"))
# screen pauses when only the reserve is left
_keep_left = s.ai_allowance_left
s.ai_allowance_left = lambda: 100
s.SOFT.clear(); s.LOG.clear()
s.SOFT.append((CATS["ps5"], {"item_id": "soft1", "all_in": 300, "title": "x", "soft_reason": "r", "best_offer": False}))
check34("screen paused when only the reserve is left", (s.screen_soft({}, set(), []), any("AI screen paused" in l for l in s.LOG)), ([], True))
s.ai_allowance_left = _keep_left; s.SOFT.clear()
check34("2.5 Flash-Lite (shut down) removed", "gemini-2.5-flash-lite" in s.CONFIG["ai_models"], False)
print(f"25 Nov description + card + AI: {sum(R34)}/{len(R34)} passed")
