"""Live viewer: serve the HTML and stream new records to the browser.

API surface (everything under ``/api/``, JSON unless noted):

    GET /                                  -> HTML viewer with LIVE_MODE = true
    GET /api/version                       -> {"server": str, "schema": int}
    GET /api/sessions                      -> {"current": str|null, "sessions": [...]}
    GET /api/sessions/{date}/{hhmmss}      -> {"id": str, "records": [...]}
    GET /api/stream                        -> SSE: hello | record | heartbeat

Session ids are ``"YYYY-MM-DD/HHMMSS"`` and map 1:1 to ``trace_<HHMMSS>.jsonl``
under the date directory. Records are read from disk on demand — the server
does not keep an in-memory window. The SSE stream only carries new records
for the *current* session (the live one). The browser fetches the baseline
through ``/api/sessions/{id}`` before opening the stream, so there is no
replay logic on the server side.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from claude_tap._version import __version__
from claude_tap.viewer import INJECT_MARKER

# Bumped when an incompatible change is made to the JSON shape exchanged with
# the viewer. The browser uses this to detect a stale embedded copy.
SCHEMA_VERSION = 1

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TRACE_FILE_RE = re.compile(r"^trace_(\d{6})\.jsonl$")


@dataclass(frozen=True)
class _SessionInfo:
    id: str
    date: str
    started_at: str
    record_count: int


class LiveViewerServer:
    """HTTP server providing the viewer + a thin JSON/SSE API."""

    def __init__(
        self,
        current_jsonl: Path | None,
        *,
        port: int = 0,
        host: str = "127.0.0.1",
        output_dir: Path,
    ) -> None:
        self._current_jsonl = current_jsonl
        self.port = port
        self.host = host
        self.output_dir = output_dir
        self._sse_clients: list[web.StreamResponse] = []
        self._runner: web.AppRunner | None = None
        self._actual_port = 0
        self._shutdown = asyncio.Event()

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self._actual_port}"

    @property
    def current_session_id(self) -> str | None:
        if self._current_jsonl is None:
            return None
        return self._jsonl_to_session_id(self._current_jsonl)

    async def start(self) -> int:
        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/api/version", self._handle_version)
        app.router.add_get("/api/sessions", self._handle_sessions)
        app.router.add_get("/api/sessions/{date}/{hhmmss}", self._handle_session_records)
        app.router.add_get("/api/stream", self._handle_stream)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        try:
            self._actual_port = site._server.sockets[0].getsockname()[1]
        except (AttributeError, IndexError, OSError):
            self._actual_port = self.port
        return self._actual_port

    async def stop(self) -> None:
        self._shutdown.set()
        for c in list(self._sse_clients):
            try:
                await c.write_eof()
            except Exception:
                pass
        self._sse_clients.clear()
        if self._runner:
            await self._runner.cleanup()

    async def broadcast(self, record: dict) -> None:
        """Push a record to every connected SSE client."""
        msg = ("event: record\ndata: " + json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode(
            "utf-8"
        )
        dropped: list[web.StreamResponse] = []
        for client in self._sse_clients:
            try:
                await client.write(msg)
            except Exception:
                dropped.append(client)
        for c in dropped:
            if c in self._sse_clients:
                self._sse_clients.remove(c)

    # ------------------------------------------------------------------
    # ID <-> path helpers (with traversal guard)
    # ------------------------------------------------------------------

    def _jsonl_to_session_id(self, p: Path) -> str | None:
        try:
            rel = p.relative_to(self.output_dir)
        except ValueError:
            return None
        parts = rel.parts
        if len(parts) != 2:
            return None
        date_dir, fname = parts
        m = _TRACE_FILE_RE.match(fname)
        if m and _DATE_RE.match(date_dir):
            return f"{date_dir}/{m.group(1)}"
        return None

    def _session_id_to_path(self, date: str, hhmmss: str) -> Path | None:
        if not _DATE_RE.match(date) or not re.match(r"^\d{6}$", hhmmss):
            return None
        path = self.output_dir / date / f"trace_{hhmmss}.jsonl"
        try:
            path.resolve().relative_to(self.output_dir.resolve())
        except (ValueError, OSError):
            return None
        return path if path.is_file() else None

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    async def _handle_index(self, request: web.Request) -> web.Response:
        template_path = Path(__file__).parent / "viewer.html"
        if not template_path.exists():
            return web.Response(status=404, text="viewer.html not found")
        html = template_path.read_text(encoding="utf-8")
        sid = self.current_session_id
        inject = (
            "<script>\n"
            "const LIVE_MODE = true;\n"
            f"const LIVE_SCHEMA = {SCHEMA_VERSION};\n"
            f"const CURRENT_SESSION_ID = {json.dumps(sid)};\n"
            f"const __CLAUDE_TAP_VERSION__ = {json.dumps(__version__)};\n"
            "</script>"
        )
        html = html.replace(INJECT_MARKER, inject + "\n" + INJECT_MARKER, 1)
        return web.Response(text=html, content_type="text/html")

    async def _handle_version(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"server": __version__, "schema": SCHEMA_VERSION},
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def _handle_sessions(self, request: web.Request) -> web.Response:
        sessions = list(self._enumerate_sessions())
        return web.json_response(
            {
                "current": self.current_session_id,
                "sessions": [s.__dict__ for s in sessions],
            },
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def _handle_session_records(self, request: web.Request) -> web.Response:
        date = request.match_info["date"]
        hhmmss = request.match_info["hhmmss"]
        path = self._session_id_to_path(date, hhmmss)
        if path is None:
            return web.Response(status=404, text="session not found")
        records: list[dict] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            return web.Response(status=500, text=str(exc))
        return web.json_response(
            {"id": f"{date}/{hhmmss}", "records": records},
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": "*",
            },
        )
        await resp.prepare(request)

        # Hello tells the browser which session this stream is following and
        # which schema version to expect, so it can detect a stale embedded
        # copy and offer a refresh.
        hello = {
            "session": self.current_session_id,
            "schema": SCHEMA_VERSION,
            "server": __version__,
        }
        try:
            await resp.write(("event: hello\ndata: " + json.dumps(hello) + "\n\n").encode("utf-8"))
        except (ConnectionError, ConnectionResetError):
            return resp

        self._sse_clients.append(resp)
        try:
            while not self._shutdown.is_set():
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                if self._shutdown.is_set():
                    break
                try:
                    await resp.write(b"event: heartbeat\ndata: \n\n")
                except (ConnectionError, ConnectionResetError, RuntimeError):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            if resp in self._sse_clients:
                self._sse_clients.remove(resp)
        return resp

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _enumerate_sessions(self):
        """Yield every session under ``output_dir``, newest first."""
        if not self.output_dir.is_dir():
            return
        for date_dir in sorted(self.output_dir.iterdir(), reverse=True):
            if not date_dir.is_dir() or not _DATE_RE.match(date_dir.name):
                continue
            for jsonl in sorted(date_dir.glob("trace_*.jsonl"), reverse=True):
                m = _TRACE_FILE_RE.match(jsonl.name)
                if not m:
                    continue
                try:
                    stat = jsonl.stat()
                    with open(jsonl, encoding="utf-8") as f:
                        count = sum(1 for line in f if line.strip())
                except OSError:
                    continue
                yield _SessionInfo(
                    id=f"{date_dir.name}/{m.group(1)}",
                    date=date_dir.name,
                    started_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    record_count=count,
                )


class LiveSink:
    """``EventBus`` sink: forwards every record into a ``LiveViewerServer``."""

    def __init__(self, server: LiveViewerServer) -> None:
        self._server = server

    async def handle(self, record: dict) -> None:
        await self._server.broadcast(record)

    async def close(self) -> None:
        pass
