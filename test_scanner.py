"""Offline tests for the scoring logic (no eBay/Supabase/Discord needed). Run: python test_scanner.py"""
import scanner as s

CATS = {c["id"]: c for c in s.CONFIG["categories"]}

def item(title, price, ship=0, offer=False, fb=50, pct=100.0, country="GB"):
    return {"itemId": "v1|" + title[:10], "title": title, "price": {"value": str(price)},
            "shippingOptions": [{"shippingCost": {"value": str(ship)}}],
            "buyingOptions": ["FIXED_PRICE"] + (["BEST_OFFER"] if offer else []),
            "seller": {"username": "x", "feedbackScore": fb, "feedbackPercentage": str(pct)},
            "itemLocation": {"country": country}, "itemWebUrl": "https://ebay.co.uk/itm/1"}

def check(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
    return ok

results = []
# Targets match the playbook
results.append(check("quest 512 target", s.buy_target(345, 8), 286))
results.append(check("steam deck 512 target (£100 rule)", s.buy_target(530, 9), 420))
results.append(check("xm5 target", s.buy_target(137, 6), 80))
# Deals / offers / rejects
r = s.evaluate(CATS["quest3"], item("Meta Quest 3 512GB with controllers", 270, 6))
results.append(check("quest 512 deal", (r["verdict"], r["variant"]), ("DEAL", "512GB")))
r = s.evaluate(CATS["quest3"], item("Meta Quest 3 VR headset", 240, 0))
results.append(check("untagged quest -> 128GB, too dear", r, None))
r = s.evaluate(CATS["quest3"], item("Meta Quest 3 512GB", 300, 0, offer=True))
results.append(check("quest offer zone", (r["verdict"], r["suggested_offer"]), ("OFFER", 286)))
results.append(check("quest 3S excluded", s.evaluate(CATS["quest3"], item("Meta Quest 3S 128GB", 150)), None))
results.append(check("broken sound excluded", s.evaluate(CATS["quest3"], item("Meta Quest 3 512GB broken sound", 150)), None))
results.append(check("foreign excluded", s.evaluate(CATS["xm6"], item("Sony WH-1000XM6", 150, country="CN")), None))
r = s.evaluate(CATS["qcultra"], item("Bose QuietComfort Ultra Headphones (2nd Gen) Black", 195))
results.append(check("bose gen2 detected", (r["verdict"], r["variant"]), ("DEAL", "Gen 2")))
results.append(check("bose gen1 at 195 rejected", s.evaluate(CATS["qcultra"], item("Bose QuietComfort Ultra Headphones Black", 195)), None))
r = s.evaluate(CATS["xm6"], item("Sony WH-1000XM6 black", 90, 4, fb=0))
results.append(check("cheap + new seller flagged", "new/low-feedback seller" in r["flags"] and "suspiciously cheap" in r["flags"], True))
r = s.evaluate(CATS["steamdeck"], item("Steam Deck OLED 512GB", 415, 5))
results.append(check("steam deck £420 all-in deal, needs £100", (r["verdict"], r["required_profit"], r["profit"] >= 100), ("DEAL", 100, True)))
print(f"\n{sum(results)}/{len(results)} passed")
