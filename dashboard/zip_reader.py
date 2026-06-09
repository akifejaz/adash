"""Read individual files out of a remote zip using HTTP Range requests.

GitHub release assets live on storage that supports byte-range fetches, so we
can open a remote zip with ``zipfile.ZipFile`` over a small seekable
adapter and pull just the entries we need (``build_recipe.md``,
``agent_<pkg>.log``, …) without downloading the whole archive.

A small LRU cache keeps recently extracted bytes hot so repeat views of the
same log/recipe are instant.
"""

from __future__ import annotations

import logging
import threading
import urllib.request
import zipfile
from collections import OrderedDict

LOGGER = logging.getLogger(__name__)

USER_AGENT = "atesor-dashboard/1.0"


class HTTPRangeReader:
    """Minimal seekable read-only file-like over an HTTP resource.

    Only the methods ``zipfile.ZipFile`` exercises are implemented: ``read``,
    ``seek``, ``tell``, ``seekable``. Requests use ``Range`` headers so a full
    download is never required.
    """

    def __init__(self, url: str, timeout: int = 30) -> None:
        self.url = url
        self.timeout = timeout
        self.pos = 0
        self.size = self._probe_size()

    def _probe_size(self) -> int:
        """Return the resource length, following redirects."""
        req = urllib.request.Request(self.url, method="HEAD")
        req.add_header("User-Agent", USER_AGENT)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            length = resp.headers.get("Content-Length")
            if length is None:
                raise RuntimeError(f"HEAD {self.url} did not return Content-Length")
            return int(length)

    # File-like surface ---------------------------------------------------
    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos += offset
        elif whence == 2:
            self.pos = self.size + offset
        else:
            raise ValueError(f"Unsupported whence {whence}")
        if self.pos < 0:
            self.pos = 0
        return self.pos

    def read(self, n: int = -1) -> bytes:
        if self.pos >= self.size:
            return b""
        if n is None or n < 0:
            n = self.size - self.pos
        end = min(self.pos + n - 1, self.size - 1)
        req = urllib.request.Request(self.url)
        req.add_header("Range", f"bytes={self.pos}-{end}")
        req.add_header("User-Agent", USER_AGENT)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = resp.read()
        self.pos += len(data)
        return data


class _LRU:
    """Tiny size-and-count bounded LRU for extracted zip entries."""

    def __init__(self, max_entries: int = 256, max_bytes: int = 64 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.bytes = 0
        self._lock = threading.Lock()
        self._store: OrderedDict[tuple[str, str], bytes] = OrderedDict()

    def get(self, key: tuple[str, str]) -> bytes | None:
        with self._lock:
            data = self._store.get(key)
            if data is not None:
                self._store.move_to_end(key)
            return data

    def put(self, key: tuple[str, str], data: bytes) -> None:
        with self._lock:
            if key in self._store:
                self.bytes -= len(self._store[key])
                del self._store[key]
            self._store[key] = data
            self.bytes += len(data)
            while self._store and (
                len(self._store) > self.max_entries or self.bytes > self.max_bytes
            ):
                _, evicted = self._store.popitem(last=False)
                self.bytes -= len(evicted)


_CACHE = _LRU()


def list_entries(url: str) -> list[str]:
    """Return the list of file names inside the remote zip at ``url``."""
    reader = HTTPRangeReader(url)
    with zipfile.ZipFile(reader) as zf:
        return zf.namelist()


def read_entry(url: str, entry: str, *, max_bytes: int = 5 * 1024 * 1024) -> bytes:
    """Return the raw bytes of ``entry`` inside the remote zip at ``url``.

    Caches results in an in-memory LRU keyed by ``(url, entry)``. Entries
    larger than ``max_bytes`` are still served but not cached, to keep the
    LRU bounded.
    """
    key = (url, entry)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    reader = HTTPRangeReader(url)
    with zipfile.ZipFile(reader) as zf:
        try:
            info = zf.getinfo(entry)
        except KeyError as exc:
            raise FileNotFoundError(f"{entry} not in zip {url}") from exc
        data = zf.read(info)

    if len(data) <= max_bytes:
        _CACHE.put(key, data)
    return data


def first_match(url: str, candidates: list[str]) -> tuple[str, bytes] | None:
    """Return the first ``(name, bytes)`` pair from ``candidates`` found in the zip.

    The whole namelist is fetched once (cheap — just the central directory),
    then we issue at most one ``read_entry`` call. Useful when the exact
    log/recipe filename varies (e.g. ``agent_<pkg>.log`` vs ``<pkg>.log``).
    """
    names = set(list_entries(url))
    for name in candidates:
        if name in names:
            return name, read_entry(url, name)
    return None
