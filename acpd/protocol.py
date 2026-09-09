"""ACP wire helpers: newline-delimited JSON-RPC 2.0.

Small on purpose. ACP is JSON-RPC over a byte stream; nothing here needs a
library, and hand-rolling it keeps acpd's startup to an interpreter and a
socket, which is the entire point of the project.
"""

import json


def encode(obj) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


def result(rid, payload) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": payload}


def error(rid, message, code=-32000) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": str(message)[:300]}}


def request(rid, method, params) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def notification(method, params) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def prompt_text(params) -> str:
    """ACP sends a prompt as content blocks; most backends want plain text."""
    out = []
    for block in params.get("prompt") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text") or "")
        elif isinstance(block, str):
            out.append(block)
    return "".join(out)
