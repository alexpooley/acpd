"""Entry point: python -m acpd

    ACPD_LISTEN=0.0.0.0:9099  python -m acpd          # socket server (the point)
                              python -m acpd --stdio  # one session on stdio
"""

import asyncio
import logging
import os
import sys

from .server import serve_forever, serve_stdio


def _backend_factory(name):
    if name == "hermes":
        from .backends.hermes import HermesBackend
        return HermesBackend
    raise SystemExit("unknown backend: %s" % name)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # stderr, always: on stdio a stray byte on stdout corrupts the protocol.
    logging.basicConfig(level=os.environ.get("ACPD_LOG", "INFO"),
                        format="[acpd] %(message)s", stream=sys.stderr)
    factory = _backend_factory(os.environ.get("ACPD_BACKEND", "hermes"))

    if "--stdio" in argv:
        return asyncio.run(serve_stdio(factory)) or 0

    listen = os.environ.get("ACPD_LISTEN", "0.0.0.0:9099")
    host, _, port = listen.rpartition(":")
    try:
        asyncio.run(serve_forever(factory, host or "0.0.0.0", int(port)))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
