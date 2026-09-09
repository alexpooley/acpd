# Example: rabbit r1 → acpd

A [rabbit r1](https://www.rabbit.tech) driving Hermes on a home server, with
**no modification to the device or to rabbit's agent**.

## What rabbit does

rabbit's node agent runs on your machine and spawns agents as jobs. For Hermes
it runs, literally:

```
BACKGROUND_JOB_COMMAND=hermes
BACKGROUND_JOB_ARGS=["acp"]
```

…then speaks ACP over that process's stdin/stdout. It is a well-behaved ACP
client. It just insists, as the protocol requires, on *spawning something*.

## The integration

Put [`hermes`](./hermes) on `PATH` where the node agent will find it. That's it.
It relays stdio to acpd; the ACP conversation is byte-identical.

```
rabbit node agent
   └ hermes (this shim)  ──TCP──▶  acpd  ──WS──▶  Hermes gateway (already warm)
```

## Why bother

Measured on the same box, connect to a ready session:

| | |
|---|---|
| `hermes acp` (spawn per connection) | 2600 ms |
| acpd | **60 ms** |

The r1 opens a connection per utterance, so that cost was paid every time —
including opening the sessions list, where no model is involved at all.

## Deployment notes

The shim needs `socat` and network reach to acpd. In this setup both containers
share a docker network, so `hermes:9099` resolves by container name and acpd's
port is **not** published to the host — it exists only on that network.

`docker-compose.snippet.yml` shows the server side: run acpd beside the agent,
supervised, as a non-root user.
