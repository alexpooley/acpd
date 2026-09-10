# acpd — an ACP server

Serve the [Agent Client Protocol](https://agentclientprotocol.com) over a
socket, backed by an agent that is already running.

```
hermes acp   (agent spawned per connection)    2600 ms to a ready session
acpd         (agent already running, warm)       60 ms
```

## What this is not

ACP over a network transport is **not** new. The protocol supports remote agents
over HTTP and WebSocket, the Rust and Kotlin SDKs ship WebSocket transports, and
several bridges exist already — [acp-ws-bridge](https://github.com/ytthuan/acp-ws-bridge),
`@rebornix/stdio-to-ws`, and others.

Those are **relays**: they carry ACP bytes over a network to an agent that is
still spawned as a subprocess. The transport moves; the cold start does not.

## The problem acpd solves

An ACP agent is normally a process the client starts. Profiled on Hermes v0.21.1:

```
python interpreter + imports    0.30 s
building the agent              0.86 s   ← SOUL.md, 30 skills, tool registry, provider
adapter boot, session creation  ~1.4 s
                                ──────
                                ~2.6 s   before a single token is generated
```

Fine for an editor opening one project. Expensive for anything that reconnects
often — a handset that talks per utterance pays it every time, including just to
open the session list, where no model is involved at all.

Meanwhile the same install already runs a long-lived server holding agents
**warm** — Hermes' `gateway.agent_cache`, 128 entries, one-hour TTL, used by its
own desktop app and TUI. The ACP entry point does not go through it.

## What acpd does

**Terminates** ACP rather than relaying it. It implements the agent side and maps
each call onto the running agent's own API, so there is no agent subprocess
anywhere and nothing to cold-start.

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

## Tuning a client without touching the agent

Two knobs, both scoped to ACP clients so the agent's other surfaces are
unaffected:

```bash
ACPD_REASONING_EFFORT=none                    # per-session override
ACPD_PROMPT_PREFIX_FILE=/path/to/context.txt  # prepended to every prompt
```

`ACPD_PROMPT_PREFIX` is deliberately **prompt-level, not system-prompt-level**.
Editing an agent's system prompt changes its cached prefix, and every surface
then pays a full cold prefill — measured on one setup at ~32 s against ~2 s
warm. Prompt text does not touch that cache, so it can be changed as often as
you like at no cost, and acpd re-reads the file each turn.

Worked example — a voice client that speaks only once a turn completes, so
time-to-last-token *is* time-to-audio:

| | before | after |
|---|---|---|
| "What is 2+2?" | 5.2 s, 22 reasoning chunks | **1–3 s**, `4.` |
| "capital of France?" | `The capital of France is **Paris**.` | `Paris.` |

Reasoning off removed several seconds of silence; the prefix removed the
markdown that text-to-speech reads aloud as punctuation.

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

## Prior art

- [acp-ws-bridge](https://github.com/ytthuan/acp-ws-bridge) — WebSocket↔stdio relay to Copilot CLI
- [hermes-acp-bridge](https://github.com/memoverflow/hermes-acp-bridge) — the inverse direction: Hermes as the *client*, Claude Code and Codex as agents
- The official [Rust](https://agentclientprotocol.github.io/rust-sdk/) and Kotlin SDKs ship HTTP/WebSocket transports

acpd differs from all of these in one respect: it does not relay to a spawned
agent. That is the whole of the performance claim.

## Status

Working in production on one home server, driving a rabbit r1 against Hermes.
The Hermes backend targets an internal, unversioned RPC surface; `preflight()`
exists because of that. Other backends welcome.

## Licence

MIT.
