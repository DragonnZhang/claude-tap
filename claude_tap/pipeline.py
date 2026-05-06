"""Transport-agnostic proxy primitives.

Both the reverse-mode (aiohttp) and forward-mode (raw asyncio + TLS) servers
build the same record shape from the same header / body / streaming logic.
Anything that does not depend on *how* bytes arrive lives here.

The proxy holds a tuple of ``Protocol`` (potentially more than one for
multi-protocol clients like opencode) and dispatches each incoming request
to the protocol whose ``allowed_paths`` matches.
"""

from __future__ import annotations

import gzip
import json
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

import aiohttp

from claude_tap.protocols import Protocol, select_for_path
from claude_tap.trace import EventBus

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

REDACT_HEADERS = frozenset({"x-api-key", "authorization"})


@dataclass
class ProxyContext:
    protocols: tuple[Protocol, ...]
    target: str
    bus: EventBus
    session: aiohttp.ClientSession
    turn_counter: int = field(default=0)

    def next_turn(self) -> int:
        self.turn_counter += 1
        return self.turn_counter

    def protocol_for(self, path: str) -> Protocol | None:
        return select_for_path(path, self.protocols)


def filter_headers(headers: Mapping[str, str], *, redact: bool = False) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        if redact and k.lower() in REDACT_HEADERS:
            out[k] = (v[:12] + "...") if len(v) > 12 else "***"
        else:
            out[k] = v
    return out


def build_upstream_url(target: str, path_qs: str, protocol: Protocol) -> str:
    rewritten = protocol.rewrite_upstream_path(path_qs, target)
    return target.rstrip("/") + "/" + rewritten.lstrip("/")


def parse_json_body(body: bytes) -> object:
    if not body:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body.decode("utf-8", errors="replace")


def maybe_decompress(body: bytes, content_encoding: str) -> bytes:
    enc = (content_encoding or "").lower()
    if not body:
        return body
    try:
        if enc == "gzip":
            return gzip.decompress(body)
        if enc == "deflate":
            return zlib.decompress(body)
    except Exception:
        return body
    return body


def build_http_record(
    *,
    request_id: str,
    turn: int,
    duration_ms: int,
    method: str,
    path: str,
    req_headers: Mapping[str, str],
    req_body: object,
    status: int,
    resp_headers: Mapping[str, str],
    resp_body: object,
    sse_events: list[dict] | None = None,
    upstream_base_url: str | None = None,
) -> dict:
    record: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "turn": turn,
        "duration_ms": duration_ms,
        "request": {
            "method": method,
            "path": path,
            "headers": filter_headers(req_headers, redact=True),
            "body": req_body,
        },
        "response": {
            "status": status,
            "headers": filter_headers(resp_headers),
            "body": resp_body,
        },
    }
    if sse_events:
        record["response"]["sse_events"] = sse_events
    if upstream_base_url:
        record["upstream_base_url"] = upstream_base_url
    return record


def build_ws_record(
    *,
    request_id: str,
    turn: int,
    duration_ms: int,
    path: str,
    req_headers: Mapping[str, str],
    client_messages: list[str],
    server_messages: list[str],
    upstream_base_url: str,
    error: str | None = None,
) -> dict:
    req_body: object = None
    for msg in client_messages:
        try:
            parsed = json.loads(msg)
            if req_body is None:
                req_body = parsed
        except (json.JSONDecodeError, ValueError):
            pass

    ws_events: list[dict] = []
    resp_body: object = None
    for msg in server_messages:
        try:
            parsed = json.loads(msg)
            ws_events.append(parsed)
            if isinstance(parsed, dict) and parsed.get("type") in ("response.completed", "response.done"):
                resp_body = parsed.get("response", parsed)
        except (json.JSONDecodeError, ValueError):
            ws_events.append({"raw": msg})

    if resp_body is None:
        for ev in ws_events:
            if isinstance(ev, dict) and ev.get("type") == "response.created":
                resp_body = ev.get("response", ev)
                break

    record: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "turn": turn,
        "duration_ms": duration_ms,
        "transport": "websocket",
        "request": {
            "method": "WEBSOCKET",
            "path": path,
            "headers": filter_headers(req_headers, redact=True),
            "body": req_body,
        },
        "response": {
            "status": 502 if error else 101,
            "headers": {},
            "body": resp_body,
        },
    }
    if ws_events:
        record["response"]["ws_events"] = ws_events
    if error:
        record["response"]["error"] = error
    if upstream_base_url:
        record["upstream_base_url"] = upstream_base_url
    return record
