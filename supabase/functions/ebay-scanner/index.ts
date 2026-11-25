// eBay Flip Bot scanner on Supabase (Edge Function `ebay-scanner`), ported from scanner.py on 25 Nov 2026.
// Why: no GitHub start-up delay (~20-40 s per run), runs every 30 s, does the slow steps in parallel, and waits at most
// 4 s for the AI photo check before posting (a late AI answer edits the card instead).
//
// pg_cron calls it every 30 s. bot_state 'runner' decides who does the live work:
//   'github'   -> the GitHub bot is live; this function only runs in shadow mode if bot_state 'scanner_shadow' = 'on'
//   'supabase' -> this function is live (posts to Discord, writes flip_listings)
// Modes (?mode=): run (default) · shadow (no posts/AI, writes shadow_listings) · dry (no writes at all, returns report) · health
// Decision logic lives in logic.ts (parity-tested against scanner.py). Config: categories.json (same file as the repo).

// deno-lint-ignore-file no-explicit-any
import * as L from "./logic.ts";
import CONFIG_FILE from "../../../categories.json" with { type: "json" };   // the repo's root config (one source of truth)

const SB_URL = Deno.env.get("SUPABASE_URL")!;
const SB_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const GEMINI = () => Deno.env.get("GEMINI_API_KEY") ?? "";
const EBAY = "https://api.ebay.com";
type Any = any;

// ---------- small helpers ----------
const sbH = { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}`, "Content-Type": "application/json" };
async function sb(path: string, init: RequestInit = {}): Promise<Any> {
  const r = await fetch(`${SB_URL}/rest/v1/${path}`, { ...init, headers: { ...sbH, ...(init.headers ?? {}) } });
  if (!r.ok) throw new Error(`db ${path.split("?")[0]} ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const t = await r.text();
  return t ? JSON.parse(t) : null;
}
const rpc = (fn: string, args: Any = {}) => sb(`rpc/${fn}`, { method: "POST", body: JSON.stringify(args) });
const setState = (key: string, value: string) =>
  sb("bot_state", { method: "POST", headers: { Prefer: "resolution=merge-duplicates" }, body: JSON.stringify({ key, value }) });
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const q = (ids: string[]) => ids.map((i) => `"${String(i).replace(/"/g, "")}"`).join(",");
function b64(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

type Run = {
  mode: "run" | "shadow" | "dry";
  C: Any; ctx: L.Ctx; table: string; write: boolean; post: boolean; ai: boolean;
  usage: Map<string, number>; limited: Map<string, number>; exhausted: Set<string>; exhaustedDirty: boolean;
  token: string; hooks: Record<string, string | null>; gone: Set<string>; t0: number; timing: Record<string, number>;
  notable: boolean; savedIds: Set<string>; lockKey: string; lockReleased: boolean; screenLocked?: boolean;
};
const bump = (R: Run, k: string, n = 1) => R.usage.set(k, (R.usage.get(k) ?? 0) + n);
const mark = (R: Run, k: string) => (R.timing[k] = Date.now() - R.t0);

// ---------- eBay ----------
async function ebayGet(R: Run, url: string): Promise<Response> {
  bump(R, "ebay");
  const r = await fetch(url, { headers: { Authorization: `Bearer ${R.token}`, "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB" }, signal: AbortSignal.timeout(20000) });
  if (r.status === 401 && R.post) { await sb("bot_state?key=eq.ebay_app_token", { method: "DELETE" }).catch(() => {}); await requestTokenRefresh(true); }
  return r;
}
async function ebaySearch(R: Run, cat: Any, query: string | null, categoryIds: string | null = null, limit = 50): Promise<Any[]> {
  const filter = ["buyingOptions:{FIXED_PRICE}", "conditions:{NEW|USED}", "itemLocationCountry:GB",
    `price:[${cat.price_min}..${cat.price_max}]`, "priceCurrency:GBP"].join(",");
  const p = new URLSearchParams({ filter, sort: "newlyListed", limit: String(limit) });
  if (query) p.set("q", query);
  if (categoryIds) p.set("category_ids", categoryIds);
  const r = await ebayGet(R, `${EBAY}/buy/browse/v1/item_summary/search?${p}`);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return (await r.json()).itemSummaries ?? [];
}
async function ebayItem(R: Run, itemId: string): Promise<Any | null> {
  const r = await ebayGet(R, `${EBAY}/buy/browse/v1/item/${encodeURIComponent(itemId)}`);
  if (r.status === 404) { R.gone.add(itemId); await r.body?.cancel(); return null; }
  return r.ok ? await r.json() : (await r.body?.cancel(), null);
}
// The eBay app keys stay in GitHub: a tiny GitHub job ('token' mode) renews the token hourly and stores it in bot_state.
async function requestTokenRefresh(force = false) {
  const st = await sb("bot_state?select=value&key=eq.token_refresh_req").catch(() => null);
  const last = Date.parse(st?.[0]?.value ?? "");
  if (!force && Number.isFinite(last) && Date.now() - last < 10 * 60000) return;
  await setState("token_refresh_req", new Date().toISOString());
  await rpc("trigger_bot", { mode: "token" }).catch(() => {});
}

// ---------- AI ----------
async function aiCall(R: Run, models: string[], parts: Any[], maxTokens: number, timeoutMs: number): Promise<[string | null, string]> {
  const key = GEMINI();
  if (!key) return [null, ""];
  const busy: string[] = [];
  for (let pass = 0; pass < 2; pass++) {
    const todo = pass === 0 ? models.filter((m) => !R.exhausted.has(m)) : busy.splice(0);
    if (pass === 1) { if (!todo.length) break; await sleep(1500); }
    for (const model of todo) {
      const gemma = model.startsWith("gemma");
      const body = { contents: [{ parts }], generationConfig: { temperature: 0, maxOutputTokens: maxTokens, ...(gemma ? {} : { responseMimeType: "application/json" }) } };
      let r: Response;
      try {
        r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
          method: "POST", headers: { "x-goog-api-key": key, "Content-Type": "application/json" }, body: JSON.stringify(body), signal: AbortSignal.timeout(timeoutMs),
        });
      } catch { if (pass === 0) busy.push(model); continue; }
      if (r.status === 429) {
        R.limited.set(model, (R.limited.get(model) ?? 0) + 1);
        const t = await r.text();
        if (/PerDay|per day|daily/i.test(t)) { R.exhausted.add(model); R.exhaustedDirty = true; R.ctx.LOG.push(`🤖 ${model}: free daily allowance used up, skipped until 08:00 UK`); }
        continue;
      }
      if (!r.ok) { await r.body?.cancel(); if ((r.status === 503 || r.status === 500) && pass === 0) busy.push(model); continue; }
      bump(R, model);
      try {
        const j = await r.json();
        const text = (j.candidates[0].content.parts as Any[]).filter((p) => !p.thought).map((p) => p.text ?? "").join("");
        return [text.replaceAll("```json", "").replaceAll("```", ""), model];
      } catch { continue; }
    }
  }
  return [null, ""];
}
async function aiCheck(R: Run, cat: Any, row: Any, details: Any): Promise<[string, string, string]> {
  if (!GEMINI() || !R.ai) return ["skipped", "AI check not configured", ""];
  const parts: Any[] = [{ text: L.aiPrompt(cat, row, details) }];
  const img = details?.image?.imageUrl || row.image_url;
  if (img) {
    try {
      const ir = await fetch(img, { signal: AbortSignal.timeout(5000) });
      if (ir.ok) {
        const buf = new Uint8Array(await ir.arrayBuffer());
        if (buf.length < 4_000_000) parts.push({ inline_data: { mime_type: (ir.headers.get("Content-Type") ?? "image/jpeg").split(";")[0], data: b64(buf) } });
      }
    } catch { /* photo optional */ }
  }
  const [text, model] = await aiCall(R, R.C.edge?.ai_models ?? R.C.ai_models ?? [], parts, 160, 12000);
  const got = text !== null ? L.parseAiCheck(text) : null;
  return got ? [got[0], got[1], model] : ["unavailable", "AI check unavailable right now", ""];
}
async function aiAllowanceLeft(R: Run, usedToday: Record<string, number>): Promise<number | null> {
  const limits = Object.entries(R.C.usage_limits ?? {}).filter(([k]) => k !== "ebay") as [string, number][];
  if (!limits.length) return null;
  return limits.reduce((a, [m, lim]) => a + (R.exhausted.has(m) ? 0 : Math.max(0, lim - (usedToday[m] ?? 0) - (R.usage.get(m) ?? 0))), 0);
}
async function aiScreen(R: Run, batch: [Any, Any][]): Promise<Map<string, [string, string, string, string]> | null> {
  if (!GEMINI() || !R.ai || !batch.length) return null;
  const out = new Map<string, [string, string, string, string]>();
  const size = R.C.ai_screen_batch ?? 30;
  for (let k = 0; k < batch.length; k += size) {
    const part = batch.slice(k, k + size);
    const [text, model] = await aiCall(R, R.C.ai_screen_models ?? R.C.ai_models ?? [], [{ text: L.screenPrompt(part) }], 60 * part.length + 200, 20000);
    let answers: Any = null;
    if (text !== null) { try { answers = JSON.parse(text.slice(text.indexOf("["), text.lastIndexOf("]") + 1)); } catch { answers = null; } }
    if (answers === null) return out.size ? out : null;
    for (const a of Array.isArray(answers) ? answers : []) {
      const i = parseInt(a?.i, 10);
      if (!Number.isFinite(i) || !part[i]) continue;
      const [cat, c] = part[i];
      const v = String(a.v ?? "").toLowerCase();
      if (v !== "yes" && v !== "no") continue;
      const names = new Set(cat.models.map((m: Any) => m.name));
      out.set(c.item_id, [v, names.has(a.m) ? a.m : "", String(a.r ?? "").slice(0, 80), model]);
    }
  }
  return out;
}

// ---------- Discord ----------
async function postHook(url: string, payload: Any): Promise<string | null> {
  let r = await fetch(`${url}?wait=true`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (r.status === 429) {
    const wait = Number((await r.json().catch(() => ({}))).retry_after ?? 2);
    await sleep((wait + 0.5) * 1000);
    r = await fetch(`${url}?wait=true`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  }
  if (!r.ok) throw new Error(`Discord HTTP ${r.status}`);
  return (await r.json().catch(() => ({}))).id ?? null;
}
async function editHook(url: string, msgId: string, payload: Any): Promise<boolean> {
  const r = await fetch(`${url}/messages/${msgId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  await r.body?.cancel();
  return r.ok;
}
function aiLine(v: string, why: string): string {
  const tag = ({ yes: "🤖 AI OK", unsure: "🤖 AI UNSURE" } as Record<string, string>)[v] ?? "🤖 AI NOT RUN (read the description yourself)";
  return (v === "yes" || v === "unsure") && why ? `${tag}: ${why}` : tag;
}
async function markCardGone(R: Run, msgId: string | null, postedAt: string | null) {
  let url = R.hooks.deals;
  if (msgId?.startsWith("offers:")) { url = R.hooks.offers; msgId = msgId.split(":", 2)[1]; }
  if (!msgId || !url || !R.post) return;
  const mins = postedAt ? Math.round((Date.now() - Date.parse(postedAt)) / 60000) : null;
  const when = mins !== null && mins < 120 ? `Gone after ${mins} min` : "Gone";
  try {
    const r = await fetch(`${url}/messages/${msgId}`);
    if (!r.ok) return;
    const embeds = (await r.json()).embeds ?? [];
    if (embeds.length) {
      const e = embeds[0];
      e.color = 0x808080;
      if (!String(e.title ?? "").startsWith("❌")) e.title = ("❌ GONE · " + (e.title ?? "")).slice(0, 256);
      e.fields = (e.fields ?? []).filter((f: Any) => f.name !== "❌ Status").concat([{ name: "❌ Status", value: `${when} (sold or ended on eBay)`, inline: false }]);
    }
    await editHook(url, msgId, { content: `❌ ${when}`, embeds, allowed_mentions: { parse: [] } });
  } catch { /* card edit is cosmetic */ }
}

// ---------- the scan ----------
async function scan(R: Run, state: Record<string, string>, usedToday: Record<string, number>) {
  const C = R.C, ctx = R.ctx, LOG = ctx.LOG;
  const now = new Date();
  const ed = C.edge ?? {};
  const fullKey = R.mode === "run" ? "last_full_scan" : "shadow_last_full_scan";
  const full = L.fullDueSince(state[fullKey] ?? null, now, ed.full_scan_every_min ?? C.full_scan_every_min ?? 12);
  // 30-s ticks: the first half of each minute does the per-minute work; the second half only re-runs the category
  // sweep, and only while eBay calls are on pace for the day (fast hours, budget guard).
  const secondHalf = now.getUTCSeconds() >= 30;
  const ukHour = Number(now.toLocaleString("en-GB", { timeZone: "Europe/London", hour: "2-digit", hour12: false }));
  const pac = new Date(now.toLocaleString("en-US", { timeZone: "America/Los_Angeles" }));
  const dayFrac = (pac.getHours() * 60 + pac.getMinutes() + 1) / 1440;
  const ebayToday = usedToday.ebay ?? 0;
  const onPace = ebayToday <= (ed.daily_call_budget ?? 4600) * dayFrac + 150;
  const [h0, h1] = ed.fast_hours_uk ?? [7, 23];
  const fastSweep = (ed.fast_sweep_sec ?? 60) <= 30 && ukHour >= h0 && ukHour < h1 && onPace && ebayToday < (ed.daily_call_budget ?? 4600);
  if (secondHalf && !fastSweep && !full) { LOG.push("half-minute tick skipped (calls budget / quiet hours)"); return { skipped: true }; }
  const minute = now.getUTCMinutes();
  R.notable = full;

  // 1. searches, all in parallel
  const enabled = new Set(C.categories.filter((c: Any) => c.enabled && c.models.length).map((c: Any) => c.id));
  const swept: Record<string, Any[]> = {};
  const sweepJobs = (C.sweeps ?? []).map(async (s: Any) => {
    const members = s.cats.filter((id: string) => enabled.has(id));
    if (!members.length || !(full || secondHalf || minute % (s.every_min ?? 1) === 0)) return;
    try {
      const items = await ebaySearch(R, s, null, s.category_ids, s.limit ?? 200);
      const age = L.newestAgeMin(items, now);
      LOG.push(`🧹 sweep \`${s.id}\`: ${items.length} listings for ${members.join(", ")}` + (age !== null ? ` · newest ${age} min old` : ""));
      for (const id of members) swept[id] = items;
    } catch (e) {
      LOG.push(`⚠️ sweep ${s.id} failed (${(e as Error).message}): falling back to keyword searches`);
    }
  });
  await Promise.all(sweepJobs);
  mark(R, "searched_sweep");
  const cats = C.categories.filter((c: Any) => c.enabled && c.models.length &&
    (full || c.id in swept || (!secondHalf && c.fast && minute % (c.fast_every_min ?? 1) === 0)));
  const kw: [Any, string][] = [];
  for (const cat of cats) if (full || !(cat.id in swept)) for (const qq of (full ? cat.queries : (cat.fast_queries ?? cat.queries))) kw.push([cat, qq]);
  const kwRes = await Promise.all(kw.map(async ([cat, qq]) => {
    try { return await ebaySearch(R, cat, qq); } catch (e) { LOG.push(`⚠️ ${cat.id}: search failed (${(e as Error).message})`); return null; }
  }));
  mark(R, "searched");
  LOG.unshift(`**${full ? "Full" : "Fast"} scan** · ${now.toISOString().slice(11, 19)} UTC · Supabase${R.mode !== "run" ? ` (${R.mode})` : ""}`);

  // 2. triage
  const cands: [Any, Any][] = [];
  for (const cat of cats) {
    const seen = new Set<string>(); const ages: number[] = [];
    const sources: Any[][] = [];
    if (cat.id in swept) sources.push(swept[cat.id]);
    kw.forEach(([c], i) => { if (c === cat && kwRes[i]) sources.push(kwRes[i]!); });
    for (const items of sources) {
      const age = L.newestAgeMin(items, now); if (age !== null) ages.push(age);
      for (const it of items) {
        if (seen.has(it.itemId)) continue;
        seen.add(it.itemId);
        const c = L.triage(ctx, cat, it);
        if (c) cands.push([cat, c]);
      }
    }
    if (ages.length && !(cat.id in swept)) LOG.push(`🕒 \`${cat.id}\` newest listing: ${Math.min(...ages)} min old`);
    const passed = cands.filter(([c]) => c === cat).length;
    const why = [...ctx.WHY.entries()].filter(([k]) => k.startsWith(cat.id + "|")).map(([k, n]) => [n, k.split("|")[1]] as [number, string]).sort((a, b) => b[0] - a[0] || (a[1] < b[1] ? 1 : -1));
    if (full || passed) LOG.push(`\`${cat.id}\` ${seen.size} checked → ${passed} passed` + (why.length ? " · dropped: " + why.map(([n, r]) => `${n} ${r}`).join(", ") : ""));
  }

  // 3. what's new (or cheaper by £5+)
  const candIds = new Set(cands.map(([, c]) => c.item_id));
  const allIds = [...new Set([...candIds, ...ctx.SOFT.map(([, c]) => c.item_id)])];
  const old = new Map<string, number>(), AI_OK = new Map<string, string>(), SCREENED_IN = new Map<string, string>();
  if (R.mode !== "dry") {
    for (let k = 0; k < allIds.length; k += 150) {
      const rows = await sb(`${R.table}?select=item_id,all_in,status,variant,category&item_id=in.(${encodeURIComponent(q(allIds.slice(k, k + 150)))})`) ?? [];
      for (const row of rows) {
        if (row.status === "ai_ok") AI_OK.set(row.item_id, row.variant ?? "");
        else if (row.status === "ai_screened_out") { SCREENED_IN.set(row.item_id, row.category ?? ""); old.set(row.item_id, Number(row.all_in || 0)); }
        else if (row.status !== "retry") old.set(row.item_id, Number(row.all_in || 0));
      }
    }
  }
  mark(R, "seen_checked");
  const fresh: [Any, Any][] = cands.filter(([cat, c]) => !old.has(c.item_id) || c.all_in <= old.get(c.item_id)! - 5 || (SCREENED_IN.get(c.item_id) ?? cat.id) !== cat.id);
  if (cands.length) LOG.push(`${cands.length - fresh.length} already seen before, ${fresh.length} new to look at`);

  // 4. soft listings: AI_OK ones go straight on; the rest go to the AI screen, which runs WHILE we look up the rest
  const rescuedNow: [Any, Any][] = [], todo: [Any, Any][] = [], seenSoft = new Set<string>();
  for (const [cat, c] of ctx.SOFT) {
    if (candIds.has(c.item_id) || seenSoft.has(c.item_id)) continue;
    if (old.has(c.item_id) && (SCREENED_IN.get(c.item_id) ?? cat.id) === cat.id) continue;
    seenSoft.add(c.item_id);
    if (AI_OK.has(c.item_id)) {
      const m = cat.models.find((m: Any) => m.name === AI_OK.get(c.item_id));
      rescuedNow.push([cat, { ...c, lane: m ? "EXACT" : "VAGUE", model: m ? m.name : null, rescued: c.soft_reason }]);
      continue;
    }
    todo.push([cat, c]);
  }
  const screenP = (async () => {
    if (!todo.length || !R.ai) return { rejects: [] as Any[], rescued: [] as [Any, Any][] };
    // the AI screen can be slow (busy Gemini): it has its own lock so it never holds up the next 30-s scan
    if (R.write && (await rpc("try_lock", { p_key: "scanner_screen_lock", p_secs: 90 }).catch(() => false)) !== true) {
      return { rejects: [], rescued: [] };
    }
    R.screenLocked = true;
    const left = await aiAllowanceLeft(R, usedToday);
    if (left !== null && left <= (C.ai_reserve_for_checks ?? 0)) {
      LOG.push(`🤖 AI screen paused: ${left} AI calls left today are kept for photo checks (${todo.length} filtered listing(s) skipped)`);
      return { rejects: [], rescued: [] };
    }
    const cap = C.ai_screen_max ?? 60, waiting = todo.length - cap, batch = todo.slice(0, cap);
    const verdicts = await aiScreen(R, batch);
    if (!verdicts || !verdicts.size) { LOG.push(`🤖 AI screen unavailable: ${batch.length} filtered listing(s) wait for the next run`); return { rejects: [], rescued: [] }; }
    const rejects: Any[] = [], rescued: [Any, Any][] = [], nos: string[] = [];
    for (const [cat, c] of batch) {
      const v = verdicts.get(c.item_id);
      if (!v) continue;
      const [verdict, modelName, reason, aiModel] = v;
      if (verdict === "yes") {
        const model = cat.models.find((m: Any) => m.name === modelName) ?? null;
        if (model) {
          const target = L.catTarget(C, cat, model);
          if (c.all_in > target && !(c.best_offer && c.all_in <= target * (1 + L.offerZone(C, cat)))) {
            nos.push(`${L.short(c)}: genuine ${modelName}, but too pricey`);
            rejects.push({ ...L.stripWorking(c), verdict: "REJECT", status: "rejected", ai_verdict: "yes", ai_reason: "genuine but too pricey", ai_model: aiModel });
            continue;
          }
        }
        rescued.push([cat, { ...c, lane: model ? "EXACT" : "VAGUE", model: model ? model.name : null, rescued: c.soft_reason }]);
        LOG.push(`🤖✅ Rescued ${L.short(c)} (filters said: ${c.soft_reason}; AI: ${reason})`);
        R.notable = true;
      } else {
        nos.push(`${L.short(c)}: ${reason}`);
        rejects.push({ ...L.stripWorking(c), verdict: "REJECT", status: "ai_screened_out", ai_verdict: "no", ai_reason: reason, ai_model: aiModel });
      }
    }
    LOG.push(`🤖 AI screened ${batch.length} filtered listing(s): ${rescued.length} rescued, ${nos.length} confirmed not worth it` + (waiting > 0 ? ` · ${waiting} more next run` : ""));
    for (const n of nos.slice(0, 12)) LOG.push(`   ✖ ${n}`);
    if (nos.length > 12) LOG.push(`   … and ${nos.length - 12} more`);
    return { rejects, rescued };
  })();

  // 5. look up + score: normal candidates now (in parallel), rescued ones once the screen answers
  const maxLookups = C.max_detail_lookups ?? 10;
  let lookups = 0;
  const results: Any[] = [];
  const toPost: { cat: Any; row: Any; details: Any }[] = [];
  const goneNow = () => ({ status: "gone_before_post", outcome: "gone", gone_at: new Date().toISOString() });
  const byLane = (a: [Any, Any], b: [Any, Any]) => (Number(a[1].lane !== "EXACT") - Number(b[1].lane !== "EXACT")) || (a[1].all_in - b[1].all_in);
  async function lookAndScore(group: [Any, Any][]) {
    group.sort(byLane);
    const take = group.slice(0, Math.max(0, maxLookups - lookups));
    for (const [, c] of group.slice(take.length)) {
      LOG.push(`⏭️ ${L.short(c)}: lookup limit reached, next run`);
      if (c.rescued) results.push({ ...L.stripWorking(c), verdict: "REJECT", status: "ai_ok", variant: c.model });
    }
    lookups += take.length;
    const det = await Promise.all(take.map(([, c]) => ebayItem(R, c.item_id).catch(() => null)));
    take.forEach(([cat, c], i) => {
      const details = det[i];
      if (!details) {
        if (R.gone.has(c.item_id)) { LOG.push(`🫥 ${L.short(c)}: sold or ended before we saw it, not posted`); results.push({ ...L.stripWorking(c), verdict: "REJECT", ...goneNow() }); }
        else LOG.push(`❔ ${L.short(c)}: listing details unavailable`);
        return;
      }
      if (L.ended(details)) {
        const row = L.score(ctx, cat, c, details);
        LOG.push(`🫥 ${L.short(row ?? c)}: sold or ended before we saw it, not posted` + (row && row.verdict !== "REJECT" ? ` (would have been a ${row.verdict})` : ""));
        results.push({ ...(row ?? { ...L.stripWorking(c), verdict: "REJECT" }), ...goneNow() });
        return;
      }
      const row = L.score(ctx, cat, c, details);
      if (row && c.rescued) row.notes = `🤖 AI found it (filters said: ${c.rescued})` + (row.notes ? "\n" + row.notes : "");
      if (!row) {
        LOG.push(`❌ ${L.short(c)}: rejected after reading the listing (` + (ctx.REJECT_WHY.get(c.item_id) ?? "wrong spec, damage or price") + ")");
        results.push({ ...L.stripWorking(c), verdict: "REJECT", status: "rejected" });
        return;
      }
      if (L.forDeals(C, row) || L.forOffers(C, row)) toPost.push({ cat, row, details });
      else if (row.verdict === "DEAL") LOG.push(`🔇 ${L.short(row)}: deal but thin margin, saved not posted`);
      else LOG.push(`🟡 ${L.short(row)}: above target (offer zone), saved not posted`);
      results.push(row);
    });
  }
  await lookAndScore(fresh);
  mark(R, "looked_up");
  await postAndCheck(R, toPost.splice(0));               // first wave posts now, before waiting for the AI screen
  // save the first wave and free the scan lock: the next 30-s tick can start while a slow AI screen finishes
  await saveRows(R, dedupe(results).filter((r) => !R.savedIds.has(r.item_id)));
  if (R.mode === "run") {                                // survival checks + tracker inside the lock (never twice at once)
    await Promise.all([survivalChecks(R).catch((e) => LOG.push(`survival checks skipped: ${e.message}`)),
      full ? trackOutcomes(R).catch((e) => LOG.push(`tracker skipped: ${e.message}`)) : Promise.resolve()]);
  }
  if (R.mode === "run" && R.write) { await rpc("release_lock", { p_key: R.lockKey }).catch(() => {}); R.lockReleased = true; }
  mark(R, "first_wave_done");
  const scr = await screenP;
  if (R.screenLocked) await rpc("release_lock", { p_key: "scanner_screen_lock" }).catch(() => {});
  if (rescuedNow.length || scr.rescued.length) {
    await lookAndScore([...rescuedNow, ...scr.rescued]);
    await postAndCheck(R, toPost.splice(0));
  }
  results.push(...scr.rejects);

  // 6. save everything we looked at (posted rows were saved first, inside postAndCheck)
  const byId = new Map<string, Any>(dedupe(results).map((r) => [r.item_id, r]));
  await saveRows(R, [...byId.values()].filter((r) => !R.savedIds.has(r.item_id)));
  if (!fresh.length && !rescuedNow.length && !scr.rescued.length) LOG.push("Nothing new.");
  return { full, fresh: fresh.length, lookups, results: byId.size };
}

// one row per listing (a listing can turn up in two categories): keep the non-REJECT one
function dedupe(rows: Any[]): Any[] {
  const byId = new Map<string, Any>();
  for (const r of rows) if (!byId.has(r.item_id) || byId.get(r.item_id).verdict === "REJECT") byId.set(r.item_id, r);
  return [...byId.values()];
}
// rows -> flip_listings (same shape as scanner.py save_new)
async function saveRows(R: Run, rows: Any[]): Promise<Any[]> {
  if (!rows.length || !R.write) return rows;
  const keys = new Set<string>(); rows.forEach((r) => Object.keys(r).forEach((k) => keys.add(k)));
  const norm = rows.map((r) => { const o: Any = {}; for (const k of keys) o[k] = r[k] ?? null; o.status = r.status || "new"; return o; });
  const saved = await sb(`${R.table}?on_conflict=item_id`, { method: "POST", headers: { Prefer: "resolution=merge-duplicates,return=representation" }, body: JSON.stringify(norm) });
  for (const r of rows) R.savedIds.add(r.item_id);
  return saved ?? norm;
}

// Start the AI photo/description checks for every card at once, wait at most `ai_wait_ms` (default 4 s) for them,
// then post. A card the AI already rejected is never posted (it goes to #ai-blocked via the database trigger); a card
// whose AI answer is still pending is posted straight away with "AI checking…" and edited when the answer arrives.
async function postAndCheck(R: Run, items: { cat: Any; row: Any; details: Any }[]) {
  if (!items.length) return;
  const C = R.C, LOG = R.ctx.LOG;
  const waitMs = C.edge?.ai_wait_ms ?? 4000;
  const done = new Map<Any, [string, string, string]>();
  const checks = items.map((x) => aiCheck(R, x.cat, x.row, x.details).then((res) => { done.set(x, res); return res; }));
  await Promise.race([Promise.all(checks), sleep(waitMs)]);
  mark(R, "ai_first_wait");
  for (const x of items) { const res = done.get(x); if (res) applyAi(x.row, res); }
  const saved = await saveRows(R, items.map((x) => x.row));
  const savedBy = new Map(saved.map((r: Any) => [r.item_id, r]));
  for (const x of items) if (x.row.status === "ai_rejected") LOG.push(`🤖❌ ${L.short(x.row)}: AI said no (${x.row.ai_reason || "no reason"}), not posted`);
  const rank: Record<string, number> = { HOT: 0, SOLID: 1 };
  const deals = items.filter((x) => L.forDeals(C, x.row)).sort((a, b) => (rank[a.row.rating] ?? 2) - (rank[b.row.rating] ?? 2) || Number(a.row.verdict !== "DEAL") - Number(b.row.verdict !== "DEAL") || Number(b.row.profit || 0) - Number(a.row.profit || 0));
  const offers = items.filter((x) => L.forOffers(C, x.row)).sort((a, b) => Number(b.row.safe_profit || 0) - Number(a.row.safe_profit || 0));
  const posted = new Map<Any, { url: string; msg: string | null; offers: boolean }>();
  for (const [list, isOffer] of [[deals, false], [offers, true]] as [Any[], boolean][]) {
    for (const x of list) {
      const row = { ...(savedBy.get(x.row.item_id) ?? {}), ...x.row };
      if (!done.has(x)) row.notes = "🤖 AI checking…" + (row.notes ? "\n" + row.notes : "");
      const { payload } = L.alertPayload(C, row, isOffer);
      LOG.push((isOffer ? `🟡 ${L.short(row)}: posting to #send-an-offer` + (row.suggested_offer ? ` (offer £${row.suggested_offer})` : "")
        : `✅ ${L.short(row)}: DEAL, posting`) + (done.has(x) ? ` · AI ${row.ai_verdict} (${row.ai_model || "-"})` : " · AI still checking"));
      R.notable = true;
      if (!R.post) continue;
      const url = isOffer ? R.hooks.offers : R.hooks.deals;
      if (!url) { LOG.push(`⚠️ no webhook for ${isOffer ? "#send-an-offer" : "#deals"}`); continue; }
      try {
        const msg = await postHook(url, payload);
        posted.set(x, { url, msg, offers: isOffer });
        await sb(`${R.table}?item_id=eq.${encodeURIComponent(row.item_id)}`, { method: "PATCH", body: JSON.stringify({ posted_at: new Date().toISOString(), discord_msg_id: isOffer ? (msg ? `offers:${msg}` : null) : msg, live_checks: 0 }) });
      } catch (e) {
        LOG.push(`⚠️ Discord alert failed (${(e as Error).message}), will retry next run`);
        await sb(`${R.table}?item_id=eq.${encodeURIComponent(row.item_id)}`, { method: "PATCH", body: JSON.stringify({ status: "retry" }) }).catch(() => {});
      }
    }
  }
  mark(R, "posted");
  const late = items.filter((x) => !done.has(x));
  if (!late.length) return;
  // the slow AI answers: save them and edit the cards (edits never ping)
  await Promise.all(late.map(async (x) => {
    const res = await checks[items.indexOf(x)];
    applyAi(x.row, res);
    const patch: Any = { ai_verdict: x.row.ai_verdict, ai_reason: x.row.ai_reason, ai_model: x.row.ai_model, notes: x.row.notes };
    if (x.row.status === "ai_rejected") patch.status = "ai_rejected";
    if (R.write) await sb(`${R.table}?item_id=eq.${encodeURIComponent(x.row.item_id)}`, { method: "PATCH", body: JSON.stringify(patch) }).catch(() => {});
    LOG.push(x.row.ai_verdict === "no" ? `🤖❌ ${L.short(x.row)}: AI said no after posting (${x.row.ai_reason || "no reason"}), card marked` : `🤖 ${L.short(x.row)}: AI ${x.row.ai_verdict} (${x.row.ai_model || "-"}), card updated`);
    const p = posted.get(x);
    if (!p?.msg) return;
    const { payload } = L.alertPayload(C, x.row, p.offers);
    if (x.row.ai_verdict === "no") {
      const e = payload.embeds[0];
      e.color = 0x808080;
      e.title = ("🚫 AI SAYS SKIP · " + e.title).slice(0, 256);
      e.description = `🚫 **AI: probably not a working ${x.row.variant}** (${x.row.ai_reason || "no reason"}). Check before buying.` + (e.description ? "\n" + e.description : "");
    }
    await editHook(p.url, p.msg, { embeds: payload.embeds, allowed_mentions: { parse: [] } });
  }));
  mark(R, "ai_done");
}
function applyAi(row: Any, [v, why, model]: [string, string, string]) {
  row.ai_verdict = v; row.ai_reason = why; row.ai_model = model;
  if (v === "no") row.status = "ai_rejected";
  else row.notes = aiLine(v, why) + (row.notes ? "\n" + row.notes : "");
}

// ---------- after the scan: survival checks, tracker ----------
async function survivalChecks(R: Run) {
  const now = new Date();
  const rows = await sb(`flip_listings?select=item_id,title,all_in,posted_at,live_checks,discord_msg_id&posted_at=gte.${new Date(now.getTime() - 25 * 60000).toISOString()}&gone_at=is.null&live_checks=lt.${L.SURVIVAL_MIN.length}&limit=20`) ?? [];
  await Promise.all(rows.map(async (row: Any) => {
    const posted = new Date(row.posted_at), done = Number(row.live_checks || 0);
    if (!L.survivalDue(posted, done, now)) return;
    const r = await ebayGet(R, `${EBAY}/buy/browse/v1/item/${encodeURIComponent(row.item_id)}`);
    const mins = Math.round((now.getTime() - posted.getTime()) / 6000) / 10;
    let patch: Any;
    const j = r.ok ? await r.json() : (await r.body?.cancel(), null);
    if (r.status === 404 || (j && (j.estimatedAvailabilities ?? []).some((a: Any) => a.estimatedAvailabilityStatus === "OUT_OF_STOCK"))) {
      patch = { gone_at: now.toISOString(), outcome: "gone", live_checks: done + 1, last_checked: now.toISOString() };
      R.ctx.LOG.push(`⏱️ ${L.short(row)}: gone ${mins} min after posting`);
      await markCardGone(R, row.discord_msg_id, row.posted_at);
    } else if (r.ok) {
      patch = { last_live_at: now.toISOString(), live_checks: done + 1, last_checked: now.toISOString() };
      R.ctx.LOG.push(`⏱️ ${L.short(row)}: still for sale ${mins} min after posting`);
    } else return;
    await sb(`flip_listings?item_id=eq.${encodeURIComponent(row.item_id)}`, { method: "PATCH", body: JSON.stringify(patch) });
  }));
}
async function trackOutcomes(R: Run) {
  const n = R.C.track_per_run ?? 5;
  if (!n) return;
  const now = Date.now();
  const rows = await sb(`flip_listings?select=item_id,first_seen,posted_at,discord_msg_id&status=neq.rejected&verdict=in.(DEAL,OFFER)&outcome=is.null&and=(first_seen.gte.${new Date(now - 7 * 86400000).toISOString()},first_seen.lte.${new Date(now - 30 * 60000).toISOString()})&order=last_checked.asc.nullsfirst&limit=${n}`) ?? [];
  if (rows.length) R.ctx.LOG.push(`📦 Tracker: re-checked ${rows.length} earlier alert(s)`);
  const iso = new Date().toISOString();
  await Promise.all(rows.map(async (row: Any) => {
    const r = await ebayGet(R, `${EBAY}/buy/browse/v1/item/${encodeURIComponent(row.item_id)}`);
    const patch: Any = { last_checked: iso };
    if (r.status === 404) {
      Object.assign(patch, { outcome: "gone", gone_at: iso });
      R.ctx.LOG.push(`📦 Tracker: ${row.item_id} has gone (sold or ended)`);
      await markCardGone(R, row.discord_msg_id, row.posted_at);
    } else if (r.ok) {
      const d = await r.json();
      if ((d.estimatedAvailabilities ?? []).some((a: Any) => a.estimatedAvailabilityStatus === "OUT_OF_STOCK")) {
        Object.assign(patch, { outcome: "gone", gone_at: iso });
        R.ctx.LOG.push(`📦 Tracker: ${row.item_id} sold out`);
        await markCardGone(R, row.discord_msg_id, row.posted_at);
      } else {
        patch.current_price = Number(d.price?.value ?? 0) || null;
        if (Date.parse(row.first_seen) < now - (6 * 24 + 23) * 3600000) patch.outcome = "unsold_7d";
      }
    } else { await r.body?.cancel(); return; }
    await sb(`flip_listings?item_id=eq.${encodeURIComponent(row.item_id)}`, { method: "PATCH", body: JSON.stringify(patch) });
  }));
}

// ---------- logs + usage ----------
async function postLogs(R: Run) {
  const url = R.hooks.logs, LOG = R.ctx.LOG;
  if (!url || !LOG.length || !R.post) return;
  const chunks: string[] = []; let cur = "";
  for (let line of LOG) { line = line.slice(0, 300); if (cur.length + line.length + 1 > 1900) { chunks.push(cur); cur = ""; } cur += line + "\n"; }
  chunks.push(cur);
  for (const c of chunks.slice(0, 5)) await postHook(url, { content: c, allowed_mentions: { parse: [] }, flags: 4096 }).catch(() => {});
}
async function reportUsage(R: Run, refreshCard: boolean) {
  const rows = [...new Set([...R.usage.keys(), ...R.limited.keys()])].map((name) => ({ name, calls: R.usage.get(name) ?? 0, limited: R.limited.get(name) ?? 0 }));
  if (rows.length) await rpc("bump_usage", { p_day: L.usageDay(), p_rows: rows });
  if (!refreshCard || !R.hooks.usage || !R.post) return;
  const day = L.usageDay();
  const from = new Date(Date.now() - 6 * 86400000).toLocaleDateString("en-CA", { timeZone: "America/Los_Angeles" });
  const got = await sb(`api_usage?select=day,name,calls,limited&day=gte.${from}`) ?? [];
  const today: Record<string, [number, number]> = {}, week: Record<string, number> = {};
  for (const r of got) { if (r.day === day) today[r.name] = [r.calls, r.limited]; week[r.name] = (week[r.name] ?? 0) + r.calls; }
  const payload = { embeds: [{ title: "📊 API usage today", description: L.renderUsage(R.C, today, week), color: 0x5865F2 }], allowed_mentions: { parse: [] } };
  const st = await sb("bot_state?select=value&key=eq.usage_msg_id");
  const msgId = st?.[0]?.value;
  if (msgId && await editHook(R.hooks.usage, msgId, payload)) return;
  const id = await postHook(R.hooks.usage, { ...payload, flags: 4096 });
  if (id) await setState("usage_msg_id", id);
}

// ---------- entry ----------
Deno.serve(async (req) => {
  const u = new URL(req.url);
  let mode = (u.searchParams.get("mode") ?? "run") as Any;
  const t0 = Date.now();
  // one round trip for everything the run needs to know
  const keys = ["runner", "scanner_shadow", "ebay_app_token", "last_full_scan", "shadow_last_full_scan", "ai_exhausted", "scanner_config"];
  const stRows = await sb(`bot_state?select=key,value&key=in.(${keys.join(",")})`).catch(() => []) ?? [];
  const state: Record<string, string> = Object.fromEntries(stRows.map((r: Any) => [r.key, r.value]));
  if (mode === "run" && state.runner !== "supabase") {
    if (state.scanner_shadow !== "on") return Response.json({ idle: "runner is github" });
    mode = "shadow";
  }
  if (mode === "health") {
    const tok = (() => { try { const s = JSON.parse(state.ebay_app_token); return Math.round((Date.parse(s.exp) - Date.now()) / 60000); } catch { return null; } })();
    const hooks = await rpc("scanner_hooks").catch((e) => ({ error: e.message }));
    return Response.json({ runner: state.runner ?? "github", shadow: state.scanner_shadow ?? "off", token_minutes_left: tok,
      webhooks: Object.fromEntries(Object.entries(hooks ?? {}).map(([k, v]) => [k, typeof v === "string" && v.startsWith("https://discord.com/api/webhooks/") ? "set" : "missing"])),
      gemini: GEMINI() ? "set" : "missing", categories: (CONFIG_FILE as Any).categories.filter((c: Any) => c.enabled).map((c: Any) => c.id) });
  }
  if (mode === "selftest") {                          // posts a test card to #bot-logs and edits it: proves Vault + Discord work
    const hooks = await rpc("scanner_hooks");
    const C0 = CONFIG_FILE as Any;
    const row = { item_id: "TEST", category: "ps5", variant: "PS5 Disc (original)", title: "TEST CARD from the Supabase scanner (not a real listing)",
      url: "https://www.ebay.co.uk", image_url: null, price: 250, shipping: 0, all_in: 250, buy_target: 280, expected_sell: 360, profit: 90,
      safe_profit: 60, required_profit: 50, verdict: "DEAL", suggested_offer: null, lane: "EXACT", rating: "SOLID", seller_feedback: 120,
      seller_pct: 100, delivery: "free", location: "Leeds · LS6", notes: "🤖 AI checking…\n🏷️ BIZ ~£70" };
    const { payload } = L.alertPayload(C0, row, false);
    delete payload.content; payload.allowed_mentions = { parse: [] }; payload.flags = 4096;
    const t1 = Date.now();
    const id = await postHook(hooks.logs, payload);
    const posted_ms = Date.now() - t1;
    row.notes = "🤖 AI OK: test edit worked\n🏷️ BIZ ~£70";
    const ok = id ? await editHook(hooks.logs, id, { embeds: L.alertPayload(C0, row, false).payload.embeds, allowed_mentions: { parse: [] } }) : false;
    return Response.json({ posted: !!id, posted_ms, edited: ok });
  }
  const lockKey = mode === "run" ? "scanner_lock" : "scanner_shadow_lock";
  if (mode !== "dry") {
    const ok = await rpc("try_lock", { p_key: lockKey, p_secs: 55 }).catch(() => false);
    if (ok !== true) return Response.json({ skipped: "previous run still going" });
  }
  const C = structuredClone(CONFIG_FILE as Any);
  try { Object.assign(C, JSON.parse(state.scanner_config ?? "{}")); } catch { /* bad override ignored */ }
  const R: Run = {
    mode, C, ctx: L.newCtx(C), table: mode === "run" ? "flip_listings" : "shadow_listings",
    write: mode !== "dry", post: mode === "run", ai: mode === "run",
    usage: new Map(), limited: new Map(), exhausted: new Set(), exhaustedDirty: false,
    token: "", hooks: {}, gone: new Set(), t0, timing: {}, notable: false, savedIds: new Set(), lockKey, lockReleased: false,
  };
  let out: Any = {};
  try {
    try { const s = JSON.parse(state.ai_exhausted ?? "{}"); if (s.day === L.usageDay()) R.exhausted = new Set(s.models ?? []); } catch { /* */ }
    let tokLeft = -1;
    try { const s = JSON.parse(state.ebay_app_token); R.token = s.t; tokLeft = Date.parse(s.exp) - Date.now(); } catch { /* */ }
    if (mode === "run" && tokLeft < 45 * 60000) requestTokenRefresh().catch(() => {});
    if (!R.token || tokLeft < 60000) { R.ctx.LOG.push("⚠️ eBay token missing or expired: asked GitHub to renew it, skipping this run"); throw new Error("no token"); }
    const [hooks, used] = await Promise.all([
      mode === "run" ? rpc("scanner_hooks") : Promise.resolve({}),
      sb(`api_usage?select=name,calls&day=eq.${L.usageDay()}`),
    ]);
    R.hooks = hooks ?? {};
    const usedToday: Record<string, number> = Object.fromEntries((used ?? []).map((r: Any) => [r.name, r.calls]));
    out = await scan(R, state, usedToday);
    if (!out.skipped && out.full && R.write) await setState(mode === "run" ? "last_full_scan" : "shadow_last_full_scan", new Date().toISOString());
  } catch (e) {
    out.error = (e as Error).message;
    R.ctx.LOG.push(`⚠️ run error: ${(e as Error).message}`);
    R.notable = true;
  } finally {
    mark(R, "done");
    if (mode !== "dry") {
      const tail: Promise<unknown>[] = [reportUsage(R, mode === "run" && new Date().getUTCSeconds() < 30 && new Date().getUTCMinutes() % 2 === 0).catch(() => {})];
      if (mode === "run") {
        tail.push(setState("last_run", new Date().toISOString()).catch(() => {}));
        if (R.notable) { R.ctx.LOG.push(`⏱️ run took ${(R.timing.done / 1000).toFixed(1)} s (searched ${((R.timing.searched ?? 0) / 1000).toFixed(1)} s, posted ${R.timing.posted ? (R.timing.posted / 1000).toFixed(1) + " s" : "-"})`); tail.push(postLogs(R).catch(() => {})); }
      }
      if (R.exhaustedDirty) tail.push(setState("ai_exhausted", JSON.stringify({ day: L.usageDay(), models: [...R.exhausted] })).catch(() => {}));
      await Promise.all(tail);
      if (!R.lockReleased) await rpc("release_lock", { p_key: lockKey }).catch(() => {});
    }
  }
  return Response.json({ mode, ...out, timing: R.timing, ebay_calls: R.usage.get("ebay") ?? 0, log: R.ctx.LOG.slice(0, mode === "dry" ? 200 : 40) });
});
