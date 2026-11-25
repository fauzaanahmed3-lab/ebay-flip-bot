import fs from "node:fs";
import * as L from "../build/logic.js";
const fx = JSON.parse(fs.readFileSync(new URL("./fixtures.json", import.meta.url)));
const { cards, dead } = JSON.parse(fs.readFileSync(new URL("./fixtures2.json", import.meta.url)));
const C = structuredClone(Object.values(fx.configs)[0]);
for (const c of fx.calls) if (!C.categories.find((x) => x.id === c.cat.id)) C.categories.push(c.cat);
let bad = 0;
for (const c of cards) {
  // python prints seller_pct raw (e.g. 100.0); ignore that cosmetic difference
  const { payload } = L.alertPayload(C, c.row, c.offers);
  delete payload.embeds[0].timestamp;
  const a = JSON.stringify(c.payload).replace(/(\d+)\.0%/g, "$1%"), b = JSON.stringify(payload);
  if (a !== b) { bad++; if (bad < 4) console.log("CARD DIFF\n py:", a.slice(0, 900), "\n ts:", b.slice(0, 900)); }
  if (L.forDeals(C, c.row) !== c.deals || L.forOffers(C, c.row) !== c.offs) { bad++; console.log("route diff", c.row.title); }
}
for (const [p, want] of Object.entries(dead)) { const got = L.deadIn({ description: `<p>${p}</p>` }); if ((got ?? null) !== (want ?? null)) { bad++; console.log("dead diff", p, want, got); } }
console.log(`cards/routing/dead parity: ${bad} differences (${cards.length} cards, ${Object.keys(dead).length} phrases)`);
