from __future__ import annotations

import json
import ssl

import aiohttp
import pytest
from aiohttp import web

from claude_tap.certs import CertificateAuthority, ensure_ca
from claude_tap.clients import generic
from claude_tap.forward_proxy import ForwardProxyServer
from claude_tap.pipeline import ProxyContext
from claude_tap.protocols import GEMINI, OPENAI, select_for_path
from claude_tap.reverse_proxy import build_app
from claude_tap.trace import EventBus, JsonlSink


def test_generic_google_v1_verbs_do_not_use_openai_models_protocol():
    protocols = generic("custom-cli").protocols
    assert select_for_path("/v1/models/test:generateContent", protocols) is GEMINI
    assert select_for_path("/v1/models/test:streamGenerateContent?alt=sse", protocols) is GEMINI
    assert select_for_path("/v1/models", protocols) is OPENAI


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["forward", "reverse"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("api", ["messages", "chat", "responses", "gemini-v1", "gemini-v1beta"])
async def test_generic_capture_returns_native_response_without_upstream(tmp_path, transport, stream, api):
    if api.startswith("gemini"):
        version = api.removeprefix("gemini-")
        verb = "streamGenerateContent?alt=sse" if stream else "generateContent"
        path = f"/{version}/models/test:{verb}"
        body = {
            "systemInstruction": {"parts": [{"text": "System"}]},
            "contents": [{"role": "user", "parts": [{"text": "Task"}]}],
        }
    else:
        path = {"messages": "/v1/messages", "chat": "/v1/chat/completions", "responses": "/v1/responses"}[api]
        body = {"model": "test-model", "stream": stream}
        if api == "responses":
            body.update(instructions="System", input="Task")
        else:
            body.update(messages=[{"role": "user", "content": "Task"}])

    class NoUpstream:
        def request(self, *args, **kwargs):
            raise AssertionError("capture-only must not call a provider")

    trace = tmp_path / "trace.jsonl"
    bus = EventBus()
    bus.subscribe(JsonlSink(trace))
    context = ProxyContext(
        generic("custom-cli").protocols, "https://unused.invalid", bus, NoUpstream(), capture_only=True
    )
    if transport == "forward":
        cert, key = ensure_ca()
        server = ForwardProxyServer("127.0.0.1", 0, CertificateAuthority(cert, key), context)
        port = await server.start()
        connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=str(cert)))
        url = "https://generic-capture.invalid" + path
        proxy = f"http://127.0.0.1:{port}"
    else:
        server = web.AppRunner(build_app(context))
        await server.setup()
        site = web.TCPSite(server, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        connector = aiohttp.TCPConnector()
        url, proxy = f"http://127.0.0.1:{port}" + path, None
    try:
        async with aiohttp.ClientSession(connector=connector) as client:
            async with client.post(url, json=body, proxy=proxy) as response:
                assert response.status == 200
                if stream:
                    text = await response.text()
                    assert "text/event-stream" in response.headers["Content-Type"]
                    marker = {
                        "messages": "message_stop",
                        "chat": "chat.completion.chunk",
                        "responses": "response.completed",
                    }.get(api, "candidates")
                    assert marker in text
                else:
                    payload = await response.json()
                    if api == "messages":
                        assert payload["type"] == "message"
                    elif api == "chat":
                        assert payload["object"] == "chat.completion"
                    elif api == "responses":
                        assert payload["status"] == "completed"
                    else:
                        assert payload["candidates"][0]["content"]["parts"][0]["text"] == "captured"
    finally:
        if transport == "forward":
            await server.stop()
        else:
            await server.cleanup()
        await bus.close_all()
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["request"]["body"] == body


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["forward", "reverse"])
async def test_capture_only_refuses_websocket_before_upstream_and_accepts_http_fallback(tmp_path, transport):
    class NoUpstream:
        trust_env = False

        def request(self, *args, **kwargs):
            raise AssertionError("HTTP capture-only must not call upstream")

        def ws_connect(self, *args, **kwargs):
            raise AssertionError("WebSocket capture-only must not call upstream")

    trace = tmp_path / "trace.jsonl"
    bus = EventBus()
    bus.subscribe(JsonlSink(trace))
    context = ProxyContext(
        generic("custom-cli").protocols, "https://unused.invalid", bus, NoUpstream(), capture_only=True
    )
    if transport == "forward":
        cert, key = ensure_ca()
        server = ForwardProxyServer("127.0.0.1", 0, CertificateAuthority(cert, key), context)
        port = await server.start()
        connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=str(cert)))
        url = "https://generic-capture.invalid/v1/responses"
        proxy = f"http://127.0.0.1:{port}"
    else:
        server = web.AppRunner(build_app(context))
        await server.setup()
        site = web.TCPSite(server, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        connector = aiohttp.TCPConnector()
        url, proxy = f"http://127.0.0.1:{port}/v1/responses", None
    body = {"model": "test-model", "instructions": "Native system", "input": "Task"}
    try:
        async with aiohttp.ClientSession(connector=connector) as client:
            with pytest.raises(aiohttp.WSServerHandshakeError) as error:
                await client.ws_connect(url, proxy=proxy)
            assert error.value.status == 426
            async with client.post(url, json=body, proxy=proxy) as response:
                assert response.status == 200
                assert (await response.json())["status"] == "completed"
    finally:
        if transport == "forward":
            await server.stop()
        else:
            await server.cleanup()
        await bus.close_all()
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["request"]["body"] == body
