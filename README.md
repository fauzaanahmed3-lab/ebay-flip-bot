# eBay Flip Bot

Scans eBay UK every 10 minutes for underpriced used Buy It Now listings in the categories in `categories.json`, saves finds to Supabase, and alerts Discord. The full strategy and research notes are kept privately (playbook).

## Files
- `categories.json` — what to search, price windows, strong sell prices. Edit prices here at the weekly refresh.
- `scanner.py` — the bot.
- `test_scanner.py` — offline checks of the profit logic (`python test_scanner.py`).
- `schema.sql` — the Supabase table.
- `.github/workflows/scan.yml` — runs the bot every 10 minutes on GitHub for free.

## Setup checklist
1. **eBay keys** — developer.ebay.com → Application Keys → create a **Production** keyset. Copy the *App ID (Client ID)* and *Cert ID (Client Secret)*.
2. **Discord** — create a server → a channel (e.g. `#deals`) → Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL.
3. **Supabase** — create the `flip_listings` table by running `schema.sql`. Copy the project URL and the service-role (secret) key.
4. **GitHub** — create a **public** repo (see cost note below), upload these files, then Settings → Secrets and variables → Actions → add:
   `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `DISCORD_WEBHOOK_URL`.
5. Actions tab → "eBay scan" → **Run workflow** to test. After that it runs by itself.


## Usage budget
- **eBay:** 7 searches per run × 144 runs/day ≈ 1,000 calls/day (free limit: 5,000).
- **GitHub Actions:** GitHub bills each run as at least 1 minute, so 144 runs/day ≈ 4,300 min/month. That's **over** the 2,000 free minutes for private repos, but **free and unlimited for public repos**. Secrets stay hidden in a public repo; only the code and price targets are visible. If you'd rather keep it private, the bot can move to a Supabase scheduled function later at no cost.
