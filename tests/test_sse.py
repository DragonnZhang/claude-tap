"""SSE parsing and per-protocol snapshot reconstruction."""

from __future__ import annotations

import json

from claude_tap.sse import AnthropicReassembler, OpenAIReassembler, parse_sse_lines

# --- parse_sse_lines -------------------------------------------------------


def test_parse_sse_two_events():
    raw = b"event: a\ndata: 1\n\nevent: b\ndata: 2\n\n"
    events, leftover = parse_sse_lines(raw)
    assert leftover == b""
    assert len(events) == 2
    assert events[0]["event"] == "a"
    assert events[0]["data"] == 1
    assert events[1]["event"] == "b"


def test_parse_sse_partial_event_kept_in_buffer():
    """An event without its trailing blank line must stay in the buffer."""
    raw = b"event: a\ndata: 1\n\nevent: b\ndata: 2\n"
    events, leftover = parse_sse_lines(raw)
    assert len(events) == 1  # only the first one is complete
    # Leftover should still contain the partial 'b' event so the next chunk
    # can complete it.
    assert b"event: b" in leftover


def test_parse_sse_data_decoded_as_json_when_possible():
    raw = b'event: x\ndata: {"k": 1}\n\n'
    events, _ = parse_sse_lines(raw)
    assert events[0]["data"] == {"k": 1}


def test_parse_sse_keeps_string_when_not_json():
    raw = b"event: x\ndata: hello\n\n"
    events, _ = parse_sse_lines(raw)
    assert events[0]["data"] == "hello"


# --- AnthropicReassembler --------------------------------------------------


def _anth_stream(parts: list[str]) -> bytes:
    return ("\n\n".join(parts) + "\n\n").encode("utf-8")


def _full_anth_stream() -> bytes:
    return _anth_stream(
        [
            'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","model":"claude-opus-4-6","content":[]}}',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}}',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
            'event: message_stop\ndata: {"type":"message_stop"}',
        ]
    )


def test_anthropic_reassembler_reconstructs_text():
    r = AnthropicReassembler()
    r.feed_bytes(_full_anth_stream())
    snap = r.reconstruct()
    assert snap is not None
    assert snap["id"] == "msg_1"
    assert snap["content"][0]["text"] == "Hello world"
    assert snap["stop_reason"] == "end_turn"
    assert snap["usage"]["output_tokens"] == 2


def test_anthropic_reassembler_robust_to_chunk_boundaries():
    """Feeding the same stream byte-by-byte must yield the same snapshot."""
    raw = _full_anth_stream()
    a = AnthropicReassembler()
    a.feed_bytes(raw)

    b = AnthropicReassembler()
    for i in range(0, len(raw), 7):
        b.feed_bytes(raw[i : i + 7])

    assert a.reconstruct() == b.reconstruct()


def test_anthropic_thinking_and_tool_use():
    parts = [
        'event: message_start\ndata: {"type":"message_start","message":{"id":"m","model":"x","content":[]}}',
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"reflect"}}',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"ing"}}',
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
        'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","name":"Read","input":{}}}',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\""}}',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":":\\"a.txt\\"}"}}',
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}',
    ]
    r = AnthropicReassembler()
    r.feed_bytes(_anth_stream(parts))
    snap = r.reconstruct() or {}
    assert snap["content"][0]["thinking"] == "reflecting"
    tool = snap["content"][1]
    assert tool["name"] == "Read"
    # Partial JSON is parsed into ``input`` on content_block_stop.
    assert tool["input"] == {"path": "a.txt"}
    assert "_partial_json" not in tool


def test_anthropic_reassembler_does_not_crash_on_malformed():
    r = AnthropicReassembler()
    r.feed_bytes(b"event: garbage\ndata: not-json\n\n")
    r.feed_bytes(b"event: message_start\ndata: not-json\n\n")
    # No exception raised.


# --- OpenAIReassembler -----------------------------------------------------


def test_parse_sse_data_only_frames_default_to_message_event():
    """Gemini's streamGenerateContent uses ``data:`` frames with no ``event:``;
    SSE spec says the event type defaults to ``message`` in that case."""
    raw = b'data: {"k":1}\n\ndata: {"k":2}\n\n'
    events, _ = parse_sse_lines(raw)
    assert len(events) == 2
    assert events[0]["event"] == "message"
    assert events[0]["data"] == {"k": 1}
    assert events[1]["event"] == "message"
    assert events[1]["data"] == {"k": 2}


def _gemini_chunk(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload) + "\n\n").encode("utf-8")


def test_gemini_reassembler_concatenates_text_parts():
    from claude_tap.sse import GeminiReassembler

    r = GeminiReassembler()
    r.feed_bytes(
        _gemini_chunk(
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": "Hello"}], "role": "model"},
                        "index": 0,
                    }
                ]
            }
        )
    )
    r.feed_bytes(
        _gemini_chunk({"candidates": [{"content": {"parts": [{"text": " world"}], "role": "model"}, "index": 0}]})
    )
    r.feed_bytes(
        _gemini_chunk(
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": "!"}], "role": "model"},
                        "index": 0,
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3, "totalTokenCount": 8},
            }
        )
    )
    snap = r.reconstruct() or {}
    assert snap["candidates"][0]["content"]["parts"][0]["text"] == "Hello world!"
    assert snap["candidates"][0]["finishReason"] == "STOP"
    assert snap["usageMetadata"]["candidatesTokenCount"] == 3


def test_gemini_reassembler_keeps_thinking_separate_from_text():
    """If the model emits ``thought=true`` parts, they should accumulate
    independently from regular text parts."""
    import json as _json  # avoid shadowing

    from claude_tap.sse import GeminiReassembler

    _ = _json

    r = GeminiReassembler()
    r.feed_bytes(
        _gemini_chunk(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "thinking-a", "thought": True},
                                {"text": "answer-a"},
                            ],
                            "role": "model",
                        }
                    }
                ]
            }
        )
    )
    r.feed_bytes(
        _gemini_chunk(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "-b", "thought": True},
                                {"text": "-b"},
                            ],
                            "role": "model",
                        }
                    }
                ]
            }
        )
    )
    snap = r.reconstruct() or {}
    parts = snap["candidates"][0]["content"]["parts"]
    thoughts = [p for p in parts if p.get("thought")]
    answers = [p for p in parts if not p.get("thought")]
    assert thoughts and thoughts[0]["text"] == "thinking-a-b"
    assert answers and answers[0]["text"] == "answer-a-b"


def test_openai_reassembler_response_completed():
    raw = b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","usage":{"input_tokens":3}}}\n\n'
    r = OpenAIReassembler()
    r.feed_bytes(raw)
    snap = r.reconstruct() or {}
    assert snap["id"] == "resp_1"
    assert snap["usage"]["input_tokens"] == 3


def test_openai_reassembler_collects_items_from_output_item_done():
    """Codex's ``response.completed.response.output`` is empty; the actual
    items (messages + function_calls) arrive as ``response.output_item.done``
    frames. Reconstruct() must splice them into the empty output."""
    chunks = [
        b'event: response.created\ndata: {"type":"response.created","response":{"id":"r","output":[]}}\n\n',
        b'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"function_call","name":"update_plan","arguments":"{}"}}\n\n',
        b'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"done"}]}}\n\n',
        b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r","output":[]}}\n\n',
    ]
    r = OpenAIReassembler()
    for c in chunks:
        r.feed_bytes(c)
    snap = r.reconstruct() or {}
    assert len(snap["output"]) == 2
    assert snap["output"][0]["type"] == "function_call"
    assert snap["output"][0]["name"] == "update_plan"
    assert snap["output"][1]["type"] == "message"
    assert snap["output"][1]["content"][0]["text"] == "done"


def test_openai_reassembler_does_not_overwrite_populated_output():
    """If the upstream did populate ``response.output`` (non-codex behavior),
    trust it — never replace with our accumulator."""
    chunks = [
        b'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"function_call","name":"streamed"}}\n\n',
        b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r","output":[{"type":"function_call","name":"final"}]}}\n\n',
    ]
    r = OpenAIReassembler()
    for c in chunks:
        r.feed_bytes(c)
    snap = r.reconstruct() or {}
    assert len(snap["output"]) == 1
    assert snap["output"][0]["name"] == "final"
