"""Run the Python test-suite and record every triage()/score() call (inputs + outputs) as parity fixtures."""
import json, sys, hashlib, io, contextlib, os
sys.path.insert(0, "/home/claude/ebay-flip-bot")
os.chdir("/home/claude/ebay-flip-bot")
import scanner as s
REC, CONFIGS = [], {}
def cfg_key():
    j = json.dumps(s.CONFIG, sort_keys=True, default=str)
    k = hashlib.md5(j.encode()).hexdigest()[:10]
    CONFIGS.setdefault(k, json.loads(j))
    return k
_tri, _sco = s.triage, s.score
def triage(cat, item):
    n_soft = len(s.SOFT)
    out = _tri(cat, item)
    soft = s.SOFT[-1][1].get("soft_reason") if len(s.SOFT) > n_soft else None
    REC.append({"fn": "triage", "cfg": cfg_key(), "cat": json.loads(json.dumps(cat, default=str)), "item": json.loads(json.dumps(item, default=str)),
                "out": json.loads(json.dumps(out, default=str)), "soft": soft})
    return out
def score(cat, cand, details):
    out = _sco(cat, cand, details)
    REC.append({"fn": "score", "cfg": cfg_key(), "cat": json.loads(json.dumps(cat, default=str)), "cand": json.loads(json.dumps(cand, default=str)),
                "details": json.loads(json.dumps(details, default=str)), "out": json.loads(json.dumps(out, default=str)),
                "why": s.REJECT_WHY.get((cand or {}).get("item_id"))})
    return out
s.triage, s.score = triage, score
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    try:
        import test_scanner  # noqa
    except SystemExit:
        pass
json.dump({"configs": CONFIGS, "calls": REC}, open("/home/claude/scanner-edge/tools/fixtures.json", "w"))
print(len(REC), "calls recorded,", len(CONFIGS), "configs")
