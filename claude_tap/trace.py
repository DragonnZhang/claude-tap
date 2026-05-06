"""Trace event bus and built-in sinks.

Every recorded request becomes a single ``record`` dict that is published to
all subscribed sinks. The pipeline never knows about file writers or live
viewers directly — it just calls ``bus.publish(record)``. This is the
abstraction that lets us add new sinks (webhook, Prometheus, …) without
touching the proxy code.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Protocol as _Protocol

from claude_tap.protocols import Protocol, Usage, select_for_path


class Sink(_Protocol):
    async def handle(self, record: dict) -> None: ...

    async def close(self) -> None: ...


class EventBus:
    def __init__(self) -> None:
        self._sinks: list[Sink] = []

    def subscribe(self, sink: Sink) -> None:
        self._sinks.append(sink)

    async def publish(self, record: dict) -> None:
        for sink in self._sinks:
            try:
                await sink.handle(record)
            except Exception:
                # A misbehaving sink must not stop the others (or the proxy).
                pass

    async def close_all(self) -> None:
        for sink in self._sinks:
            try:
                await sink.close()
            except Exception:
                pass


class JsonlSink:
    """Append every record as one JSON line."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = open(path, "a", encoding="utf-8")
        self._lock = asyncio.Lock()
        self.count = 0

    async def handle(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._lock:
            self._fp.write(line)
            self._fp.flush()
            self.count += 1

    async def close(self) -> None:
        if not self._fp.closed:
            self._fp.flush()
            self._fp.close()


class StatsSink:
    """In-memory token / model accounting for the run summary.

    Receives a tuple of protocols rather than a single one; for each record we
    pick the protocol that owns the request path and use *its* usage
    extractor. This lets multi-protocol clients (opencode) report tokens
    correctly regardless of which backend the user routed to.
    """

    def __init__(self, protocols: tuple[Protocol, ...]) -> None:
        self._protocols = protocols
        self.api_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_create_tokens = 0
        self.models: dict[str, int] = {}

    async def handle(self, record: dict) -> None:
        self.api_calls += 1
        req = record.get("request") or {}
        req_body = req.get("body") or {}
        if isinstance(req_body, dict):
            model = req_body.get("model") or "unknown"
            self.models[model] = self.models.get(model, 0) + 1

        resp_body = (record.get("response") or {}).get("body") or {}
        usage: Usage = Usage()
        if isinstance(resp_body, dict):
            protocol = select_for_path(req.get("path") or "", self._protocols) or (
                self._protocols[0] if self._protocols else None
            )
            if protocol is not None:
                usage = protocol.extract_usage(resp_body)
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_create_tokens += usage.cache_create_tokens

    async def close(self) -> None:
        pass

    def summary(self) -> dict:
        return {
            "api_calls": self.api_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_create_tokens": self.cache_create_tokens,
            "models": dict(self.models),
        }
