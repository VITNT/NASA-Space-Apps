"""A response cache for the read-only data endpoints.

The archive only changes when a new run is published, so a response is fully
determined by its URL and the run ids. Each distinct request is computed once,
kept in memory and on disk (so a restart keeps it), and served with an ETag
and Cache-Control so browsers and a CDN in front of the server can reuse it.
On a small VM this turns every repeat view, shared link and demo into a file read.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode

import anyio

Scope = dict[str, Any]
Message = dict[str, Any]
ASGIApp = Callable[[Scope, Callable[[], Awaitable[Message]], Callable[[Message], Awaitable[None]]], Awaitable[None]]

CACHEABLE_PATHS = (
    "/v1/calendar", "/v1/coverage", "/v1/areas/countries", "/v1/spatial/grid", "/v1/spatial/points",
    "/v1/export/hotspots/count", "/v1/anomalies", "/v1/calibration",
)
CACHE_CONTROL = b"public, max-age=3600, s-maxage=3600, stale-while-revalidate=86400"
LARGEST_BODY = 16 * 1024 * 1024


class ResponseCache:
    """ASGI middleware: memory LRU in front of an on-disk store."""

    def __init__(self, app: ASGIApp, version: str, directory: Path | None,
                 memory_bytes: int = 48 * 1024 * 1024, disk_bytes: int = 512 * 1024 * 1024) -> None:
        self.app = app
        self.version = version
        self.memory_bytes = memory_bytes
        self.disk_bytes = disk_bytes
        self.memory: OrderedDict[str, tuple[bytes, bytes, bytes]] = OrderedDict()
        self.memory_used = 0
        self.lock = threading.Lock()
        self.directory = None
        self.disk: dict[Path, tuple[float, int]] = {}
        if directory is not None:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                probe = directory / ".write-test"
                probe.write_bytes(b"")
                probe.unlink()
                self.directory = directory
                for path in directory.glob("*/*.bin"):
                    stat = path.stat()
                    self.disk[path] = (stat.st_mtime, stat.st_size)
            except OSError:
                self.directory = None

    def key(self, scope: Scope, gzip: bool) -> str:
        path = scope["path"]
        query = urlencode(sorted(parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)))
        return hashlib.sha256(f"{self.version}|{path}?{query}|{int(gzip)}".encode()).hexdigest()

    def _disk_path(self, key: str) -> Path:
        assert self.directory is not None
        return self.directory / key[:2] / f"{key}.bin"

    def get(self, key: str) -> tuple[bytes, bytes, bytes] | None:
        with self.lock:
            if key in self.memory:
                self.memory.move_to_end(key)
                return self.memory[key]
        if self.directory is None:
            return None
        path = self._disk_path(key)
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        type_length, encoding_length = raw[0], raw[1]
        entry = (raw[2:2 + type_length], raw[2 + type_length:2 + type_length + encoding_length],
                 raw[2 + type_length + encoding_length:])
        self._remember(key, entry)
        return entry

    def _remember(self, key: str, entry: tuple[bytes, bytes, bytes]) -> None:
        size = len(entry[2])
        if size > self.memory_bytes // 4:
            return
        with self.lock:
            if key in self.memory:
                return
            self.memory[key] = entry
            self.memory_used += size
            while self.memory_used > self.memory_bytes:
                _, (_, _, body) = self.memory.popitem(last=False)
                self.memory_used -= len(body)

    def put(self, key: str, entry: tuple[bytes, bytes, bytes]) -> None:
        self._remember(key, entry)
        if self.directory is None:
            return
        content_type, encoding, body = entry
        path = self._disk_path(key)
        try:
            path.parent.mkdir(exist_ok=True)
            pending = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            pending.write_bytes(bytes([len(content_type), len(encoding)]) + content_type + encoding + body)
            os.replace(pending, path)
        except OSError:
            return
        with self.lock:
            self.disk[path] = (path.stat().st_mtime, len(body))
            used = sum(size for _, size in self.disk.values())
            if used <= self.disk_bytes:
                return
            for old, (_, size) in sorted(self.disk.items(), key=lambda item: item[1][0]):
                if used <= self.disk_bytes * .9:
                    break
                try:
                    old.unlink()
                except OSError:
                    pass
                used -= size
                del self.disk[old]

    async def __call__(self, scope: Scope, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        if scope["type"] != "http" or scope["method"] != "GET" or not scope["path"].startswith(CACHEABLE_PATHS):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        gzip = b"gzip" in headers.get(b"accept-encoding", b"")
        key = self.key(scope, gzip)
        etag = f'"{key[:32]}"'.encode()
        common = [(b"cache-control", CACHE_CONTROL), (b"etag", etag), (b"vary", b"Accept-Encoding")]
        if headers.get(b"if-none-match") == etag:
            await send({"type": "http.response.start", "status": 304, "headers": common})
            await send({"type": "http.response.body", "body": b""})
            return
        entry = await anyio.to_thread.run_sync(self.get, key)
        if entry is not None:
            content_type, encoding, body = entry
            response_headers = [*common, (b"content-type", content_type), (b"content-length", str(len(body)).encode()),
                                (b"x-cache", b"hit")]
            if encoding:
                response_headers.append((b"content-encoding", encoding))
            await send({"type": "http.response.start", "status": 200, "headers": response_headers})
            await send({"type": "http.response.body", "body": body})
            return

        state: dict[str, Any] = {"status": None, "type": b"", "encoding": b"", "chunks": [], "size": 0}

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                original = [(name, value) for name, value in message.get("headers", []) if name.lower() not in (b"cache-control", b"etag")]
                for name, value in original:
                    if name.lower() == b"content-type":
                        state["type"] = value
                    elif name.lower() == b"content-encoding":
                        state["encoding"] = value
                if message["status"] == 200:
                    message = {**message, "headers": [*original, *common, (b"x-cache", b"miss")]}
            elif message["type"] == "http.response.body" and state["status"] == 200:
                state["size"] += len(message.get("body", b""))
                if state["size"] <= LARGEST_BODY:
                    state["chunks"].append(message.get("body", b""))
                if not message.get("more_body") and state["size"] <= LARGEST_BODY:
                    entry = (state["type"], state["encoding"], b"".join(state["chunks"]))
                    await anyio.to_thread.run_sync(self.put, key, entry)
            await send(message)

        await self.app(scope, receive, capture)
