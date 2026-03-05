#!/usr/bin/env python3
"""
Circuly MCP Server — Remote/HTTP version (Railway deployment)
Transport: SSE (Server-Sent Events) via FastAPI
"""

import asyncio
import base64
import json
import os
import uuid
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ── Config ────────────────────────────────────────────────────────────────────
CIRCULY_API_BASE   = os.environ.get("CIRCULY_API_BASE", "https://api.circuly.io/api/2025-01").rstrip("/")
CIRCULY_API_KEY    = os.environ.get("CIRCULY_API_KEY", "").strip()
CIRCULY_API_SECRET = os.environ.get("CIRCULY_API_SECRET", "").strip()

app = FastAPI(title="Circuly MCP Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Circuly HTTP helpers ──────────────────────────────────────────────────────
def _basic_token() -> str:
    raw = f"{CIRCULY_API_KEY}:{CIRCULY_API_SECRET}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")

def _headers() -> Dict[str, str]:
    return {"Accept": "application/json", "Authorization": f"Basic {_basic_token()}"}

async def _request(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{CIRCULY_API_BASE}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, headers=_headers(), params=params or {})
    if r.status_code >= 400:
        raise RuntimeError(f"Circuly API {r.status_code}: {r.text[:1500]}")
    return r.json()

# ── MCP Tools ─────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "circuly_subscriptions_list",
        "description": "List Circuly subscriptions (GET /subscriptions).",
        "inputSchema": {"type": "object", "properties": {
            "params": {"type": "object", "description": "Optional query params e.g. {status: active, page: 2}"}
        }, "required": []},
    },
    {
        "name": "circuly_customers_list",
        "description": "List Circuly customers (GET /customers).",
        "inputSchema": {"type": "object", "properties": {
            "params": {"type": "object", "description": "Optional query params"}
        }, "required": []},
    },
    {
        "name": "circuly_customer_get",
        "description": "Get a customer by ID.",
        "inputSchema": {"type": "object", "properties": {
            "customer_id": {"type": "string", "description": "Customer ID"}
        }, "required": ["customer_id"]},
    },
    {
        "name": "circuly_orders_list",
        "description": "List Circuly orders (GET /orders).",
        "inputSchema": {"type": "object", "properties": {
            "params": {"type": "object", "description": "Optional query params"}
        }, "required": []},
    },
    {
        "name": "circuly_order_get",
        "description": "Get an order by ID.",
        "inputSchema": {"type": "object", "properties": {
            "order_id": {"type": "string", "description": "Order ID"}
        }, "required": ["order_id"]},
    },
    {
        "name": "circuly_transactions_list",
        "description": "List Circuly transactions (GET /transactions).",
        "inputSchema": {"type": "object", "properties": {
            "params": {"type": "object", "description": "Optional query params"}
        }, "required": []},
    },
    {
        "name": "circuly_recurring_payments_list",
        "description": "List Circuly recurring payments (GET /recurring-payments).",
        "inputSchema": {"type": "object", "properties": {
            "params": {"type": "object", "description": "Optional query params"}
        }, "required": []},
    },
    {
        "name": "circuly_get",
        "description": "Generic GET to any Circuly endpoint.",
        "inputSchema": {"type": "object", "properties": {
            "path":   {"type": "string", "description": "Path e.g. /subscriptions, /orders/123"},
            "params": {"type": "object", "description": "Optional query params"}
        }, "required": ["path"]},
    },
]

async def call_tool(name: str, args: dict) -> str:
    try:
        if name == "circuly_subscriptions_list":
            result = await _request("/subscriptions", args.get("params"))
        elif name == "circuly_customers_list":
            result = await _request("/customers", args.get("params"))
        elif name == "circuly_customer_get":
            result = await _request(f"/customers/{args['customer_id']}")
        elif name == "circuly_orders_list":
            result = await _request("/orders", args.get("params"))
        elif name == "circuly_order_get":
            result = await _request(f"/orders/{args['order_id']}")
        elif name == "circuly_transactions_list":
            result = await _request("/transactions", args.get("params"))
        elif name == "circuly_recurring_payments_list":
            result = await _request("/recurring-payments", args.get("params"))
        elif name == "circuly_get":
            result = await _request(args["path"], args.get("params"))
        else:
            return f"Unknown tool: {name}"

        text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        return text[:20000] + "\n…(truncated)" if len(text) > 20000 else text
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
            "serverInfo": {"name": "circuly-mcp", "version": "2.0.0"},
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
# In-memory queues per session
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
    return {"status": "ok", "service": "circuly-mcp"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
