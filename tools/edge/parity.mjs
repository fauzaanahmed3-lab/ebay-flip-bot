// Replays every triage()/score() call recorded from the Python tests through logic.ts and compares outputs.
import fs from "node:fs";
import * as L from "../build/logic.js";
const { configs, calls } = JSON.parse(fs.readFileSync(new URL("./fixtures.json", import.meta.url)));
let ok = 0, bad = 0;
const close = (a, b) => typeof a === "number" && typeof b === "number" ? Math.abs(a - b) < 0.011 : JSON.stringify(a) === JSON.stringify(b);
function diff(a, b) {
  if (a === null || b === null || typeof a !== "object") return close(a, b) ? [] : [`${JSON.stringify(a)} != ${JSON.stringify(b)}`];
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  const out = [];
  for (const k of keys) if (!close(a[k] ?? null, b[k] ?? null)) out.push(`${k}: py=${JSON.stringify(a[k])} ts=${JSON.stringify(b[k])}`);
  return out;
}
for (const c of calls) {
  const ctx = L.newCtx(configs[c.cfg]);
  let got, d;
  if (c.fn === "triage") {
    got = L.triage(ctx, c.cat, c.item);
    const soft = ctx.SOFT.length ? ctx.SOFT[ctx.SOFT.length - 1][1].soft_reason : null;
    d = diff(c.out, got);
    if ((soft ?? null) !== (c.soft ?? null)) d.push(`soft: py=${c.soft} ts=${soft}`);
  } else {
    got = L.score(ctx, c.cat, c.cand, c.details);
    d = diff(c.out, got);
    const why = ctx.REJECT_WHY.get(c.cand?.item_id) ?? null;
    if ((c.why ?? null) !== why && c.out === null) d.push(`why: py=${c.why} ts=${why}`);
  }
  if (d.length) { bad++; if (bad <= 12) console.log(`✗ ${c.fn} ${c.cat.id} ${(c.item ?? c.cand)?.title?.slice(0, 50)}\n   ` + d.slice(0, 6).join("\n   ")); }
  else ok++;
}
console.log(`parity: ${ok} same, ${bad} different (of ${calls.length})`);
process.exit(bad ? 1 : 0);
