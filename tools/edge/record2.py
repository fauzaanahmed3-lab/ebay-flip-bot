import json, sys, os, io, contextlib
sys.path.insert(0, "/home/claude/ebay-flip-bot"); os.chdir("/home/claude/ebay-flip-bot")
import scanner as s
fx = json.load(open("/home/claude/scanner-edge/tools/fixtures.json"))
rows = [c["out"] for c in fx["calls"] if c["fn"] == "score" and c["out"]]
s.CONFIG.update(fx["configs"][list(fx["configs"])[0]])
s.CAT_BY_ID.update({c["cat"]["id"]: c["cat"] for c in fx["calls"]})
os.environ.setdefault("DISCORD_OFFERS_WEBHOOK_URL", "x"); os.environ.setdefault("DISCORD_WEBHOOK_URL", "x")
out = []
for r in rows:
    for offers in (False, True):
        got = {}
        s.post_discord = lambda payload, daily=False, url=None: got.update(p=payload) or "1"
        s.discord_alert(dict(r), offers=offers)
        p = got["p"]; p["embeds"][0].pop("timestamp", None)
        out.append({"row": r, "offers": offers, "payload": p, "deals": s.for_deals(r), "offs": s.for_offers(r), "cat": s.CAT_BY_ID.get(r["category"])})
phr = ["PS5 is dead", "won't turn on", "if faulty contact us", "no display issues", "Turns on but no display output", "not faulty", "spares or repairs", "please do not turn off", "HDMI port is broken", "account banned", "console is banned", "never been banned", "it's broken", "stopped working"]
dead = {p: s.dead_in({"description": f"<p>{p}</p>"}) for p in phr}
json.dump({"cards": out, "dead": dead}, open("/home/claude/scanner-edge/tools/fixtures2.json", "w"), default=str)
print(len(out), "cards")
