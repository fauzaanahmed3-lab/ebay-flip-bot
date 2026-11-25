# ebay-scanner (Supabase Edge Function)

The same bot as `scanner.py`, ported to TypeScript so it runs inside Supabase every 30 seconds
(no GitHub start-up delay). `logic.ts` = the decisions (parity-tested against scanner.py with
`tools/edge/record.py` + `parity.mjs`), `index.ts` = searches, lookups, Discord, database.

Deploy: a one-line entry file that imports this index.ts from raw.githubusercontent.com at a pinned commit (the bundler pulls in logic.ts and the root categories.json).
Live switch: `bot_state.runner` = `supabase` (this function) or `github` (scanner.py).
The eBay app keys stay in GitHub: the hourly `token` workflow run renews the token in `bot_state`.
