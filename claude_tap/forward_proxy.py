"""HTTPS forward proxy with CONNECT + per-host TLS termination.

We accept ``CONNECT host:443`` from the client, terminate TLS using a leaf
cert minted by the local CA, read the plaintext HTTP request from inside the
tunnel, then forward it as a normal HTTPS call to the real upstream.

The TLS handshake is done by routing the original socket through a temporary
loopback ``asyncio`` server because ``loop.start_tls`` is unreliable on some
Python builds (notably macOS Python 3.11).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import aiohttp

from claude_tap.certs import CertificateAuthority
from claude_tap.pipeline import (
    HOP_BY_HOP,
    ProxyContext,
    build_http_record,
    capture_only_response,
    filter_headers,
    maybe_decompress,
    parse_json_body,
)
from claude_tap.protocols import Protocol

log = logging.getLogger("claude_tap")


class ForwardProxyServer:
    def __init__(self, host: str, port: int, ca: CertificateAuthority, ctx: ProxyContext) -> None:
        self.host = host
        self.port = port
        self._ca = ca
        self._ctx = ctx
        self._server: asyncio.Server | None = None
        self.actual_port: int = port

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        self.actual_port = self._server.sockets[0].getsockname()[1]
        return self.actual_port

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30)
            if not line:
                return
            parts = line.decode("utf-8", errors="replace").strip().split(" ")
            if len(parts) < 3:
                return
            method = parts[0].upper()
            if method == "CONNECT":
                await self._handle_connect(parts[1], reader, writer)
            else:
                await self._handle_plain(method, parts[1], reader, writer)
        except (ConnectionError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("forward proxy connection failed")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_connect(
        self,
        authority: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if ":" in authority:
            hostname, port_str = authority.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                port = 443
        else:
            hostname, port = authority, 443

        # Drain the rest of the CONNECT request headers.
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if line in (b"\r\n", b"\n", b""):
                break

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        ssl_ctx = self._ca.make_ssl_context(hostname)

        tls_reader_holder: list[asyncio.StreamReader] = []
        tls_writer_holder: list[asyncio.StreamWriter] = []
        connected = asyncio.Event()

        async def _accept(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            tls_reader_holder.append(r)
            tls_writer_holder.append(w)
            connected.set()

        tls_server = await asyncio.start_server(_accept, "127.0.0.1", 0, ssl=ssl_ctx)
        tls_port = tls_server.sockets[0].getsockname()[1]

        try:
            relay_r, relay_w = await asyncio.open_connection("127.0.0.1", tls_port)
        except (ConnectionError, OSError) as exc:
            tls_server.close()
            log.warning("forward: cannot connect to TLS relay for %s: %s", hostname, exc)
            return

        async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        relay_back = asyncio.create_task(_pipe(relay_r, writer))
        client_to_relay = asyncio.create_task(_pipe(reader, relay_w))

        try:
            await asyncio.wait_for(connected.wait(), timeout=15)
        except asyncio.TimeoutError:
            log.warning("forward: TLS handshake timed out for %s", hostname)
            relay_back.cancel()
            client_to_relay.cancel()
            tls_server.close()
            return

        tls_server.close()
        tls_reader = tls_reader_holder[0]
        tls_writer = tls_writer_holder[0]

        try:
            await self._handle_tunneled(hostname, port, tls_reader, tls_writer)
        finally:
            relay_back.cancel()
            client_to_relay.cancel()
            try:
                tls_writer.close()
                await tls_writer.wait_closed()
            except Exception:
                pass

    async def _handle_tunneled(
        self,
        hostname: str,
        port: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        while True:
            try:
                request_line = await asyncio.wait_for(reader.readline(), timeout=600)
            except (asyncio.TimeoutError, ConnectionError):
                break
            if not request_line:
                break
            text = request_line.decode("utf-8", errors="replace").strip()
            if not text:
                break
            parts = text.split(" ", 2)
            if len(parts) < 3:
                break
            method, path, _ = parts

            headers: dict[str, str] = {}
            while True:
                hl = await asyncio.wait_for(reader.readline(), timeout=30)
                if hl in (b"\r\n", b"\n", b""):
                    break
                hd = hl.decode("utf-8", errors="replace").strip()
                if ":" in hd:
                    k, v = hd.split(":", 1)
                    headers[k.strip()] = v.strip()

            body = b""
            cl = headers.get("Content-Length") or headers.get("content-length")
            if cl:
                try:
                    body = await asyncio.wait_for(reader.readexactly(int(cl)), timeout=60)
                except (ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                    pass

            protocol = self._ctx.protocol_for(path)
            if protocol is None:
                msg = b"Not Found"
                writer.write(b"HTTP/1.1 404 Not Found\r\n")
                writer.write(f"Content-Length: {len(msg)}\r\n\r\n".encode())
                writer.write(msg)
                await writer.drain()
                continue

            upstream_base = f"https://{hostname}:{port}"
            upstream = f"{upstream_base}{path}"
            await self._forward(method, path, headers, body, upstream, upstream_base, writer, protocol)

    async def _forward(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        upstream_url: str,
        upstream_base: str,
        client_writer: asyncio.StreamWriter,
        protocol: Protocol,
    ) -> None:
        ctx = self._ctx
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        t0 = time.monotonic()
        turn = ctx.next_turn()
        req_body = parse_json_body(body)
        streaming = protocol.is_streaming(path, req_body)
        model = req_body.get("model", "") if isinstance(req_body, dict) else ""
        log.info("[Turn %d] -> %s %s (%s) model=%s stream=%s", turn, method, path, protocol.name, model, streaming)

        if ctx.capture_only:
            resp_body = capture_only_response(protocol, path, req_body)
            duration_ms = int((time.monotonic() - t0) * 1000)
            record = build_http_record(
                request_id=request_id,
                turn=turn,
                duration_ms=duration_ms,
                method=method,
                path=path,
                req_headers=headers,
                req_body=req_body,
                status=200,
                resp_headers={"Content-Type": "application/json"},
                resp_body=resp_body,
                upstream_base_url=upstream_base,
            )
            await ctx.bus.publish(record)
            body_bytes = json.dumps(resp_body, separators=(",", ":")).encode()
            client_writer.write(b"HTTP/1.1 200 OK\r\n")
            client_writer.write(b"Content-Type: application/json\r\n")
            client_writer.write(f"Content-Length: {len(body_bytes)}\r\n\r\n".encode())
            client_writer.write(body_bytes)
            await client_writer.drain()
            log.info("[Turn %d] capture-only response returned; upstream skipped", turn)
            return

        fwd = filter_headers(headers)
        fwd.pop("Host", None)
        fwd.pop("host", None)
        fwd["Accept-Encoding"] = "identity"

        try:
            resp = await ctx.session.request(
                method=method,
                url=upstream_url,
                headers=fwd,
                data=body,
                timeout=aiohttp.ClientTimeout(total=600, sock_read=300),
            )
        except Exception as exc:
            log.error("[Turn %d] upstream error: %s", turn, exc)
            err = str(exc).encode()
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n")
            client_writer.write(f"Content-Length: {len(err)}\r\nContent-Type: text/plain\r\n\r\n".encode())
            client_writer.write(err)
            await client_writer.drain()
            return

        if streaming and resp.status == 200:
            await self._stream_back(
                resp, client_writer, request_id, turn, t0, method, path, headers, req_body, protocol, upstream_base
            )
        else:
            await self._buffered_back(
                resp, client_writer, request_id, turn, t0, method, path, headers, req_body, upstream_base
            )

    async def _stream_back(
        self,
        upstream: aiohttp.ClientResponse,
        writer: asyncio.StreamWriter,
        request_id: str,
        turn: int,
        t0: float,
        method: str,
        path: str,
        req_headers: dict[str, str],
        req_body: object,
        protocol: Protocol,
        upstream_base: str,
    ) -> None:
        ctx = self._ctx
        writer.write(f"HTTP/1.1 {upstream.status} {upstream.reason or ''}\r\n".encode())
        for k, v in upstream.headers.items():
            if k.lower() not in HOP_BY_HOP:
                writer.write(f"{k}: {v}\r\n".encode())
        writer.write(b"Transfer-Encoding: chunked\r\n\r\n")
        await writer.drain()

        reassembler = protocol.make_reassembler()

        try:
            async for chunk in upstream.content.iter_any():
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
                reassembler.feed_bytes(chunk)
        except (ConnectionError, asyncio.CancelledError):
            pass

        try:
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except Exception:
            pass

        duration_ms = int((time.monotonic() - t0) * 1000)
        record = build_http_record(
            request_id=request_id,
            turn=turn,
            duration_ms=duration_ms,
            method=method,
            path=path,
            req_headers=req_headers,
            req_body=req_body,
            status=upstream.status,
            resp_headers=dict(upstream.headers),
            resp_body=reassembler.reconstruct(),
            sse_events=reassembler.events,
            upstream_base_url=upstream_base,
        )
        await ctx.bus.publish(record)

    async def _buffered_back(
        self,
        upstream: aiohttp.ClientResponse,
        writer: asyncio.StreamWriter,
        request_id: str,
        turn: int,
        t0: float,
        method: str,
        path: str,
        req_headers: dict[str, str],
        req_body: object,
        upstream_base: str,
    ) -> None:
        ctx = self._ctx
        body = await upstream.read()
        duration_ms = int((time.monotonic() - t0) * 1000)
        decoded = maybe_decompress(body, upstream.headers.get("Content-Encoding", ""))
        resp_body = parse_json_body(decoded)

        record = build_http_record(
            request_id=request_id,
            turn=turn,
            duration_ms=duration_ms,
            method=method,
            path=path,
            req_headers=req_headers,
            req_body=req_body,
            status=upstream.status,
            resp_headers=dict(upstream.headers),
            resp_body=resp_body,
            upstream_base_url=upstream_base,
        )
        await ctx.bus.publish(record)

        writer.write(f"HTTP/1.1 {upstream.status} {upstream.reason or ''}\r\n".encode())
        skip = HOP_BY_HOP | {"content-length"}
        for k, v in upstream.headers.items():
            if k.lower() not in skip:
                writer.write(f"{k}: {v}\r\n".encode())
        writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode())
        writer.write(body)
        await writer.drain()

    async def _handle_plain(
        self,
        method: str,
        url: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        from urllib.parse import urlparse

        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if line in (b"\r\n", b"\n", b""):
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if ":" in decoded:
                k, v = decoded.split(":", 1)
                headers[k.strip()] = v.strip()

        body = b""
        cl = headers.get("Content-Length") or headers.get("content-length")
        if cl:
            try:
                body = await asyncio.wait_for(reader.readexactly(int(cl)), timeout=60)
            except (ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                pass

        parsed = urlparse(url)
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")

        protocol = self._ctx.protocol_for(path)
        if protocol is None:
            msg = b"Not Found"
            writer.write(b"HTTP/1.1 404 Not Found\r\n")
            writer.write(f"Content-Length: {len(msg)}\r\n\r\n".encode())
            writer.write(msg)
            await writer.drain()
            return

        upstream_base = f"{parsed.scheme}://{parsed.netloc}"
        await self._forward(method, path, headers, body, url, upstream_base, writer, protocol)
