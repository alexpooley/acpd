"""Backend interface.

A backend adapts ACP onto whatever an agent actually speaks. It never sees the
wire: acpd owns the ACP side, calls these methods, and turns whatever the
backend yields into ACP notifications.

Implement six things and any ACP client can drive your agent:

    async def connect()                       -> None
    async def session_new(cwd)                -> {"sessionId": str, "model": str}
    async def session_load(session_id)        -> None
    async def session_list()                  -> [{"sessionId", "title", "cwd", "updatedAt"}]
    async def prompt(session_id, text)        -> stop reason str, after streaming
    async def cancel(session_id)              -> None

Streaming and permissions are pushed back through the callbacks acpd installs:
``on_update(update_dict)`` for session/update payloads, and
``ask_permission(tool_call, options)`` which returns the chosen option id.

Keep policy OUT of a backend. Whether a command is risky, how long a human may
take to answer, what a sensible default is -- that belongs to the agent, which
almost certainly has settings for it already.
"""
