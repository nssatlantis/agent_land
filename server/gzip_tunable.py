"""server/gzip_tunable.py — tunable gzip middleware with live config + window/memlevel.

Wraps Starlette's GZipMiddleware but exposes minimum_size, compresslevel,
wbits (window), memLevel and thread_minimum_size as live FORUM_GZIP_*
tunables (config.py). Window 9-15 (15=32KB history, best for our 6-27KB
HTML/CSS/JSON), memLevel 1-9 (8=256KB default). Keeps Starlette's
DEFAULT_EXCLUDED_CONTENT_TYPES, streaming (more_body + Z_SYNC_FLUSH),
CapacityLimiter(40) offload for >=thread_minimum_size, and Vary handling.

Usage:
  from server.gzip_tunable import TunableGZipMiddleware
  Middleware(TunableGZipMiddleware)  # reads config live (700/6/15/8)
  Middleware(TunableGZipMiddleware, minimum_size=700, compresslevel=6)  # override
"""

from __future__ import annotations

import zlib
from typing import NoReturn

import anyio.lowlevel
import anyio.to_thread
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import config

DEFAULT_EXCLUDED_CONTENT_TYPES = (
    "application/gzip",
    "application/x-gzip",
    "application/zip",
    "audio/*",
    "font/woff",
    "font/woff2",
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/event-stream",
    "video/*",
)

_gzip_capacity_limiter: anyio.lowlevel.RunVar[anyio.CapacityLimiter] = (
    anyio.lowlevel.RunVar("_gzip_capacity_limiter")
)


def _get_gzip_capacity_limiter() -> anyio.CapacityLimiter:
    try:
        return _gzip_capacity_limiter.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(40)
        _gzip_capacity_limiter.set(limiter)
        return limiter


def _clamp(v: int, lo: int, hi: int, default: int) -> int:
    try:
        iv = int(v)
    except Exception:
        return default
    return max(lo, min(hi, iv))


def _normalize_content_types(content_types: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(ct.partition(";")[0].strip().lower() for ct in content_types)


class _IdentityResponder:
    content_encoding: str = ""

    def __init__(
        self,
        app: ASGIApp,
        minimum_size: int,
        *,
        exclude_content_types: tuple[str, ...] = DEFAULT_EXCLUDED_CONTENT_TYPES,
    ) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.exclude_content_types = _normalize_content_types(exclude_content_types)
        self.send: Send = _unattached_send
        self.initial_message: Message = {}
        self.started = False
        self.content_encoding_set = False
        self.content_type_is_excluded = False
        self.partial_response = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.send = send
        await self.app(scope, receive, self.send_with_compression)

    async def send_with_compression(self, message: Message) -> None:
        message_type = message["type"]
        if message_type == "http.response.start":
            self.initial_message = message
            headers = Headers(raw=self.initial_message["headers"])
            self.content_encoding_set = "content-encoding" in headers
            self.partial_response = message["status"] == 206
            media_type = (
                headers.get("content-type", "").partition(";")[0].strip().lower()
            )
            media_types = {media_type, media_type.partition("/")[0] + "/*"}
            self.content_type_is_excluded = not media_types.isdisjoint(
                self.exclude_content_types
            )
        elif message_type == "http.response.body" and (
            self.content_encoding_set
            or self.partial_response
            or self.content_type_is_excluded
        ):
            if not self.started:
                self.started = True
                await self.send(self.initial_message)
            await self.send(message)
        elif message_type == "http.response.body" and not self.started:
            self.started = True
            body = message.get("body", b"")
            more_body = message.get("more_body", False)
            if len(body) < self.minimum_size and not more_body:
                await self.send(self.initial_message)
                await self.send(message)
            elif not more_body:
                body = await self.apply_compression(body, more_body=False)
                headers = MutableHeaders(raw=self.initial_message["headers"])
                headers.add_vary_header("Accept-Encoding")
                if body != message["body"]:
                    headers["Content-Encoding"] = self.content_encoding
                    headers["Content-Length"] = str(len(body))
                    message["body"] = body
                await self.send(self.initial_message)
                await self.send(message)
            else:
                body = await self.apply_compression(body, more_body=True)
                headers = MutableHeaders(raw=self.initial_message["headers"])
                headers.add_vary_header("Accept-Encoding")
                if body != message["body"]:
                    headers["Content-Encoding"] = self.content_encoding
                    del headers["Content-Length"]
                    message["body"] = body
                await self.send(self.initial_message)
                await self.send(message)
        elif message_type == "http.response.body":
            body = message.get("body", b"")
            more_body = message.get("more_body", False)
            message["body"] = await self.apply_compression(body, more_body=more_body)
            await self.send(message)
        elif message_type == "http.response.pathsend":  # pragma: no branch
            await self.send(self.initial_message)
            await self.send(message)

    async def apply_compression(self, body: bytes, *, more_body: bool) -> bytes:
        return body


class _GZipResponder(_IdentityResponder):
    content_encoding = "gzip"

    def __init__(
        self,
        app: ASGIApp,
        minimum_size: int,
        compresslevel: int = 6,
        wbits: int = 15,
        memlevel: int = 8,
        thread_minimum_size: int = 128 * 1024,
        *,
        exclude_content_types: tuple[str, ...] = DEFAULT_EXCLUDED_CONTENT_TYPES,
    ) -> None:
        super().__init__(app, minimum_size, exclude_content_types=exclude_content_types)
        self.compresslevel = _clamp(compresslevel, 1, 9, 6)
        self.wbits = _clamp(wbits, 9, 15, 15)
        self.memlevel = _clamp(memlevel, 1, 9, 8)
        self.thread_minimum_size = thread_minimum_size
        self._compressor: zlib._Compress | None = None

    @property
    def compressor(self) -> zlib._Compress:
        if self._compressor is None:
            # 16 + wbits = gzip wrapper, wbits 15=32KB history best for 6-27KB payloads
            self._compressor = zlib.compressobj(
                self.compresslevel, zlib.DEFLATED, 16 + self.wbits, self.memlevel
            )
        return self._compressor

    async def apply_compression(self, body: bytes, *, more_body: bool) -> bytes:
        if len(body) >= self.thread_minimum_size:
            limiter = _get_gzip_capacity_limiter()
            return await anyio.to_thread.run_sync(
                self._compress_body, body, more_body, limiter=limiter
            )
        return self._compress_body(body, more_body)

    def _compress_body(self, body: bytes, more_body: bool) -> bytes:
        if more_body:
            return self.compressor.compress(body) + self.compressor.flush(
                zlib.Z_SYNC_FLUSH
            )
        return self.compressor.compress(body) + self.compressor.flush()


async def _unattached_send(message: Message) -> NoReturn:
    raise RuntimeError("send awaitable not set")  # pragma: no cover


class TunableGZipMiddleware:
    """Live-configurable gzip. Reads FORUM_GZIP_* on each request so .env
    edits apply without restart (like other config tunables). Explicit
    constructor args override the live values."""

    def __init__(
        self,
        app: ASGIApp,
        minimum_size: int | None = None,
        compresslevel: int | None = None,
        wbits: int | None = None,
        memlevel: int | None = None,
        thread_minimum_size: int | None = None,
        *,
        exclude_content_types: tuple[str, ...] | None = None,
    ) -> None:
        self.app = app
        self._minimum_size = minimum_size
        self._compresslevel = compresslevel
        self._wbits = wbits
        self._memlevel = memlevel
        self._thread_minimum_size = thread_minimum_size
        self._exclude_content_types = exclude_content_types

    def _resolve(self) -> tuple[int, int, int, int, int, tuple[str, ...]]:
        # Live read — config.__getattr__ resolves env each call, reload-safe
        try:
            cfg_min = int(config.GZIP_MINIMUM_SIZE)
        except Exception:
            cfg_min = 700
        try:
            cfg_level = int(config.GZIP_COMPRESSLEVEL)
        except Exception:
            cfg_level = 6
        try:
            cfg_wbits = int(config.GZIP_WBITS)
        except Exception:
            cfg_wbits = 15
        try:
            cfg_mem = int(config.GZIP_MEMLEVEL)
        except Exception:
            cfg_mem = 8
        try:
            cfg_thread = int(config.GZIP_THREAD_MINIMUM_SIZE)
        except Exception:
            cfg_thread = 128 * 1024
        minimum_size = self._minimum_size if self._minimum_size is not None else cfg_min
        compresslevel = (
            self._compresslevel if self._compresslevel is not None else cfg_level
        )
        wbits = self._wbits if self._wbits is not None else cfg_wbits
        memlevel = self._memlevel if self._memlevel is not None else cfg_mem
        thread_min = (
            self._thread_minimum_size
            if self._thread_minimum_size is not None
            else cfg_thread
        )
        exclude = (
            self._exclude_content_types
            if self._exclude_content_types is not None
            else DEFAULT_EXCLUDED_CONTENT_TYPES
        )
        return (
            _clamp(minimum_size, 0, 10 * 1024 * 1024, 700),
            _clamp(compresslevel, 1, 9, 6),
            _clamp(wbits, 9, 15, 15),
            _clamp(memlevel, 1, 9, 8),
            max(0, int(thread_min)),
            exclude,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        minimum_size, compresslevel, wbits, memlevel, thread_min, exclude = (
            self._resolve()
        )
        if "gzip" in headers.get("Accept-Encoding", ""):
            responder: _IdentityResponder = _GZipResponder(
                self.app,
                minimum_size,
                compresslevel=compresslevel,
                wbits=wbits,
                memlevel=memlevel,
                thread_minimum_size=thread_min,
                exclude_content_types=exclude,
            )
        else:
            responder = _IdentityResponder(
                self.app, minimum_size, exclude_content_types=exclude
            )
        await responder(scope, receive, send)
