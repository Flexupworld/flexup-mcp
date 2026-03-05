#!/usr/bin/env python3
"""
Odoo MCP Server — Remote/HTTP version (Railway deployment)
Transport: SSE (Server-Sent Events) via FastAPI
Compatible with Odoo 19 / Odoo.com cloud
"""

import asyncio
import json
import os
import uuid
import xmlrpc.client
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ── Config ────────────────────────────────────────────────────────────────────
ODOO_URL      = os.environ.get("ODOO_URL", "https://kwan-kwest.odoo.com")
ODOO_DB       = os.environ.get("ODOO_DB", "kwan-kwest")
ODOO_USERNAME = os.environ.get("ODOO_USERNAME", "")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")
ODOO_API_KEY  = os.environ.get("ODOO_API_KEY", "")

app = FastAPI(title="Odoo MCP Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

executor = ThreadPoolExecutor(max_workers=4)

# ── Odoo helpers (sync, run in thread) ───────────────────────────────────────
def _get_connection():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD or ODOO_API_KEY, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models

def _search_records(model, domain=None, fields=None, limit=10):
    uid, models = _get_connection()
    cred = ODOO_PASSWORD or ODOO_API_KEY
    ids = models.execute_kw(ODOO_DB, uid, cred, model, "search",
                            [domain or []], {"limit": limit})
    return models.execute_kw(ODOO_DB, uid, cred, model, "read",
                             [ids], {"fields": fields or ["id", "name"]})

def _create_record(model, values):
    uid, models = _get_connection()
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD or ODOO_API_KEY,
                             model, "create", [values])

def _update_record(model, record_id, values):
    uid, models = _get_connection()
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD or ODOO_API_KEY,
                             model, "write", [[record_id], values])

async def run_sync(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, fn, *args)

# ── MCP Tools ─────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "odoo_search",
        "description": "Search records in any Odoo model (res.partner, sale.order, account.move, stock.quant, product.product, etc.)",
        "inputSchema": {"type": "object", "properties": {
            "model":  {"type": "string",  "description": "Odoo model e.g. res.partner"},
            "domain": {"type": "array",   "description": "Search domain e.g. [['is_company','=',true]]", "default": []},
            "fields": {"type": "array",   "description": "Fields to return", "default": ["id", "name"]},
            "limit":  {"type": "integer", "description": "Max records", "default": 10},
        }, "required": ["model"]},
    },
    {
        "name": "odoo_create",
        "description": "Create a new record in Odoo.",
        "inputSchema": {"type": "object", "properties": {
            "model":  {"type": "string"},
            "values": {"type": "object", "description": "Field values for the new record"},
        }, "required": ["model", "values"]},
    },
    {
        "name": "odoo_update",
        "description": "Update an existing record in Odoo.",
        "inputSchema": {"type": "object", "properties": {
            "model":     {"type": "string"},
            "record_id": {"type": "integer"},
            "values":    {"type": "object"},
        }, "required": ["model", "record_id", "values"]},
    },
]

async def call_tool(name: str, args: dict) -> str:
    try:
        if name == "odoo_search":
            result = await run_sync(_search_records,
                args["model"], args.get("domain", []),
                args.get("fields", ["id", "name"]), args.get("limit", 10))
            return json.dumps(result, indent=2, default=str)
        elif name == "odoo_create":
            record_id = await run_sync(_create_record, args["model"], args["values"])
            return f"Created record with ID: {record_id}"
        elif name == "odoo_update":
            result = await run_sync(_update_record, args["model"], args["record_id"], args["values"])
            return f"Updated: {result}"
        else:
            return f"Unknown tool: {name}"
    except Exception as e:
        return f"Error: {e}"

# ── MCP message handler ───────────────────────────────────────────────────────
async def handle_mcp(message: dict) -> Optional[dict]:
    method = message.get("method")
    req_id = message.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "odoo-mcp", "version": "2.0.0"},
        }}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        p = message.get("params", {})
        text = await call_tool(p.get("name", ""), p.get("arguments", {}) or {})
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": text}]
        }}

    if method == "notifications/initialized":
        return None

    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}

# ── SSE endpoint ──────────────────────────────────────────────────────────────
_queues: Dict[str, asyncio.Queue] = {}

@app.get("/sse")
async def sse_connect(request: Request):
    session_id = str(uuid.uuid4())
    q: asyncio.Queue = asyncio.Queue()
    _queues[session_id] = q

    endpoint_event = f"data: {json.dumps({'type':'endpoint','uri':f'/messages?session={session_id}'})}\n\n"

    async def event_stream():
        yield endpoint_event
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            _queues.pop(session_id, None)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/messages")
async def post_message(request: Request, session: str):
    body = await request.json()
    response = await handle_mcp(body)
    if response and session in _queues:
        await _queues[session].put(response)
    return JSONResponse({"ok": True})

@app.get("/health")
async def health():
    return {"status": "ok", "service": "odoo-mcp"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
