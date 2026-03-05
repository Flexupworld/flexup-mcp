# FlexUp MCP Servers — Railway Deployment

## Structure
- `circuly_mcp_server.py` — Circuly API (subscriptions, customers, orders...)
- `odoo_mcp_server.py`    — Odoo ERP (search, create, update records)
- `requirements.txt`      — Python dependencies
- `Procfile`              — Railway start command

## Deploy on Railway

### Step 1 — GitHub
1. Create a new GitHub repo: `flexup-mcp`
2. Push these files to the repo

### Step 2 — Railway (circuly)
1. Go to railway.app → New Project → Deploy from GitHub repo
2. Select `flexup-mcp`
3. Add environment variables:
   - `CIRCULY_API_KEY=your_key`
   - `CIRCULY_API_SECRET=your_secret`
   - `CIRCULY_API_BASE=https://api.circuly.io/api/2025-01`
4. Railway gives you a URL: `https://flexup-circuly.up.railway.app`

### Step 3 — Railway (odoo) — separate service
1. Same repo, new Railway service
2. Override start command: `uvicorn odoo_mcp_server:app --host 0.0.0.0 --port $PORT`
3. Add environment variables:
   - `ODOO_URL=https://kwan-kwest.odoo.com`
   - `ODOO_DB=kwan-kwest`
   - `ODOO_USERNAME=your_email`
   - `ODOO_API_KEY=your_odoo_api_key`
4. Railway gives you: `https://flexup-odoo.up.railway.app`

### Step 4 — Connect to Claude
1. Go to claude.ai → Settings → Connectors → Add custom connector
2. Add: `https://flexup-circuly.up.railway.app/sse`  → name: "Circuly"
3. Add: `https://flexup-odoo.up.railway.app/sse`     → name: "Odoo"
4. Done — available on ALL devices, ALL projects

## Cost
- Both services on Railway free tier: ~$0-5/month total
- MCP servers are idle 99% of the time → minimal compute usage
