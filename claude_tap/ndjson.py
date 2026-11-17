"""Reassemble native Ollama chat streams without changing their wire framing."""

from __future__ import annotations

import copy
import json


class OllamaReassembler:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self._buffer = b""
        self._snapshot: dict | None = None

    def feed_bytes(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._consume(line)

    def _consume(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            data = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            self.events.append({"event": "incomplete", "data": line.decode("utf-8", errors="replace")})
            return
        self.events.append({"event": "message", "data": data})
        if not isinstance(data, dict):
            return
        if self._snapshot is None:
            self._snapshot = {}
        self._snapshot.update({key: copy.deepcopy(value) for key, value in data.items() if key != "message"})
        message = data.get("message")
        if isinstance(message, dict):
            target = self._snapshot.setdefault("message", {})
            for key, value in message.items():
                if key in ("content", "thinking") and isinstance(value, str):
                    previous = target.get(key)
                    target[key] = (previous if isinstance(previous, str) else "") + value
                elif key == "tool_calls" and isinstance(value, list):
                    if not isinstance(target.get(key), list):
                        target[key] = []
                    target[key].extend(copy.deepcopy(value))
                else:
                    target[key] = copy.deepcopy(value)

    def reconstruct(self) -> dict | None:
        if self._buffer.strip():
            self._consume(self._buffer)
        self._buffer = b""
        return self._snapshot
