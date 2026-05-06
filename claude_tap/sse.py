"""Protocol-agnostic SSE parsing plus per-protocol snapshot reassembly.

The reassemblers consume a stream of decoded SSE events and rebuild the
*final* message/response object that the upstream API would have returned in
non-streaming mode. The reconstructed snapshot is what we record into the
trace so downstream tooling (viewer, export) sees a consistent shape.
"""

from __future__ import annotations

import copy
import json
from typing import Protocol


def parse_sse_lines(buf: bytes) -> tuple[list[dict], bytes]:
    """Parse complete SSE events out of ``buf``.

    Returns ``(events, leftover)``. Each event is ``{"event": str, "data": ...}``
    where ``data`` is decoded JSON when possible, otherwise the raw string.

    SSE events are terminated by a blank line (``\\n\\n``). Anything after the
    last ``\\n\\n`` is incomplete and stays in ``leftover`` for the next call.
    """

    if b"\n\n" not in buf:
        return [], buf

    chunks = buf.split(b"\n\n")
    leftover = chunks[-1]
    raw_events = chunks[:-1]

    events: list[dict] = []
    for raw in raw_events:
        cur_event: str | None = None
        cur_data: list[str] = []
        for line in raw.decode("utf-8", errors="replace").split("\n"):
            line = line.rstrip("\r")
            if line.startswith("event:"):
                cur_event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                cur_data.append(line[len("data:") :].strip())
            # Comments (`:`), retry, id, and unknown fields are ignored.
        if cur_event is None and not cur_data:
            continue
        body = "\n".join(cur_data)
        try:
            data: object = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            data = body
        # Per the SSE spec, a frame with `data:` but no `event:` defaults to
        # the event type "message". Gemini's streamGenerateContent uses this
        # form; without the default we'd silently drop every chunk.
        events.append({"event": cur_event if cur_event is not None else "message", "data": data})

    return events, leftover


class StreamReassembler(Protocol):
    """Build a final response snapshot from an SSE event stream."""

    events: list[dict]

    def feed_bytes(self, chunk: bytes) -> None: ...

    def reconstruct(self) -> dict | None: ...


class _BaseReassembler:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self._buf: bytes = b""

    def feed_bytes(self, chunk: bytes) -> None:
        self._buf += chunk
        events, self._buf = parse_sse_lines(self._buf)
        for ev in events:
            self.events.append(ev)
            self._accumulate(ev["event"], ev["data"])

    def _accumulate(self, event_type: str, data: object) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class AnthropicReassembler(_BaseReassembler):
    """Reassemble Anthropic Messages API streaming events."""

    def __init__(self) -> None:
        super().__init__()
        self._snapshot: dict | None = None

    def reconstruct(self) -> dict | None:
        return self._snapshot

    def _accumulate(self, event_type: str, data: object) -> None:
        if not isinstance(data, dict):
            return
        try:
            if event_type == "message_start":
                self._snapshot = copy.deepcopy(data.get("message", {}))
                return

            if self._snapshot is None:
                return

            if event_type == "content_block_start":
                block = copy.deepcopy(data.get("content_block", {}))
                content = self._snapshot.setdefault("content", [])
                idx = data.get("index", len(content))
                while len(content) <= idx:
                    content.append({})
                content[idx] = block
            elif event_type == "content_block_delta":
                content = self._snapshot.get("content") or []
                idx = data.get("index", 0)
                if idx >= len(content):
                    return
                block = content[idx]
                delta = data.get("delta", {})
                d_type = delta.get("type")
                if d_type == "text_delta":
                    block["text"] = block.get("text", "") + delta.get("text", "")
                elif d_type == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
                elif d_type == "input_json_delta":
                    block["_partial_json"] = block.get("_partial_json", "") + delta.get("partial_json", "")
            elif event_type == "content_block_stop":
                content = self._snapshot.get("content") or []
                idx = data.get("index", 0)
                if idx >= len(content):
                    return
                block = content[idx]
                if "_partial_json" in block:
                    try:
                        block["input"] = json.loads(block["_partial_json"])
                    except (json.JSONDecodeError, ValueError):
                        pass
                    del block["_partial_json"]
            elif event_type == "message_delta":
                delta = data.get("delta", {})
                for k, v in delta.items():
                    self._snapshot[k] = v
                usage = data.get("usage", {})
                if usage:
                    self._snapshot.setdefault("usage", {}).update(usage)
        except Exception:
            # Streaming protocols evolve; never fail reconstruction.
            pass


class OpenAIReassembler(_BaseReassembler):
    """Reassemble OpenAI Responses API streaming events."""

    def __init__(self) -> None:
        super().__init__()
        self._snapshot: dict | None = None

    def reconstruct(self) -> dict | None:
        return self._snapshot

    def _accumulate(self, event_type: str, data: object) -> None:
        if not isinstance(data, dict):
            return
        if event_type in ("response.created", "response.completed", "response.done"):
            response = data.get("response")
            if isinstance(response, dict):
                self._snapshot = copy.deepcopy(response)
            elif event_type in ("response.completed", "response.done"):
                self._snapshot = copy.deepcopy(data)


class PassthroughReassembler(_BaseReassembler):
    """For protocols whose SSE shape we don't model (Cursor / Qoder / Devin).

    We still parse every SSE frame so ``response.sse_events`` in the trace
    captures the full event stream, but we never synthesize a structured
    response snapshot. ``reconstruct()`` returns ``None``.
    """

    def reconstruct(self) -> dict | None:
        return None

    def _accumulate(self, event_type: str, data: object) -> None:
        # The base class already appends to ``self.events``; nothing else
        # to do for the passthrough case.
        pass


class GeminiReassembler(_BaseReassembler):
    """Reassemble Google Gemini ``:streamGenerateContent`` events.

    Each SSE chunk is a self-contained JSON like::

        {"candidates": [{"content": {"parts": [{"text": "Hello"}], "role": "model"}}],
         "usageMetadata": {...}}

    We accumulate by appending the text/thinking parts, and the latest
    ``usageMetadata`` and ``finishReason`` win.
    """

    def __init__(self) -> None:
        super().__init__()
        self._snapshot: dict | None = None

    def reconstruct(self) -> dict | None:
        return self._snapshot

    def _accumulate(self, event_type: str, data: object) -> None:
        # Gemini uses the default "message" event (data-only frames). The
        # JSON itself carries everything we need.
        if not isinstance(data, dict):
            return
        try:
            if self._snapshot is None:
                self._snapshot = copy.deepcopy(data)
            else:
                self._merge_chunk(self._snapshot, data)
        except Exception:
            # Malformed chunk shouldn't break reconstruction.
            pass

    @staticmethod
    def _merge_chunk(snap: dict, chunk: dict) -> None:
        # Merge candidates by index.
        chunk_cands = chunk.get("candidates")
        if isinstance(chunk_cands, list):
            snap_cands = snap.setdefault("candidates", [])
            for cand in chunk_cands:
                if not isinstance(cand, dict):
                    continue
                idx = cand.get("index", 0) if isinstance(cand.get("index"), int) else 0
                while len(snap_cands) <= idx:
                    snap_cands.append({})
                _merge_candidate(snap_cands[idx], cand)
        # Latest usageMetadata replaces, latest finishReason replaces.
        if isinstance(chunk.get("usageMetadata"), dict):
            snap["usageMetadata"] = chunk["usageMetadata"]
        if "promptFeedback" in chunk:
            snap["promptFeedback"] = chunk["promptFeedback"]


def _merge_candidate(snap_cand: dict, chunk_cand: dict) -> None:
    snap_content = snap_cand.setdefault("content", {})
    chunk_content = chunk_cand.get("content") or {}
    if "role" in chunk_content and "role" not in snap_content:
        snap_content["role"] = chunk_content["role"]
    chunk_parts = chunk_content.get("parts")
    if isinstance(chunk_parts, list):
        snap_parts = snap_content.setdefault("parts", [])
        for chunk_part in chunk_parts:
            if not isinstance(chunk_part, dict):
                continue
            _merge_part(snap_parts, chunk_part)
    if "finishReason" in chunk_cand:
        snap_cand["finishReason"] = chunk_cand["finishReason"]
    if "safetyRatings" in chunk_cand:
        snap_cand["safetyRatings"] = chunk_cand["safetyRatings"]


def _merge_part(snap_parts: list, chunk_part: dict) -> None:
    """Append-or-merge a single part. Text parts of the same kind get
    concatenated; everything else is appended as-is."""
    # Determine the "kind" of this part (text / thought / functionCall /
    # inlineData ...) and the most recent matching part in snap_parts.
    if "text" in chunk_part and not chunk_part.get("thought"):
        for prev in reversed(snap_parts):
            if isinstance(prev, dict) and "text" in prev and not prev.get("thought"):
                prev["text"] = prev.get("text", "") + chunk_part.get("text", "")
                return
    if chunk_part.get("thought") and "text" in chunk_part:
        for prev in reversed(snap_parts):
            if isinstance(prev, dict) and prev.get("thought") and "text" in prev:
                prev["text"] = prev.get("text", "") + chunk_part.get("text", "")
                return
    snap_parts.append(copy.deepcopy(chunk_part))
