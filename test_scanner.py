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
r = run("quest3", item("Meta Quest 3 512GB", 295, offer=True, fb=200), details())
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
    ("switch2", "Nintendo Switch 2 Console Black w/ Blue/Red Joy-Cons", 270),
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
check10("target in £300 band leaves £75", s._buy_target(400, 0) , 324.0)
check10("target never lands where profit < rule", all(n - s._buy_target(n, 0) - s.PACK >= s.required_profit(s._buy_target(n, 0)) - 0.01 for n in range(120, 1200, 7)), True)
check10("per-category cap overrides £500", s.buy_target(1000, 15, 900) > 500, True)
check10("offers never post live", "r[\"verdict\"] == \"DEAL\"" in open("scanner.py").read(), True)
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
    return _R(200, {"candidates":[{"content":{"parts":[{"text":_json.dumps({"verdict":"no","reason":"Controller only, not a console"})}]}}]})
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
txt = s.render_usage({"gemini-3.5-flash-lite": (12, 0), "ebay": (2101, 0), "gemini-2.5-flash-lite": (20, 3)}, {"gemini-3.5-flash-lite": 80, "ebay": 20000}, _n)
check14("shows used / limit / left", "12 / 500 · 488 left" in txt, True)
check14("shows limit hits", "hit limit 3x" in txt, True)
check14("AI checks left adds the free models", "AI checks left today: ~988" in txt, True)
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
s.triage(CATS["ps5"], item("PS5 Slim Disc missing box", 380, 0, fb=200))
s.triage(CATS["ps5"], item("PS5 slim controller replacement", 380, 0, fb=200))
s.triage(CATS["ps5"], item("PlayStation 5 slim damaged box", 380, 0, fb=200))
got = s.ai_screen(list(s.SOFT))
ids = [c["item_id"] for _, c in s.SOFT]
check15("screen falls back to 3.1 when 3.5 is out", got[ids[0]][3], "gemini-3.1-flash-lite")
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
s.triage(CATS["ps5"], item("PS5 Slim Disc missing box", 380, 0, fb=200))
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
s.triage(CATS["ps5"], item("PS5 Slim Disc missing box", 380, 0, fb=200))
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
check18("only PS5 Pro, Quest 3, Xbox Series X on", _on, ["ps5pro", "quest3", "xboxsx"])
_xb = CATS["xboxsx"]
check18("Xbox disc target clears £90 at a typical sale", s.cat_target(_xb, _xb["models"][1]), 378.0)
r = run("xboxsx", item("Xbox Series X 1TB console with controller", 360, fb=200), details())
check18("Xbox deal needs £90 and clears it", (r["required_profit"], r["safe_profit"] >= 90), (90, True))
check18("Xbox £1 over target is dropped (no offer alerts)", s.triage(_xb, {**item("Xbox Series X 1TB console", 370, fb=200), "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"]}), None)
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
_FakeDT.m = 1; odd = _picked(); _FakeDT.m = 2; even = _picked()
s.datetime = _old_dt
check18("Xbox every 2 minutes, others every minute", (sorted(odd), sorted(even)), (["ps5pro", "quest3"], ["ps5pro", "quest3", "xboxsx"]))
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
