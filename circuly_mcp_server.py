#!/usr/bin/env python3
"""
Circuly MCP Server — Remote HTTP/SSE (Railway)
Compatible with Claude.ai custom connectors (MCP 2024-11-05)
"""

import asyncio
import base64
import json
import os
import uuid
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

CIRCULY_API_BASE   = os.environ.get("CIRCULY_API_BASE", "https://api.circuly.io/api/2025-01").rstrip("/")
CIRCULY_API_KEY    = os.environ.get("CIRCULY_API_KEY", "").strip()
CIRCULY_API_SECRET = os.environ.get("CIRCULY_API_SECRET", "").strip()

app = FastAPI(title="Circuly MCP")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def _token():
    return base64.b64encode(f"{CIRCULY_API_KEY}:{CIRCULY_API_SECRET}".encode()).decode()

async def _get(path: str, params=None):
    url = f"{CIRCULY_API_BASE}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(url, headers={"Accept": "application/json", "Authorization": f"Basic {_token()}"}, params=params or {})
    if r.status_code >= 400:
        raise RuntimeError(f"Circuly {r.status_code}: {r.text[:500]}")
    return r.json()

TOOLS = [
    {"name": "circuly_subscriptions_list", "description": "List Circuly subscriptions.", "inputSchema": {"type": "object", "properties": {"params": {"type": "object", "default": {}}}, "required": []}},
    {"name": "circuly_customers_list", "description": "List Circuly customers.", "inputSchema": {"type": "object", "properties": {"params": {"type": "object", "default": {}}}, "required": []}},
    {"name": "circuly_customer_get", "description": "Get a customer by ID.", "inputSchema": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]}},
    {"name": "circuly_orders_list", "description": "List Circuly orders.", "inputSchema": {"type": "object", "properties": {"params": {"type": "object", "default": {}}}, "required": []}},
    {"name": "circuly_order_get", "description": "Get an order by ID.", "inputSchema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}},
    {"name": "circuly_transactions_list", "description": "List Circuly transactions.", "inputSchema": {"type": "object", "properties": {"params": {"type": "object", "default": {}}}, "required": []}},
    {"name": "circuly_recurring_payments_list", "description": "List recurring payments.", "inputSchema": {"type": "object", "properties": {"params": {"type": "object", "default": {}}}, "required": []}},
    {"name": "circuly_get", "description": "Generic GET to any Circuly endpoint.", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "params": {"type": "object", "default": {}}}, "required": ["path"]}},
]

async def run_tool(name: str, args: dict) -> str:
    try:
        p = args.get("params") or {}
        if name == "circuly_subscriptions_list":        res = await _get("/subscriptions", p)
        elif name == "circuly_customers_list":          res = await _get("/customers", p)
        elif name == "circuly_customer_get":            res = await _get(f"/customers/{args['customer_id']}")
        elif name == "circuly_orders_list":             res = await _get("/orders", p)
        elif name == "circuly_order_get":               res = await _get(f"/orders/{args['order_id']}")
        elif name == "circuly_transactions_list":       res = await _get("/transactions", p)
        elif name == "circuly_recurring_payments_list": res = await _get("/recurring-payments", p)
        elif name == "circuly_get":                     res = await _get(args["path"], p)
        else: return f"Unknown tool: {name}"
        t = json.dumps(res, indent=2, ensure_ascii=False, default=str)
        return t[:20000] + "\n…(truncated)" if len(t) > 20000 else t
    except Exception as e:
        return f"Error: {e}"

async def handle_mcp(msg: dict) -> Optional[dict]:
    method = msg.get("method", "")
    rid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "circuly-mcp", "version": "2.1.0"},
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

_sessions: Dict[str, asyncio.Queue] = {}

@app.get("/sse")
async def sse(request: Request):
    sid = str(uuid.uuid4())
    q: asyncio.Queue = asyncio.Queue()
    _sessions[sid] = q
    base_url = str(request.base_url).rstrip("/")
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
    return {"status": "ok", "service": "circuly-mcp", "version": "2.1.0"}

@app.get("/")
async def root():
    return {"service": "circuly-mcp", "endpoints": ["/sse", "/messages", "/health"]}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
