# Backend walkthrough: Hermes

A worked example of mapping ACP onto an agent that already has a server.

## Finding the surface

Hermes runs `hermes serve` — described in its own `--help` as *"the
JSON-RPC/WebSocket gateway the desktop app and remote clients connect to"*. It
exposes 84 methods. No docs, no OpenAPI, no schema, no protocol version. The
nearest thing to a guarantee is a CI script that watches Python symbols and is
advisory by default.

That sounds alarming and mostly is not: the vendor's own desktop app depends on
this surface, which is real pressure against churn. But it is why `preflight()`
exists.

## The mapping

Close to one-to-one, which is the sign you have found the right surface:

| ACP | Hermes RPC |
|---|---|
| `session/new` | `session.create` |
| `session/list` | `session.list` |
| `session/load` | `session.resume` |
| `session/prompt` | `prompt.submit` |
| `session/cancel` | `session.interrupt` |
| `session/request_permission` | `approval.request` event + `approval.respond` |

## Events

`prompt.submit` returns `{"status": "streaming"}` immediately and the server
pushes frames. One real turn, captured:

```
message.start → thinking.delta → reasoning.delta ×38 → message.delta ×3
              → reasoning.available → message.complete {text, usage}
```

Mapped:

```
message.delta    → agent_message_chunk
reasoning.delta  → agent_thought_chunk
thinking.delta   → agent_thought_chunk
tool.start       → tool_call
tool.complete    → tool_call_update
message.complete → terminal, stopReason "end_turn"
turn.error       → terminal, stopReason "refusal"
```

Frames with no row are dropped because ACP has no representation for them
(`moa.*`, `pet.*`, `voice.*`, `wake.*`) — not because they were judged
uninteresting. If ACP grows a vocabulary for one, add a row.

## The auth chain

Three steps, all required:

```
POST /auth/password-login  {"provider","username","password"}  → session cookie
POST /api/auth/ws-ticket                                       → 30s single-use ticket
WS   /api/ws   subprotocols: ["hermes-gateway-v1",
                              "hermes-gateway-ticket.<ticket>"]
```

Two traps, both of which cost an hour:

**The ticket travels as a WebSocket subprotocol**, not a header or a query
param, because browsers cannot set `Authorization` on an upgrade. A plain
`Authorization: Basic` on the upgrade returns **403**, not 401 — the credentials
are fine, the mechanism is wrong.

**aiohttp's default cookie jar silently drops cookies set for a bare IP host.**
Login returns 200, the cookie vanishes, and the ticket call 401s with
`reason: "no_cookie"`. Use `aiohttp.CookieJar(unsafe=True)`.

## A caveat worth knowing

Sessions created through this backend are tagged with the server's own source
(`tui`) rather than `acp`, because they are created by the server's session API
rather than by Hermes' ACP adapter. They appear normally in `session/list`, but
Hermes' *own* `hermes acp` only restores sessions tagged `acp` — so a session
created via acpd cannot be resumed by falling back to the stock adapter. The
reverse works fine.
