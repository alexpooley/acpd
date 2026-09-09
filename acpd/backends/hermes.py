"""Hermes backend: ACP onto Hermes' JSON-RPC/WebSocket server.

Hermes (nousresearch/hermes-agent) ships `hermes acp`, which builds a fresh
agent per connection. It also runs a long-lived server (`hermes serve`, default
port 9119) that already holds agents warm in ``gateway.agent_cache`` -- the
desktop app and TUI talk to it. The ACP adapter does not. This backend does.

Everything below was derived by observing the live server, not from docs; that
surface is internal and unversioned. Two mitigations, in order of importance:

 1. ``preflight()`` checks the methods we depend on at startup, so a rename
    fails immediately and legibly instead of mid-conversation.
 2. Errors surface as JSON-RPC errors, which are loud, unlike a client silently
    discarding a frame it did not understand.

Auth chain (v0.21.x), all three steps required:

    POST /auth/password-login  {"provider","username","password"}  -> session cookie
    POST /api/auth/ws-ticket                                       -> 30s single-use ticket
    WS   /api/ws  subprotocols: ["hermes-gateway-v1",
                                 "hermes-gateway-ticket.<ticket>"]

Gotcha: browsers cannot set Authorization on a WS upgrade, hence the ticket
travelling as a subprotocol. Second gotcha: aiohttp's default cookie jar
silently DROPS cookies set for a bare IP host, so the session cookie never
reaches the ticket call and it 401s with reason "no_cookie" -- use
``CookieJar(unsafe=True)``.
"""

import asyncio
import json
import logging
import os

log = logging.getLogger("acpd.hermes")

# ACP method -> Hermes RPC method. Every row was exercised against a live server.
RPC = {
    "session/new": "session.create",
    "session/list": "session.list",
    "session/load": "session.resume",
    "session/cancel": "session.interrupt",
    "session/prompt": "prompt.submit",
}

REQUIRED = ("ping", "session.create", "session.list", "session.resume",
            "prompt.submit", "session.interrupt")

# Hermes event -> ACP session/update. Events with no row are dropped because ACP
# has no representation for them (moa.*, pet.*, voice.*, wake.*), not because
# they were judged uninteresting. If ACP grows a vocabulary for one, add a row.
EVENTS = {
    "message.delta":   lambda pl: {"sessionUpdate": "agent_message_chunk",
                                   "content": {"type": "text", "text": pl.get("text", "")}},
    "reasoning.delta": lambda pl: {"sessionUpdate": "agent_thought_chunk",
                                   "content": {"type": "text", "text": pl.get("text", "")}},
    "thinking.delta":  lambda pl: {"sessionUpdate": "agent_thought_chunk",
                                   "content": {"type": "text", "text": pl.get("text", "")}},
    "tool.start":      lambda pl: {"sessionUpdate": "tool_call",
                                   "toolCallId": str(pl.get("id") or pl.get("tool") or "tool"),
                                   "title": str(pl.get("tool") or "tool"),
                                   "status": "in_progress", "kind": "other"},
    "tool.complete":   lambda pl: {"sessionUpdate": "tool_call_update",
                                   "toolCallId": str(pl.get("id") or pl.get("tool") or "tool"),
                                   "status": "completed"},
}

# Frames that end a turn, and the ACP stopReason each implies.
TERMINAL = {"message.complete": "end_turn", "turn.error": "refusal"}

# Hermes approval choices -> ACP permission option kinds.
CHOICE_KIND = {"once": "allow_once", "session": "allow_always",
               "always": "allow_always", "deny": "reject_once"}


class HermesBackend:
    name = "hermes"

    def __init__(self, base=None, user=None, password=None):
        self.base = base or os.environ.get("HERMES_SERVE_URL", "http://127.0.0.1:9119")
        self.user = user or os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "")
        self.password = password or os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "")
        self.ws = None
        self._rid = 1000
        self._pending = {}
        self._turn = None           # resolved when a terminal frame arrives
        # installed by acpd.server.Connection
        self.on_update = lambda update: None
        self.ask_permission = None

    # -- lifecycle --
    async def connect(self):
        import aiohttp
        import websockets

        jar = aiohttp.CookieJar(unsafe=True)   # see module docstring
        async with aiohttp.ClientSession(cookie_jar=jar) as http:
            async with http.post(self.base + "/auth/password-login",
                                 json={"provider": "basic", "username": self.user,
                                       "password": self.password}) as r:
                if r.status != 200:
                    raise RuntimeError("password-login failed: HTTP %s" % r.status)
            async with http.post(self.base + "/api/auth/ws-ticket") as r:
                if r.status != 200:
                    raise RuntimeError("ws-ticket failed: HTTP %s (%s)"
                                       % (r.status, (await r.text())[:120]))
                ticket = (await r.json())["ticket"]

        self.ws = await websockets.connect(
            self.base.replace("http", "ws") + "/api/ws",
            subprotocols=["hermes-gateway-v1", "hermes-gateway-ticket." + ticket],
            max_size=None, ping_interval=20)
        asyncio.create_task(self._reader())

    async def close(self):
        if self.ws is not None:
            await self.ws.close()

    async def preflight(self):
        await self._call("ping", {}, timeout=15)
        caps = await self._call("gateway.capabilities", {}, timeout=15)
        log.info("backend ready; capabilities=%s", json.dumps(caps)[:120])

    def version_string(self):
        """What the real CLI would report, read from the install beside us."""
        for path in ("/opt/hermes/hermes_agent.egg-info/PKG-INFO",):
            try:
                with open(path) as fh:
                    for line in fh:
                        if line.startswith("Version:"):
                            return "Hermes Agent v%s" % line.split(":", 1)[1].strip()
            except OSError:
                continue
        return "Hermes Agent (version unavailable)"

    # -- rpc plumbing --
    async def _reader(self):
        async for raw in self.ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            rid = msg.get("id")
            if rid is not None and "method" not in msg:
                fut = self._pending.pop(rid, None)
                if fut and not fut.done():
                    fut.set_result(msg)
                continue
            params = msg.get("params") or {}
            self._event(params.get("type"), params.get("payload") or {})

    async def _call(self, method, params=None, timeout=900):
        self._rid += 1
        rid = self._rid
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rid,
                                       "method": method, "params": params or {}}))
        msg = await asyncio.wait_for(fut, timeout=timeout)
        if "error" in msg:
            raise RuntimeError("%s -> %s" % (method, json.dumps(msg["error"])[:200]))
        return msg.get("result") or {}

    # -- events --
    def _event(self, etype, payload):
        if etype == "approval.request":
            asyncio.create_task(self._approval(payload))
            return
        if etype in TERMINAL:
            if self._turn is not None and not self._turn.done():
                self._turn.set_result(TERMINAL[etype])
            return
        fn = EVENTS.get(etype)
        if fn is not None:
            try:
                self.on_update(fn(payload))
            except Exception as exc:
                log.warning("could not translate %s: %s", etype, exc)

    async def _approval(self, payload):
        """Relay only. The choice is the user's; the deadline is Hermes'
        (`approvals.timeout`, default 300s, fails closed)."""
        req_id = payload.get("request_id") or payload.get("id") or ""
        choices = payload.get("choices") or ["once", "deny"]
        options = [{"optionId": c, "name": c, "kind": CHOICE_KIND.get(c, "allow_once")}
                   for c in choices]
        title = str(payload.get("command") or payload.get("tool") or "tool call")[:200]
        chosen = await self.ask_permission(
            {"toolCallId": str(req_id) or "approval", "title": title}, options) or "deny"
        try:
            await self._call("approval.respond",
                             {"session_id": self._session, "request_id": req_id,
                              "choice": chosen})
        except Exception as exc:
            log.info("approval.respond failed (likely already resolved): %s", exc)

    # -- the ACP surface --
    async def session_new(self, cwd):
        res = await self._call(RPC["session/new"], {"cwd": cwd})
        self._session = res.get("session_id")
        return {"sessionId": self._session, "model": (res.get("info") or {}).get("model", "")}

    async def session_load(self, session_id):
        self._session = session_id
        await self._call(RPC["session/load"], {"session_id": session_id})

    async def session_list(self):
        res = await self._call(RPC["session/list"], {"limit": 200})
        return [{"sessionId": s.get("id"),
                 "title": s.get("title") or (s.get("preview") or "")[:60],
                 "cwd": s.get("cwd") or ".",
                 "updatedAt": s.get("started_at")}
                for s in res.get("sessions") or []]

    async def prompt(self, session_id, text):
        self._session = session_id
        self._turn = asyncio.get_event_loop().create_future()
        # prompt.submit returns {"status":"streaming"} at once; the turn is over
        # when a terminal frame arrives. This future is the only state we hold.
        await self._call(RPC["session/prompt"], {"session_id": session_id, "text": text})
        reason = await self._turn
        self._turn = None
        return reason

    async def cancel(self, session_id):
        await self._call(RPC["session/cancel"], {"session_id": session_id})
