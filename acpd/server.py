"""acpd — an ACP server.

The Agent Client Protocol is specified as a subprocess contract: the client
spawns an agent binary and talks JSON-RPC over its pipes. That is a fine model
for an editor opening one project, and a poor one for a device that reconnects
per utterance -- every connection pays a cold agent build.

acpd serves ACP over a SOCKET instead, backed by an agent process that is
already running and already warm. Measured against Hermes on a home server:

    hermes acp (spawn per connection)   2600 ms to a ready session
    acpd (socket, warm backend)           60 ms

Clients need no modification. They still spawn something -- a few lines of
socat, or any shim that relays stdio to a port -- and the ACP conversation is
byte-identical. See examples/rabbit-r1.

Design rules, learned the hard way:

* One connection, one session, one backend link. NOT multiplexed onto a shared
  connection: sharing means sharing request ids, cancellations and permission
  routing, and every mistake in that class fails silently -- the client simply
  discards a malformed frame and nothing appears in any log you own.
* No policy. acpd does not decide what is risky, how long a human may take to
  answer, or which choice is sensible. It carries the question and the answer.
* Fail loudly. A missing backend method is an error the operator sees, not a
  quiet downgrade to something slower.
"""

import asyncio
import logging
import os

from . import protocol as p

log = logging.getLogger("acpd")


def session_context():
    """Operator-supplied context, seeded ONCE at the start of each session.

    Not a system prompt, and not a per-turn prefix -- both were tried:

    * A system prompt (or an agent "profile" carrying one) changes the agent's
      cached prompt prefix. Every surface of the agent then pays a full cold
      prefill: measured on the reference setup at ~32s against ~2s warm, and two
      prefixes in rotation evict each other.
    * A per-turn prefix avoids that but repeats itself through the conversation
      history, growing every turn and saying the same thing to a model that has
      already read it.

    Seeding it as the first message of a session costs one copy per session and
    changes no cache prefix.

    Read fresh at each session start, so edits apply to the next session without
    restarting acpd. Empty by default: acpd invents no instructions of its own;
    this is where the operator puts theirs.

        ACPD_SESSION_CONTEXT       inline text
        ACPD_SESSION_CONTEXT_FILE  path to a file (wins if both are set)
    """
    path = os.environ.get("ACPD_SESSION_CONTEXT_FILE", "")
    if path:
        try:
            with open(path) as fh:
                return fh.read().strip()
        except OSError as exc:
            log.warning("session context file unreadable (%s); continuing without it", exc)
            return ""
    return os.environ.get("ACPD_SESSION_CONTEXT", "").strip()

# Fallback only. Capabilities are the BACKEND's to declare -- it is the thing
# that either can or cannot honour them, and hardcoding them here would make
# acpd lie on behalf of any backend that differs. Override by setting
# ``capabilities`` on your backend class.
#
# Only claim what you can serve: telling a client you speak a capability you
# cannot honour is worse than admitting you lack it, because the client may
# withhold work or send data down a channel you never implemented.
DEFAULT_CAPABILITIES = {
    "loadSession": False,
    "promptCapabilities": {"image": False},
    "sessionCapabilities": {},
}


class Connection:
    """One ACP client. Owns its backend link and the state of one turn."""

    def __init__(self, backend, reader, writer):
        self.backend = backend
        self.reader = reader
        self.writer = writer
        self.session_id = None
        self._seeded = False        # context goes on the first prompt only
        self._rid = 0
        self._pending = {}          # our requests to the client
        self._loop = asyncio.get_event_loop()
        backend.on_update = self._send_update
        backend.ask_permission = self._ask_permission

    # -- to the client --
    def _write(self, obj):
        self.writer.write(p.encode(obj))

    def _send_update(self, update):
        self._write(p.notification("session/update",
                                   {"sessionId": self.session_id, "update": update}))

    async def _ask_permission(self, tool_call, options):
        """ACP has the AGENT ask the client. Backends whose agent works the
        other way round (an event plus a respond call) bridge through here.

        Deliberately unbounded: the agent owns the deadline. A second timer here
        races the first -- ours firing sends an answer the agent never asked
        for; theirs firing leaves us answering a question that no longer exists.
        """
        self._rid += 1
        rid = "acpd-%d" % self._rid
        fut = self._loop.create_future()
        self._pending[rid] = fut
        self._write(p.request(rid, "session/request_permission",
                              {"sessionId": self.session_id,
                               "toolCall": tool_call, "options": options}))
        res = await fut
        return ((res or {}).get("outcome") or {}).get("optionId")

    # -- from the client --
    async def _handle(self, msg):
        method, params = msg.get("method"), msg.get("params") or {}

        if method == "initialize":
            return {"protocolVersion": 1,
                    "agentInfo": {"name": "acpd", "version": self.backend.name},
                    "agentCapabilities": getattr(self.backend, "capabilities",
                                                 DEFAULT_CAPABILITIES),
                    "authMethods": []}

        if method == "session/new":
            info = await self.backend.session_new(params.get("cwd") or ".")
            self.session_id = info["sessionId"]
            return {"sessionId": self.session_id,
                    "models": {"availableModels": [],
                               "currentModelId": info.get("model", "")},
                    "modes": {"availableModes": [], "currentModeId": "default"}}

        if method == "session/load":
            self.session_id = params.get("sessionId")
            await self.backend.session_load(self.session_id)
            return {}

        if method == "session/list":
            return {"sessions": await self.backend.session_list()}

        if method == "session/prompt":
            self.session_id = params.get("sessionId") or self.session_id
            text = p.prompt_text(params)
            if not self._seeded:
                # Once per session, on the first prompt. Not a system message
                # (a second system role breaks generation on some agents -- it
                # returned an empty answer on Hermes) and not every turn (that
                # repeats itself through the history forever).
                self._seeded = True
                context = session_context()
                if context:
                    text = context + "\n\n" + text
            reason = await self.backend.prompt(self.session_id, text)
            return {"stopReason": reason}

        if method == "session/cancel":
            await self.backend.cancel(params.get("sessionId") or self.session_id)
            return {}

        raise RuntimeError("unsupported ACP method: %s" % method)

    async def run(self):
        while True:
            line = await self.reader.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                msg = __import__("json").loads(line)
            except ValueError:
                log.warning("unparsable line from client; ignoring")
                continue

            rid = msg.get("id")
            if rid is not None and "method" not in msg:      # a reply to us
                fut = self._pending.pop(rid, None)
                if fut and not fut.done():
                    fut.set_result(msg.get("result") or {})
                continue
            try:
                out = await self._handle(msg)
                if rid is not None:
                    self._write(p.result(rid, out))
            except Exception as exc:
                log.error("%s failed: %s", msg.get("method"), exc)
                if rid is not None:
                    self._write(p.error(rid, exc))


async def _serve(backend_factory, reader, writer):
    peer = writer.get_extra_info("peername")
    log.info("connection from %s", peer)
    try:
        # A client may probe the binary before opening a session (rabbit sends
        # `--version`). The shim forwards argv verbatim as the first line so the
        # answer comes from here, beside the real agent, rather than being
        # invented by the shim where it could drift.
        first = (await reader.readline()).decode("utf-8", "replace").strip()
        backend = backend_factory()
        if first in ("--version", "-v", "version"):
            writer.write((backend.version_string() + "\n").encode())
            await writer.drain()
            return
        if first not in ("acp", ""):
            log.warning("refusing unsupported invocation: %r", first[:60])
            writer.write(("acpd: unsupported arguments: %s\n" % first[:60]).encode())
            await writer.drain()
            return
        await backend.connect()
        await Connection(backend, reader, writer).run()
    except Exception as exc:
        log.error("connection from %s failed: %s: %s", peer, type(exc).__name__, exc)
    finally:
        try:
            writer.close()
        except Exception:
            pass
        log.info("connection from %s closed", peer)


async def serve_forever(backend_factory, host="0.0.0.0", port=9099):
    """Accept ACP over TCP. One Connection and one backend link per client."""
    # Preflight once so a moved backend surface is loud at startup rather than
    # on whichever connection happens to arrive first.
    probe = backend_factory()
    await probe.connect()
    await probe.preflight()
    await probe.close()

    server = await asyncio.start_server(
        lambda r, w: _serve(backend_factory, r, w), host, port)
    log.info("listening on %s:%s (backend: %s)", host, port, probe.name)
    async with server:
        await server.serve_forever()


async def serve_stdio(backend_factory):
    """Serve one ACP session on stdin/stdout -- a drop-in for the spawn model.

    Useful for testing, and for clients that cannot be pointed at a socket.
    Startup is still fast because the backend is a network client, not an agent.
    """
    import sys
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    class _StdoutWriter:
        def write(self, data):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

        async def drain(self):
            pass

        def close(self):
            pass

        def get_extra_info(self, _):
            return "stdio"

    backend = backend_factory()
    await backend.connect()
    await Connection(backend, reader, _StdoutWriter()).run()
