#!/usr/bin/env python3
"""
Odoo MCP Server — Remote HTTP/SSE (Railway)
Compatible with Claude.ai custom connectors (MCP 2024-11-05)
"""

import asyncio
import json
import os
import uuid
import xmlrpc.client
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

ODOO_URL      = os.environ.get("ODOO_URL", "https://kwan-kwest.odoo.com")
ODOO_DB       = os.environ.get("ODOO_DB", "kwan-kwest")
ODOO_USERNAME = os.environ.get("ODOO_USERNAME", "")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")
ODOO_API_KEY  = os.environ.get("ODOO_API_KEY", "")

app = FastAPI(title="Odoo MCP")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

executor = ThreadPoolExecutor(max_workers=4)

# ── Odoo helpers ──────────────────────────────────────────────────────────────
def _cred():
    return ODOO_PASSWORD or ODOO_API_KEY

def _connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, _cred(), {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models

def _search(model, domain=None, fields=None, limit=10):
    uid, models = _connect()
    ids = models.execute_kw(ODOO_DB, uid, _cred(), model, "search", [domain or []], {"limit": limit})
    return models.execute_kw(ODOO_DB, uid, _cred(), model, "read", [ids], {"fields": fields or ["id", "name"]})

def _create(model, values):
    uid, models = _connect()
    return models.execute_kw(ODOO_DB, uid, _cred(), model, "create", [values])

def _update(model, record_id, values):
    uid, models = _connect()
    return models.execute_kw(ODOO_DB, uid, _cred(), model, "write", [[record_id], values])

async def run_sync(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, fn, *args)

# ── Tools ─────────────────────────────────────────────────────────────────────
TOOLS = [
    {"name": "odoo_search",
     "description": "Search records in any Odoo model (res.partner, sale.order, account.move, stock.quant, product.product, etc.)",
     "inputSchema": {"type": "object", "properties": {
         "model":  {"type": "string",  "description": "Odoo model e.g. res.partner"},
         "domain": {"type": "array",   "description": "Search domain e.g. [['is_company','=',true]]", "default": []},
         "fields": {"type": "array",   "description": "Fields to return", "default": ["id", "name"]},
         "limit":  {"type": "integer", "description": "Max records", "default": 10},
     }, "required": ["model"]}},
    {"name": "odoo_create",
     "description": "Create a new record in Odoo.",
     "inputSchema": {"type": "object", "properties": {
         "model":  {"type": "string"},
         "values": {"type": "object", "description": "Field values for the new record"},
     }, "required": ["model", "values"]}},
    {"name": "odoo_update",
     "description": "Update an existing record in Odoo.",
     "inputSchema": {"type": "object", "properties": {
         "model":     {"type": "string"},
         "record_id": {"type": "integer"},
         "values":    {"type": "object"},
     }, "required": ["model", "record_id", "values"]}},
]

async def run_tool(name: str, args: dict) -> str:
    try:
        if name == "odoo_search":
            res = await run_sync(_search, args["model"], args.get("domain", []), args.get("fields", ["id", "name"]), args.get("limit", 10))
            return json.dumps(res, indent=2, default=str)
        elif name == "odoo_create":
            rid = await run_sync(_create, args["model"], args["values"])
            return f"Created record with ID: {rid}"
        elif name == "odoo_update":
            res = await run_sync(_update, args["model"], args["record_id"], args["values"])
            return f"Updated: {res}"
        else:
            return f"Unknown tool: {name}"
    except Exception as e:
        return f"Error: {e}"

# ── MCP protocol ──────────────────────────────────────────────────────────────
async def handle_mcp(msg: dict) -> Optional[dict]:
    method = msg.get("method", "")
    rid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "odoo-mcp", "version": "2.1.0"},
        }}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        p = msg.get("params", {})
        text = await run_tool(p.get("name", ""), p.get("arguments", {}) or {})
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}]}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}

# ── SSE ───────────────────────────────────────────────────────────────────────
_sessions: Dict[str, asyncio.Queue] = {}

@app.get("/sse")
async def sse(request: Request):
    sid = str(uuid.uuid4())
    q: asyncio.Queue = asyncio.Queue()
    _sessions[sid] = q
    base_url = str(request.base_url).rstrip("/").replace("http://", "https://")
    endpoint_url = f"{base_url}/messages?sessionId={sid}"

    async def stream():
        yield f"event: endpoint\ndata: {endpoint_url}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _sessions.pop(sid, None)

    return StreamingResponse(stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

@app.post("/messages")
async def messages(request: Request, sessionId: str):
    body = await request.json()
    response = await handle_mcp(body)
    if response is not None and sessionId in _sessions:
        await _sessions[sessionId].put(response)
    return Response(status_code=202)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "odoo-mcp", "version": "2.1.0"}

@app.get("/")
async def root():
    return {"service": "odoo-mcp", "endpoints": ["/sse", "/messages", "/health"]}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
