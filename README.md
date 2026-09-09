# acpd — an ACP server

Serve the [Agent Client Protocol](https://agentclientprotocol.com) over a
socket, backed by an agent that is already running.

```
hermes acp   (spawn an agent per connection)    2600 ms to a ready session
acpd         (socket, warm agent behind it)       60 ms
```

## The problem

ACP is specified as a **subprocess contract**: the client spawns an agent binary
and talks JSON-RPC over its pipes. Every transport the reference library ships
is a spawn — `spawn_agent_process`, `spawn_stdio_connection`, and so on.

That is the right model for an editor opening one project. It is the wrong model
for anything that reconnects often, because each connection pays a cold agent
build. Profiled on Hermes v0.21.1:

```
python interpreter + imports    0.30 s
building the agent              0.86 s   ← SOUL.md, 30 skills, tool registry, provider
adapter boot, session creation  ~1.4 s
                                ──────
                                ~2.6 s   before a single token is generated
```

Meanwhile the same install runs a long-lived server that **already holds agents
warm** — Hermes' `gateway.agent_cache`, 128 entries, one-hour TTL, used by its
desktop app and TUI. The ACP entry point simply does not go through it.

## What acpd does

Speaks ACP on a socket; forwards to the running agent's own API.

```
ACP client  ──stdio──▶  4-line shim  ──TCP──▶  acpd  ──▶  your agent (warm)
```

Clients need no modification. They still spawn something — a few lines of
`socat` is enough — and the ACP conversation is byte-identical. See
[`examples/rabbit-r1`](examples/rabbit-r1), where a rabbit r1 handset drives
Hermes with an unmodified device and an unmodified rabbit agent.

## Quick start

```bash
pip install acpd

# beside your agent, wherever its server is reachable
ACPD_LISTEN=0.0.0.0:9099 python -m acpd
```

Point a client at it with a shim on `PATH`:

```sh
#!/bin/sh
{ printf '%s\n' "$*"; exec cat; } | exec socat - TCP:localhost:9099
```

## Backends

`acpd/backends/hermes.py` is the reference implementation, ~220 lines, most of
it three lookup tables. A backend implements six methods and any ACP client can
drive your agent:

```python
async def connect()                  # open your agent's API
async def session_new(cwd)           # -> {"sessionId", "model"}
async def session_load(session_id)
async def session_list()             # -> [{"sessionId", "title", "cwd", "updatedAt"}]
async def prompt(session_id, text)   # -> stop reason, after streaming
async def cancel(session_id)
```

Streaming and permissions come back through two callbacks acpd installs:
`on_update(update)` and `await ask_permission(tool_call, options)`.

See [`docs/acp-surface.md`](docs/acp-surface.md) for what acpd implements and
[`docs/hermes-mapping.md`](docs/hermes-mapping.md) for a worked mapping,
including a real auth chain and its two non-obvious traps.

## Design rules

These are not stylistic. Each one is a bug that was written and then removed.

**One connection, one session, one backend link.** Never multiplexed onto a
shared connection. Sharing means sharing request ids, cancellations and
permission routing, and every mistake in that class fails *silently* — the
client discards a malformed frame and nothing appears in any log you own.

**No policy.** acpd does not decide what is risky, how long a human may take to
answer, or what a sensible default is. It carries the question and the answer.
An earlier version imposed a 300-second approval timeout; the agent already had
one, at the same value, with a comment explaining the number. Two timers racing
is worse than one, and the second one was ours.

**Fail loudly.** No silent fallback to the slow path. A backend whose API has
moved should produce an error an operator sees, not a service that quietly got
40× slower. `preflight()` checks the methods a backend depends on at startup.

**Claim only what you can honour.** Advertising a capability you cannot serve is
worse than admitting you lack it — a client may withhold work from you, or send
data down a channel you never implemented.

## Status

Working in production on one home server, driving a rabbit r1 against Hermes.
The Hermes backend targets an internal, unversioned RPC surface; `preflight()`
exists because of that. Other backends welcome.

## Licence

MIT.
