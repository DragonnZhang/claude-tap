import json

import aiohttp
import pytest
from aiohttp import web

from claude_tap.certs import CertificateAuthority, ensure_ca
from claude_tap.forward_proxy import ForwardProxyServer
from claude_tap.ndjson import OllamaReassembler
from claude_tap.pipeline import ProxyContext
from claude_tap.protocols import OLLAMA
from claude_tap.reverse_proxy import build_app
from claude_tap.trace import EventBus, JsonlSink
from claude_tap.viewer import extract_metadata


def test_ollama_default_stream_and_fragmented_utf8_thinking_and_tools():
    assert OLLAMA.is_streaming("/api/chat", {"model": "gemma4:e4b"})
    assert not OLLAMA.is_streaming("/api/chat", {"stream": False})
    calls = [{"function": {"name": "weather", "arguments": {"city": "Paris"}}}]
    chunks = [
        {"model": "test", "message": {"role": "assistant", "thinking": "think", "content": "caf"}, "done": False},
        {"message": {"content": "é", "tool_calls": calls}, "done": False},
        {"message": {"content": ""}, "done": True, "prompt_eval_count": 3, "eval_count": 2},
    ]
    encoded = b"\r\n".join(json.dumps(chunk, ensure_ascii=False).encode() for chunk in chunks)
    parser = OllamaReassembler()
    for byte in encoded:
        parser.feed_bytes(bytes([byte]))
    result = parser.reconstruct()
    assert result["message"] == {"role": "assistant", "thinking": "think", "content": "café", "tool_calls": calls}
    assert result["done"] and len(parser.events) == 3
    usage = OLLAMA.extract_usage(result)
    assert (usage.input_tokens, usage.output_tokens) == (3, 2)
    assert parser.reconstruct() == result and len(parser.events) == 3


def test_partial_ollama_stream_keeps_incomplete_and_error_evidence():
    parser = OllamaReassembler()
    parser.feed_bytes(b'{"message":{"content":"partial"},"done":false}\n{"error":"stopped"}\n{"message":')
    result = parser.reconstruct()
    assert result == {"message": {"content": "partial"}, "done": False, "error": "stopped"}
    assert parser.events[-1] == {"event": "incomplete", "data": '{"message":'}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["forward", "reverse"])
@pytest.mark.parametrize("stream", [False, True])
async def test_native_ollama_recording_preserves_wire_and_reassembles(tmp_path, transport, stream):
    chunks = [
        {"model": "test", "message": {"role": "assistant", "content": "one"}, "done": False},
        {
            "message": {
                "content": "two",
                "tool_calls": [{"function": {"name": "shell", "arguments": {"command": "pwd"}}}],
            },
            "done": True,
            "prompt_eval_count": 7,
            "eval_count": 4,
        },
    ]
    expected = {
        "model": "test",
        "message": {"role": "assistant", "content": "onetwo", "tool_calls": chunks[1]["message"]["tool_calls"]},
        "done": True,
        "prompt_eval_count": 7,
        "eval_count": 4,
    }
    payload = b"\n".join(json.dumps(chunk).encode() for chunk in chunks) + b"\n"

    async def respond(request):
        assert (await request.json())["messages"][0]["content"] == "Task"
        if not stream:
            return web.json_response(expected)
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)
        await response.write(payload[:17])
        await response.write(payload[17:])
        await response.write_eof()
        return response

    upstream_app = web.Application()
    upstream_app.router.add_post("/api/chat", respond)
    upstream = web.AppRunner(upstream_app)
    await upstream.setup()
    site = web.TCPSite(upstream, "127.0.0.1", 0)
    await site.start()
    upstream_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    trace = tmp_path / "trace.jsonl"
    bus = EventBus()
    bus.subscribe(JsonlSink(trace))
    async with aiohttp.ClientSession() as session:
        context = ProxyContext((OLLAMA,), upstream_url, bus, session)
        if transport == "forward":
            cert, key = ensure_ca()
            server = ForwardProxyServer("127.0.0.1", 0, CertificateAuthority(cert, key), context)
            port = await server.start()
            url, proxy = upstream_url + "/api/chat", f"http://127.0.0.1:{port}"
        else:
            server = web.AppRunner(build_app(context))
            await server.setup()
            proxy_site = web.TCPSite(server, "127.0.0.1", 0)
            await proxy_site.start()
            url, proxy = f"http://127.0.0.1:{proxy_site._server.sockets[0].getsockname()[1]}/api/chat", None
        try:
            async with session.post(
                url,
                json={"model": "test", "messages": [{"role": "user", "content": "Task"}], "stream": stream},
                proxy=proxy,
            ) as response:
                assert response.status == 200
                if stream:
                    assert await response.read() == payload
                else:
                    assert await response.json() == expected
        finally:
            if transport == "forward":
                await server.stop()
            else:
                await server.cleanup()
            await bus.close_all()
            await upstream.cleanup()
    record = json.loads(trace.read_text().strip())
    assert record["response"]["body"] == expected
    metadata = extract_metadata(json.dumps(record))
    assert metadata["input_tokens"] == 7 and metadata["output_tokens"] == 4
    assert metadata["response_tool_names"] == ["shell"]


def test_chat_and_ollama_metadata_report_native_usage_tools_and_errors():
    message = {"role": "assistant", "tool_calls": [{"function": {"name": "shell", "arguments": {"command": "pwd"}}}]}
    for response in [
        {"message": message, "prompt_eval_count": 7, "eval_count": 4},
        {"choices": [{"message": message}], "usage": {"prompt_tokens": 7, "completion_tokens": 4}},
    ]:
        metadata = extract_metadata(json.dumps({"response": {"body": response}}))
        assert (metadata["input_tokens"], metadata["output_tokens"]) == (7, 4)
        assert metadata["response_tool_names"] == ["shell"]
    assert (
        extract_metadata(json.dumps({"response": {"body": {"error": "model unavailable"}}}))["error_message"]
        == "model unavailable"
    )
