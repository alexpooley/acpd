# ACP surface

What acpd implements, and what it deliberately does not. Everything here was
observed on the wire against a real client (a rabbit r1) and a real agent
(Hermes), not read off a spec.

## Client → agent

| method | acpd | notes |
|---|---|---|
| `initialize` | answered locally | static; the backend has no ACP handshake to forward to |
| `session/new` | → backend | `cwd` and `mcpServers` arrive here, not in argv |
| `session/load` | → backend | resume by `sessionId` |
| `session/list` | → backend | this is what fills a client's session picker |
| `session/prompt` | → backend | **synchronous**: one result carrying `stopReason` when the turn ends |
| `session/cancel` | → backend | |

`session/prompt` being synchronous while most agent APIs stream is the one place
acpd holds state: a future per connection, resolved when the backend reports a
terminal frame.

## Agent → client

Notifications, all `session/update`, distinguished by `sessionUpdate`:

```
agent_message_chunk     the answer, streamed
agent_thought_chunk     reasoning / thinking
tool_call               a tool started
tool_call_update        a tool finished
```

One request:

```
session/request_permission   { sessionId, toolCall, options[] }
                             ← { outcome: { outcome: "selected", optionId } }
```

Note the direction: the **agent** asks the **client**. Backends whose agent
works the other way round (emit an event, wait for a respond call) bridge
through `ask_permission`.

## Not implemented

- `session/fork`
- image content blocks (`promptCapabilities.image` is advertised `false`)
- `_meta` passthrough

Absent by choice, not oversight: acpd advertises only what a backend can
actually honour. A client told an agent speaks a capability may withhold work or
send data down a channel that was never implemented.

## Framing

Newline-delimited JSON-RPC 2.0 over a byte stream. No content-length headers, no
library required — `acpd/protocol.py` is 40 lines.
