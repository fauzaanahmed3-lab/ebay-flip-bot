// eBay Flip Bot: the pure decision logic, ported 1:1 from scanner.py (25 Nov 2026).
// No network, no Deno APIs: the Edge Function (index.ts) does all I/O; tests run this file under Node.
// Keep it in step with scanner.py while both exist (parity test: tools/parity.mjs).

// deno-lint-ignore-file no-explicit-any
export type Any = any;

export type Ctx = {
  CONFIG: Any;
  WHY: Map<string, number>;        // "cat|reason" -> count
  SOFT: [Any, Any][];              // (category, candidate) dropped only by word filters
  REJECT_WHY: Map<string, string>; // item_id -> why score() rejected it after reading the listing
  LOG: string[];
};
export function newCtx(CONFIG: Any): Ctx {
  return { CONFIG, WHY: new Map(), SOFT: [], REJECT_WHY: new Map(), LOG: [] };
}
function no(ctx: Ctx, catId: string, reason: string): null {
  const k = `${catId}|${reason}`;
  ctx.WHY.set(k, (ctx.WHY.get(k) ?? 0) + 1);
  return null;
}
function soft(ctx: Ctx, cat: Any, cand: Any, reason: string): null {
  const k = `${cat.id}|${reason} → AI`;
  ctx.WHY.set(k, (ctx.WHY.get(k) ?? 0) + 1);
  ctx.SOFT.push([cat, { ...cand, soft_reason: reason }]);
  return null;
}

// Python helpers
// Python's round(): half-to-even on the (binary) value
function pyRound(x: number, nd = 0): number {
  const f = 10 ** nd, v = x * f, fl = Math.floor(v), diff = v - fl;
  const n = diff > 0.5 ? fl + 1 : diff < 0.5 ? fl : (fl % 2 === 0 ? fl : fl + 1);
  return n / f;
}
export const r2 = (x: number) => pyRound(x, 2);
const fmt0 = (x: number) => String(pyRound(Number(x)));
const gNum = (x: number) => String(Number(x));          // Python "{:g}" for typical feedback %s (96.5 -> 96.5, 100.0 -> 100)
const has = (p: string, t: string) => new RegExp(p).test(t);

// ---------- text helpers ----------
export function norm(text: string | null | undefined): string {
  let t = (text ?? "").toLowerCase().replaceAll("-", " ").replaceAll("_", " ");
  t = t.replaceAll("quietcomfort", "qc");
  t = t.replace(/\b(xm|gen|qc)\s+(\d)/g, "$1$2");
  t = t.replace(/(\d)\s+(gb|tb)\b/g, "$1$2");
  return t.replace(/\s+/g, " ").trim();
}
export function anyMatch(patterns: string[], text: string): string | null {
  for (const p of patterns) if (has(p, text)) return p;
  return null;
}
const NEGATION = /\b(no|not|without|never|nothing|zero)\s+(\w+\s+)?$/;
const escRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
export function junkWord(words: string[], text: string): string | null {
  for (const w of words) {
    const re = new RegExp(escRe(w), "g");
    let m: RegExpExecArray | null;
    while ((m = re.exec(text))) {
      if (!NEGATION.test(text.slice(Math.max(0, m.index - 20), m.index))) return w;
      if (m[0].length === 0) re.lastIndex++;
    }
  }
  return null;
}
const DAMAGE = /(?<!no )(?<!without )(?<!not )\b(crack|broken|faulty|not working|doesn.?t work|drift|burn ?in|dead pixel|water damage|won.?t charge|no sound)/;
export function damageIn(text: string): boolean {
  return DAMAGE.test(norm(text));
}
const DEAD_SRC =
  String.raw`\b(is|it'?s|its|console is|unit is|headset is|deck is|totally|completely|now) (dead|faulty|broken)\b` +
  String.raw`|\bstopped working\b|\bdead (console|unit|ps5|xbox|switch|headset|deck)\b` +
  String.raw`|\b(won'?t|wont|doesn'?t|does not|will not|can'?t|cannot|no longer) (turn|switch|power|boot)(s|ing)? ?(on|up)\b` +
  String.raw`|\b(won'?t|wont|doesn'?t|does not|will not) (start|boot|load)\b|\bno power\b|\bnot powering( on| up)?\b` +
  String.raw`|\bblue light of death\b|\bblod\b|\bno (display|picture|signal|video)( output)?\b(?! (issues?|problems?|faults?))` +
  String.raw`|\bhdmi (port )?(is )?(broken|damaged|faulty|not working)\b` +
  String.raw`|\b(disc|disk) drive (is )?(not working|broken|faulty|doesn'?t work)\b|\bsold as (faulty|spares)\b` +
  String.raw`|\bfor (spares|parts)( (or|and|/) repairs?)?\b|\bspares (or|and|/) repairs?\b|\b(console|account) (is )?banned\b` +
  String.raw`|\bneeds (a )?(new )?(repair|fixing|motherboard|apu|hdmi port)\b`;
const DEAD_CONTEXT_OK = /(\bif\b|in case|should|\bnot\b|\bnever\b|\bno\b|\bisn'?t\b|return|refund|contact)[^.!|]{0,30}$/;
export function stripHtml(h: string | null | undefined): string {
  return (h ?? "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}
export function listingText(details: Any): string {
  const d = details ?? {};
  const parts = [d.conditionDescription || "", stripHtml(d.shortDescription || ""), stripHtml(d.description || "").slice(0, 6000)];
  return parts.filter(Boolean).join(" | ");
}
export function deadIn(details: Any): string | null {
  const text = norm(listingText(details)).replaceAll("’", "'");
  const re = new RegExp(DEAD_SRC, "g");
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (DEAD_CONTEXT_OK.test(text.slice(Math.max(0, m.index - 40), m.index))) { if (!m[0].length) re.lastIndex++; continue; }
    return m[0];
  }
  return null;
}

// ---------- profit rules ----------
const PROFIT_TIERS: [number, number, number][] = [[0, 300, 50], [300, 400, 75], [400, 1e6, 100]];
export function requiredProfit(allIn: number): number {
  for (const [lo, hi, need] of PROFIT_TIERS) if (lo <= allIn && allIn < hi) return need;
  return 100;
}
export function buyerFee(price: number): number {
  const p = Math.max(price, 0);
  return r2(0.10 + 0.07 * Math.min(p, 20) + 0.04 * Math.max(0, Math.min(p, 300) - 20) + 0.02 * Math.max(0, Math.min(p, 4000) - 300));
}
export function netSale(price: number): number {
  return r2(price - buyerFee(price));
}
function _buyTarget(C: Any, strong: number, postOut: number): number {
  const net = netSale(strong) - postOut - C.packaging_cost;
  let best = 0;
  for (const [lo, hi, need] of PROFIT_TIERS) {
    const t = Math.min(net - need, hi - 0.01);
    if (t >= lo) best = Math.max(best, t);
  }
  return r2(best);
}
export function buyTarget(C: Any, strong: number, postOut: number, maxBuy?: number | null): number {
  const cap = maxBuy ?? C.max_buy ?? 1e6;
  return Math.min(_buyTarget(C, strong, postOut), cap);
}
export function catTarget(C: Any, cat: Any, model: Any): number {
  let t = buyTarget(C, model.strong, cat.post_out, cat.max_buy);
  const msp = cat.min_safe_profit;
  if (msp) t = Math.min(t, r2(netSale(model.typical ?? model.strong) - cat.post_out - C.packaging_cost - msp));
  return t;
}
export function offerTarget(C: Any, cat: Any, model: Any): number | null {
  const lane = cat.offer_channel;
  if (!lane) return null;
  const cap = cat.max_buy ?? C.max_buy ?? 1e6;
  return r2(Math.min(cap, netSale(model.typical ?? model.strong) - cat.post_out - C.packaging_cost - lane.min_safe_profit));
}
export function inOfferLane(C: Any, cat: Any, model: Any, allIn: number, bestOffer: boolean): boolean {
  const ot = offerTarget(C, cat, model);
  return !!ot && (allIn <= ot || (bestOffer && allIn <= ot * (1 + C.offer_zone_pct)));
}
export function offerZone(C: Any, cat: Any): number {
  return cat.no_offers ? 0 : C.offer_zone_pct;
}
const NO_PAD = new RegExp(
  String.raw`console only\b(?! (used|been|had|played|ever|a few|once|twice))|\bno (controllers?|pads?)\b|` +
  String.raw`without (a |the |any )?(controllers?|pads?)\b|(controllers?|pads?) (is |are )?not included|` +
  String.raw`(doesn'?t|does not) come with (a |the |any )?(controllers?|pads?)`);
export function modelFor(cat: Any, text: string): Any | null {
  return cat.models.find((m: Any) => has(m.match, text)) ?? null;
}

// ---------- stages 2-4: cheap checks on the search result ----------
export function triage(ctx: Ctx, cat: Any, item: Any): Any | null {
  const C = ctx.CONFIG;
  const title: string = item.title ?? "";
  const t = norm(title);
  if ((item.itemLocation?.country ?? "GB") !== "GB") return no(ctx, cat.id, "not UK");

  const price = Number(item.price.value);
  const shipOpts = item.shippingOptions ?? [];
  const shipKnown = !!(shipOpts.length && "shippingCost" in shipOpts[0]);
  const ship = shipKnown ? Number(shipOpts[0].shippingCost.value ?? 0) || 0 : 0;
  const fee = buyerFee(price);
  const allIn = r2(price + ship + fee);

  const bestTarget = Math.max(...cat.models.map((m: Any) => catTarget(C, cat, m)));
  let gate = bestTarget * (1 + offerZone(C, cat));
  if (cat.offer_channel) gate = Math.max(gate, Math.max(...cat.models.map((m: Any) => offerTarget(C, cat, m) as number)) * (1 + C.offer_zone_pct));
  if (allIn > gate) return no(ctx, cat.id, "too pricey");

  const junk = [...C.junk, ...(cat.phone ? C.phone_rules.junk : [])];
  if (junkWord(junk, t)) return no(ctx, cat.id, "faulty or parts");

  const seller = item.seller ?? {};
  const fb = parseInt(seller.feedbackScore ?? 0, 10) || 0;
  const pct = Number(seller.feedbackPercentage ?? 0) || 0;
  if (fb < C.seller_min_feedback || pct < C.seller_min_pct) return no(ctx, cat.id, "weak seller");
  if (cat.phone && (fb < C.phone_rules.min_feedback || pct < C.phone_rules.min_pct)) return no(ctx, cat.id, "weak seller");

  const bestOffer = (item.buyingOptions ?? []).includes("BEST_OFFER");
  const cand: Any = {
    item_id: item.itemId, category: cat.id, title: title.slice(0, 200),
    url: item.itemWebUrl ?? null, image_url: item.image?.imageUrl ?? null,
    price, shipping: ship, all_in: allIn, fee,
    best_offer: bestOffer, ship_known: shipKnown,
    seller: null, seller_feedback: fb, seller_pct: pct,
    listed_at: item.itemCreationDate ?? null,
    lane: null, model: null, condition: item.condition ?? null,
    item_location: item.itemLocation ?? {},
  };

  if (cat.must_have && !has(cat.must_have, t)) return no(ctx, cat.id, "not this product");
  if (cat.must_not && has(cat.must_not, t) && !has(cat.must_not_unless ?? "(?!)", t)) return no(ctx, cat.id, "older/other model");

  if (junkWord(C.junk_soft ?? [], t) || anyMatch([...cat.wrong, ...(C.junk_patterns ?? [])], t)) return soft(ctx, cat, cand, "junk/wrong item");
  if (cat.require && !cat.require.every((w: string) => t.includes(w))) return soft(ctx, cat, cand, "missing key word");
  if (cat.require_any && !has(cat.require_any, t)) return soft(ctx, cat, cand, "not the main product");
  const hints: string[] | undefined = cat.category_hint;
  const names = (item.categories ?? []).map((c: Any) => c.categoryName ?? "").join(" ").toLowerCase();
  if (hints && names && !hints.some((h) => names.includes(h))) return soft(ctx, cat, cand, "wrong eBay category");

  const model = modelFor(cat, t);
  if (model) {
    const target = catTarget(C, cat, model);
    if (allIn > target && !(bestOffer && allIn <= target * (1 + offerZone(C, cat))) && !inOfferLane(C, cat, model, allIn, bestOffer)) {
      return no(ctx, cat.id, "too pricey");
    }
    return { ...cand, lane: "EXACT", model: model.name };
  }
  if (has(cat.family, t)) return { ...cand, lane: "VAGUE" };
  return soft(ctx, cat, cand, "model not recognised");
}

// ---------- stage 5: confirm with item details, then score ----------
export const DROP = ["best_offer", "model", "condition", "ship_known", "fee", "soft_reason", "rescued", "item_location"];
export function stripWorking(c: Any): Any {
  const o: Any = {};
  for (const [k, v] of Object.entries(c)) if (!DROP.includes(k)) o[k] = v;
  return o;
}
export function tooCheapToBeTrue(cat: Any, model: Any, allIn: number, condition: string): string | null {
  const typical = model.typical ?? model.strong;
  if (allIn < 0.75 * typical) return `£${fmt0(allIn)} is under 75% of the usual £${fmt0(typical)}.`;
  const shop = cat.new_shop_price;
  if (shop && /^(new|brand new|sealed)/.test(condition.toLowerCase()) && allIn < shop) {
    return `Listed as new at £${fmt0(allIn)}, well under the ~£${shop} shop price.`;
  }
  return null;
}
const pyTitle = (s: string) => s.toLowerCase().replace(/(^|[^a-z])([a-z])/g, (_m, a, b) => a + b.toUpperCase());
export function deliveryAndLocation(cand: Any, details: Any | null): [string, string | null] {
  const d = details ?? {};
  let shipKnown = cand.ship_known ?? true, ship = Number(cand.shipping ?? 0) || 0, extra = false;
  if (!shipKnown) {
    const costs = (d.shippingOptions ?? []).map((o: Any) => o.shippingCost).filter(Boolean);
    if (costs.length) { shipKnown = true; ship = Number(costs[0].value ?? 0) || 0; extra = true; }
  }
  const delivery = !shipKnown ? "collection" : ship === 0 ? "free" : `£${ship.toFixed(2)}` + (extra ? " extra" : "");
  const loc = (d.itemLocation && Object.keys(d.itemLocation).length ? d.itemLocation : null) ?? cand.item_location ?? {};
  const town = pyTitle((loc.city ?? "").trim());
  let pc = (loc.postalCode ?? "").trim().replace(/\*+$/, "").toUpperCase();
  pc = pc ? pc.split(/\s+/)[0] : "";
  const where = [town, pc].filter(Boolean).join(" · ") || null;
  return [delivery, where];
}
export function deliveryBadge(row: Any): string {
  const dl = row.delivery, loc = row.location;
  const where = loc ? `📍 ${loc}` : "";
  if (dl === "collection") return `⚠️ **COLLECTION ONLY** (no postage on the listing)${where ? " · " + where : ""}`;
  if (dl) {
    const head = dl === "free" ? "🚚 **FREE P&P**" : dl.endsWith(" extra") ? `🚚 **P&P ${dl.slice(0, -6)}** (add it to the price)`
      : `🚚 **P&P ${dl}** (in the all-in price)`;
    return head + (where ? ` · ${where}` : "");
  }
  return where;
}

export function score(ctx: Ctx, cat: Any, cand0: Any, details: Any | null): Any | null {
  const C = ctx.CONFIG;
  let cand = cand0;
  const notes: string[] = [];
  let aspects = "";
  let photos: number | null = null;
  if (details) {
    aspects = norm((details.localizedAspects ?? []).map((a: Any) => `${a.name} ${a.value}`).join(" "));
    photos = 1 + (details.additionalImages ?? []).length;
    if (anyMatch(cat.wrong_specs ?? [], aspects) || damageIn(details.conditionDescription ?? "")) return null;
    const dead = deadIn(details);
    if (dead) { ctx.REJECT_WHY.set(cand.item_id, `description says "${dead}"`); return null; }
    if (cat.needs_controller) {
      const blob = norm([details.conditionDescription || "", stripHtml(details.shortDescription || ""), stripHtml(details.description || "").slice(0, 4000)].join(" "));
      if (NO_PAD.test(blob)) return null;
    }
  }
  if (details && details.seller?.sellerAccountType === "BUSINESS" && cand.fee) {
    cand = { ...cand, all_in: r2(cand.all_in - cand.fee), fee: 0 };
  }
  let lane: string = cand.lane;
  let model: Any = cat.models.find((m: Any) => m.name === cand.model) ?? null;
  if (lane === "VAGUE") {
    model = aspects ? modelFor(cat, aspects) : null;
    if (model) {
      lane = "GEM";
      notes.push("💎 Vague title, but the listing details confirm the model (fewer buyers will spot it).");
    } else if (/\bmodel\b/.test(aspects)) {
      return null;
    } else {
      lane = "UNCONFIRMED";
      model = cat.models.reduce((a: Any, b: Any) => (b.strong < a.strong ? b : a));
      notes.push("❓ Model not confirmed: check the photos before buying.");
    }
  }

  const target = catTarget(C, cat, model);
  const allIn: number = cand.all_in, ship: number = cand.shipping;
  let suggestedOffer: number | null = null, offerLane = false, verdict: string;
  if (lane === "UNCONFIRMED") {
    if (allIn > target - C.unconfirmed_extra_margin) return null;
    verdict = "DEAL";
  } else if (allIn <= target) {
    verdict = "DEAL";
  } else if (cand.best_offer && allIn <= target * (1 + offerZone(C, cat))) {
    verdict = "OFFER";
    const feeRate = cand.fee ? 1 : 0;
    const ot = offerTarget(C, cat, model);
    const aim = ot ? Math.min(target, ot) : target;
    suggestedOffer = Math.trunc(aim - ship - feeRate * buyerFee(aim - ship));
  } else if ((lane === "EXACT" || lane === "GEM") && inOfferLane(C, cat, model, allIn, cand.best_offer)) {
    verdict = "OFFER"; offerLane = true;
    const ot = offerTarget(C, cat, model) as number;
    const feeRate = cand.fee ? 1 : 0;
    const laneOffer = Math.trunc(ot - ship - feeRate * buyerFee(ot - ship));
    suggestedOffer = laneOffer < cand.price ? laneOffer : null;
  } else {
    return null;
  }

  const pay = suggestedOffer ? suggestedOffer + ship + (cand.fee ? buyerFee(suggestedOffer) : 0) : allIn;
  const profit = r2(netSale(model.strong) - cat.post_out - C.packaging_cost - pay);
  const safeProfit = r2(netSale(model.typical ?? model.strong) - cat.post_out - C.packaging_cost - pay);
  let need = Math.max(requiredProfit(pay), cat.min_safe_profit || 0);
  if (offerLane) need = cat.offer_channel.min_safe_profit;

  const tags: string[] = [];
  const fb = cand.seller_feedback, pct = cand.seller_pct;
  const trusted = fb >= C.trusted_min_feedback && pct >= C.trusted_min_pct;
  if (fb < 10) tags.push(`LOW FB (${fb})`);
  else if (pct < C.trusted_min_pct) tags.push(`FB ${gNum(pct)}%`);
  if (photos !== null && photos < 3) tags.push(`${photos} PIC${photos !== 1 ? "S" : ""}`);
  const cond = (cand.condition || "").toLowerCase();
  if (/^(new|open|brand new)/.test(cond)) tags.push("NEW/OPEN BOX");
  if (cat.phone) tags.push("IMEI CHECK");
  if (details === null || details === undefined) tags.push("DETAILS NOT CHECKED");
  const tooCheap = tooCheapToBeTrue(cat, model, allIn, cond);
  if (tooCheap) notes.push(`⚠️ Too cheap to be true? ${tooCheap} Check for a scam or a hijacked account before paying.`);
  if (model.note) notes.push(model.note);
  if (offerLane && suggestedOffer) notes.push(`Offer £${suggestedOffer} → ~£${need} profit at an average price.`);
  else if (offerLane) notes.push(`~£${need}+ profit at an average price as listed; a lower offer is a bonus.`);

  const strongGross = model.strong;
  const businessProfit = r2(profit + (strongGross - netSale(strongGross)) - (strongGross * (C.business_fee_pct ?? 0.075) + (C.business_fee_fixed ?? 0.30)));
  tags.push(`BIZ ~£${fmt0(businessProfit)}`);

  const fastSeller = (cat.weekly_sales || 0) >= 10;
  let rating: string;
  if ((lane === "EXACT" || lane === "GEM") && trusted && verdict === "DEAL" && (cand.ship_known ?? true)
      && profit >= need * C.hot_ratio && safeProfit >= need) {
    rating = "HOT";
    notes.unshift("Profitable even at an average price · trusted seller" + (fastSeller ? " · fast seller." : "."));
  } else if (lane === "UNCONFIRMED" || !trusted || safeProfit < need / 2) {
    rating = "CHECK";
    if (safeProfit < need / 2 && trusted && lane !== "UNCONFIRMED") tags.push(`THIN MARGIN (~£${fmt0(safeProfit)} avg)`);
  } else {
    rating = "SOLID";
  }
  if (tooCheap && rating === "HOT") rating = "CHECK";
  if (tags.length) notes.push("🏷️ " + tags.join(" · "));

  const [delivery, location] = deliveryAndLocation(cand, details);
  return {
    ...stripWorking(cand),
    safe_profit: safeProfit, variant: model.name, lane, verdict, rating,
    buy_target: target, expected_sell: model.strong, profit,
    required_profit: need, suggested_offer: suggestedOffer, delivery, location,
    notes: notes.join("\n") || null, flags: null,
  };
}

export function ended(details: Any, now = new Date()): boolean {
  if ((details.estimatedAvailabilities ?? []).some((a: Any) => a.estimatedAvailabilityStatus === "OUT_OF_STOCK")) return true;
  const end = details.itemEndDate;
  if (end) { const t = Date.parse(end); return Number.isFinite(t) ? t <= now.getTime() : false; }
  return false;
}

// ---------- routing ----------
export function quiet(row: Any): boolean {
  const need = Number(row.required_profit || 50);
  return row.rating === "CHECK" && Number(row.safe_profit || 0) < need / 2;
}
export function forOffers(C: Any, row: Any): boolean {
  const cat = catById(C, row.category);
  const lane = cat.offer_channel;
  if (!lane || !["DEAL", "OFFER"].includes(row.verdict) || ["ai_rejected", "gone_before_post"].includes(row.status)) return false;
  if (row.verdict === "DEAL" && !cat.offers_only) return false;
  return Number(row.safe_profit || 0) >= lane.min_safe_profit;
}
export function forDeals(C: Any, row: Any): boolean {
  return row.verdict === "DEAL" && !quiet(row) && !["ai_rejected", "gone_before_post"].includes(row.status) && !catById(C, row.category).offers_only;
}
// 3rd lane (#small-flips): weaker cards that only pass because of the 25 Nov eased margins. Never HOT.
export function isSmall(C: Any, row: Any): boolean {
  const lane = C.small_lane;
  if (!lane || row.rating === "HOT" || !["DEAL", "OFFER"].includes(row.verdict)) return false;
  const floor = (lane.below_safe_profit ?? {})[row.category];
  if (row.verdict === "DEAL" && floor !== undefined && Number(row.safe_profit || 0) < floor) return true;
  const t = Number(row.buy_target || 0), allIn = Number(row.all_in ?? (Number(row.price || 0) + Number(row.shipping || 0)));
  return row.verdict === "OFFER" && t > 0 && allIn > t * (1 + (lane.offer_zone_pct ?? 0.12));
}
export function catById(C: Any, id: string): Any {
  return C.categories.find((c: Any) => c.id === id) ?? {};
}

// ---------- AI ----------
export const AI_PROMPT = `You check eBay UK listings for a reseller. Product wanted: {product} ({model}).
Decide if this listing is a COMPLETE, WORKING {model} that can be resold as one.
Answer "no" if: it is an accessory, box, game, part, code or a different product/model; it is faulty, broken, for parts, untested, locked or banned; or the photo clearly shows a different item or serious damage.
READ THE WHOLE DESCRIPTION: sellers often hide the fault there while the title looks normal. If the seller notes or description say it is dead, won't turn on / no power, has a fault (e.g. no display/HDMI fault, disc drive not working, overheating, blue light, error codes), is for spares or repair, or the account/console is banned or locked, answer "no" with code "faulty", even if the title and photo look fine.
Answer "unsure" if key facts are missing (e.g. stock photo plus vague text, or main parts like controllers/charger missing).
{extra}Otherwise answer "yes".
If (and only if) you answer "no", also give "code", exactly one of: accessory, different_model, faulty, missing_essential_part, not_the_item, other.
Never answer "no" because the price looks low, or because of your own beliefs about model numbers or serial numbers; use "unsure" if that is your only worry.
Reply with JSON only: {"verdict": "yes|no|unsure", "code": "reason code, only for no", "reason": "max 15 words"}

LISTING
Title: {title}
Price: £{price} (all-in £{all_in})
Condition: {condition}
Seller notes: {cond_desc}
Item specifics: {aspects}
Description: {desc}
`;
export const PAD_RULE = `It MUST come with at least one working controller (as standard in the box). Answer "no" if the text or photo shows it comes without one (e.g. "console only", "no controller"). If a controller is neither mentioned nor visible, answer "unsure" with reason "controller not confirmed".
`;
const fill = (tpl: string, v: Record<string, string>) => tpl.replace(/\{(\w+)\}/g, (m, k) => (k in v ? v[k] : m));
export function aiPrompt(cat: Any, row: Any, details: Any): string {
  const d = details ?? {};
  const aspects = (d.localizedAspects ?? []).slice(0, 15).map((a: Any) => `${a.name}: ${a.value}`).join("; ");
  return fill(AI_PROMPT, {
    product: cat.label ?? cat.id, model: row.variant || cat.label, extra: cat.needs_controller ? PAD_RULE : "",
    title: row.title, price: String(row.price), all_in: String(row.all_in),
    condition: d.condition ?? "?", cond_desc: (d.conditionDescription || "-").slice(0, 400),
    aspects: aspects.slice(0, 600) || "-", desc: listingText(d).slice(0, 3000) || "-",
  });
}
const BLOCK_CODES = ["accessory", "different_model", "faulty", "missing_essential_part", "not_the_item"];
export function applyAiRules(verdict: string, code: string | null, reason: string): [string, string] {
  if (verdict === "no" && !BLOCK_CODES.includes((code || "").trim().toLowerCase())) {
    return ["unsure", ("(AI wanted to block for an unlisted reason) " + reason).slice(0, 160)];
  }
  return [verdict, reason];
}
export function parseAiCheck(text: string): [string, string] | null {
  try {
    const out = JSON.parse(text.slice(text.indexOf("{"), text.lastIndexOf("}") + 1));
    let v = String(out.verdict ?? "unsure").toLowerCase();
    v = ["yes", "no", "unsure"].includes(v) ? v : "unsure";
    return applyAiRules(v, String(out.code ?? ""), String(out.reason ?? "").slice(0, 120));
  } catch { return null; }
}
export const SCREEN_PROMPT = `You screen eBay UK listings for a reseller who buys and resells working units.
For each listing, decide if it is selling the PRODUCT for its category.
"yes" = the actual unit is for sale and presumably works. Bundles with extra games, controllers or accessories are fine.
Cosmetic wear, light scratches, no original box or missing manuals are fine.
"no" = only an accessory, game, part, case, strap, controller, charger or box; a different model or product;
faulty, broken, for parts, locked or banned; a "wanted" ad; or clearly not the product.
Also "no" if an essential part is missing (e.g. VR headset without its controllers, console or handheld without
its controller/power lead), because resale prices assume a complete unit.
If "yes", set "m" to the matching model from that product's list when the title makes it clear, otherwise "".

PRODUCTS
{products}

LISTINGS (index | product | all-in price | condition | title)
{listings}

Reply with a JSON array only, one object per listing:
[{"i": 0, "v": "yes|no", "m": "model or empty", "r": "reason, max 8 words"}]
`;
export function screenPrompt(part: [Any, Any][]): string {
  const cats = new Map<string, Any>();
  for (const [cat] of part) cats.set(cat.id, cat);
  const products = [...cats.entries()].map(([cid, c]) => `[${cid}] ${c.ai_hint || c.label || cid} Models: ` + c.models.map((m: Any) => m.name).join("; ")).join("\n");
  const listings = part.map(([cat, c], i) => `${i} | ${cat.id} | £${fmt0(c.all_in)} | ${c.condition || "?"} | ${c.title}`).join("\n");
  return fill(SCREEN_PROMPT, { products, listings });
}

// ---------- Discord cards ----------
const STYLE: Record<string, [string, number]> = {
  HOT: ["🔥 HOT DEAL: buy fast", 0xE74C3C], GEM: ["💎 Hidden gem", 0x9B59B6], DEAL: ["🟢 Deal", 0x2ECC71],
  OFFER: ["🟡 Send an offer", 0xF1C40F], UNCONFIRMED: ["❓ Unconfirmed: check photos", 0x95A5A6],
};
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function priceAge(checked: string | null | undefined, today: Date): [string, boolean] {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(checked ?? ""));
  if (!m) return ["", false];
  const d = Date.UTC(+m[1], +m[2] - 1, +m[3]);
  const t = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  return [`prices checked ${+m[3]} ${MONTHS[+m[2] - 1]}`, (t - d) / 86400000 > 14];
}
export function short(row: Any): string {
  return `${String(row.title ?? "").slice(0, 60)} · £${fmt0(Number(row.all_in || 0))}`;
}
export function alertPayload(C: Any, row: Any, offers = false, now = new Date()): { payload: Any; ping: boolean } {
  const key = offers ? "OFFER" : row.rating === "HOT" ? "HOT" : row.lane === "UNCONFIRMED" ? "UNCONFIRMED" : row.lane === "GEM" ? "GEM" : row.verdict;
  const [label, colour] = STYLE[key];
  let priceLine = `£${Number(row.all_in).toFixed(2)} all-in (target £${fmt0(row.buy_target)})`;
  if (row.suggested_offer) priceLine += `\n**Offer £${row.suggested_offer}** + postage`;
  const fields: Any[] = [
    { name: "💷 Price", value: priceLine, inline: true },
    { name: "💰 Est. profit", value: `~£${fmt0(row.profit)} if it resells ~£${row.expected_sell}\n~£${fmt0(row.safe_profit || 0)} at an average price · need £${row.required_profit}`, inline: true },
    { name: "👤 Seller", value: `${row.seller_feedback} feedback · ${gNum(row.seller_pct)}%`, inline: true },
  ];
  const [checked, stale] = priceAge(catById(C, row.category).prices_checked, now);
  const cardNotes = (row.notes ? String(row.notes).split("\n") : []).concat(stale ? ["⚠️ PRICES 2+ WEEKS OLD: check sold listings"] : []);
  if (cardNotes.length) fields.push({ name: "📝 Notes", value: cardNotes.join("\n").slice(0, 1000), inline: false });
  const embed: Any = {
    author: { name: `${label} · ${row.variant}` },
    title: String(row.title).slice(0, 250), url: row.url, color: colour, fields,
    footer: { text: `${row.category} · ${String(row.lane).toLowerCase()} · rating ${String(row.rating).toLowerCase()}` + (checked ? ` · ${checked}` : "") },
    timestamp: now.toISOString(),
  };
  const badge = deliveryBadge(row);
  if (badge) embed.description = badge;
  if (row.image_url) embed.thumbnail = { url: row.image_url };
  const ping = !offers && (key === "HOT" || (row.verdict === "DEAL" && row.rating === "SOLID"));
  const payload: Any = { embeds: [embed], allowed_mentions: { parse: ping ? ["everyone"] : [] } };
  if (ping) payload.content = key === "HOT" ? "@everyone 🔥 Hot one, act fast" : "@everyone 🟢 Good deal";
  return { payload, ping };
}

// ---------- timing helpers ----------
export function newestAgeMin(items: Any[], now = new Date()): number | null {
  const ts = items.map((it) => Date.parse(String(it.itemCreationDate))).filter((x) => Number.isFinite(x));
  if (!ts.length) return null;
  return Math.round((now.getTime() - Math.max(...ts)) / 6000) / 10;
}
export function fullDueSince(lastIso: string | null, now: Date, everyMin: number): boolean {
  if (!everyMin || !lastIso) return true;
  const t = Date.parse(lastIso);
  if (!Number.isFinite(t)) return true;
  return now.getTime() - t >= everyMin * 60000;
}
export const SURVIVAL_MIN = [1, 2, 3, 5, 10, 20];
export function survivalDue(postedAt: Date, checksDone: number, now: Date): boolean {
  return checksDone < SURVIVAL_MIN.length && now.getTime() >= postedAt.getTime() + SURVIVAL_MIN[checksDone] * 60000;
}

// ---------- usage card ----------
export const USAGE_NAMES: Record<string, string> = {
  "gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite", "gemini-3.1-flash-lite": "Gemini 3.1 Flash-Lite", ebay: "eBay searches + lookups",
};
export function usageDay(now = new Date()): string {
  return now.toLocaleDateString("en-CA", { timeZone: "America/Los_Angeles" });
}
export function bar(used: number, limit: number, width = 12): string {
  const filled = limit ? Math.min(width, Math.round(width * used / limit)) : 0;
  return "▰".repeat(filled) + "▱".repeat(width - filled);
}
export function renderUsage(C: Any, today: Record<string, [number, number]>, week: Record<string, number>, now = new Date()): string {
  // reset = next midnight Pacific
  const pac = new Date(now.toLocaleString("en-US", { timeZone: "America/Los_Angeles" }));
  const msLeft = (24 * 3600 - (pac.getHours() * 3600 + pac.getMinutes() * 60 + pac.getSeconds())) * 1000;
  const reset = new Date(now.getTime() + msLeft);
  const h = Math.floor(msLeft / 3600000), m = Math.floor((msLeft % 3600000) / 60000);
  const ukHM = (d: Date) => d.toLocaleTimeString("en-GB", { timeZone: "Europe/London", hour: "2-digit", minute: "2-digit" });
  const lines = [`Resets ${ukHM(reset)} UK (in ${h}h ${m}m)`, ""];
  let aiLeft = 0;
  const n = (x: number) => x.toLocaleString("en-GB");
  for (const [name, limit] of Object.entries(C.usage_limits ?? {}) as [string, number][]) {
    const [used, hit] = today[name] ?? [0, 0];
    lines.push(USAGE_NAMES[name] ?? name);
    lines.push(`${bar(used, limit)} ${n(used)} / ${n(limit)} · ${n(Math.max(0, limit - used))} left` + (hit ? ` · hit limit ${hit}x` : ""));
    if (name !== "ebay") aiLeft += Math.max(0, limit - used);
    lines.push("");
  }
  lines.push(`AI checks left today: ~${n(aiLeft)}`);
  const g7 = Object.entries(week).filter(([k]) => k !== "ebay").reduce((a, [, v]) => a + v, 0);
  lines.push(`Last 7 days: ${n(g7)} AI checks · ${n(week.ebay ?? 0)} eBay calls`);
  return "```\n" + lines.join("\n") + "\n```" + `\nUpdated ${ukHM(now)} UK · counted by the bot, no extra API calls`;
}
