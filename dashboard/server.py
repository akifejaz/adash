"""FastAPI server for the Atesor AI package dashboard.

Packages are sourced from GitHub Releases on ``akifejaz/atesor`` (release tags
``builds-YYYY-MM``). The porting recipe (``build_recipe.md``) and the agent
log (``agent_<pkg>.log``) live inside each release zip — we read them on
demand over HTTP byte-range requests instead of downloading the full archive.

Endpoints:

* ``GET /``                       – single-page dashboard UI.
* ``GET /api/manifest``           – cached manifest JSON.
* ``POST /api/refresh``           – force-refresh from GitHub.
* ``GET /pkg/{filename}/recipe``  – ``build_recipe.md`` from the zip.
* ``GET /pkg/{filename}/log``     – ``agent_<pkg>.log`` (or fallback ``<pkg>.log``).
* ``GET /pkg/{filename}/manifest``– per-package ``manifest.json`` from the zip.

Configuration (env vars):

* ``ATESOR_GH_OWNER`` (default ``akifejaz``)
* ``ATESOR_GH_REPO``  (default ``atesor``)
* ``ATESOR_TTL_SECONDS`` (default ``600``)
* ``GITHUB_TOKEN`` / ``GH_TOKEN`` – optional, raises rate limit.

Run with::

    uvicorn dashboard.server:app --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import github_source as gh
from . import zip_reader

LOGGER = logging.getLogger(__name__)

MANIFEST_PATH = gh.DEFAULT_MANIFEST
STATIC_DIR = Path(__file__).resolve().parent / "static"

OWNER = gh.DEFAULT_OWNER
REPO = gh.DEFAULT_REPO
TTL_SECONDS = int(os.environ.get("ATESOR_TTL_SECONDS", "600"))

# zlib-20260604-132432-debian.zip → pkg name "zlib"
ASSET_FILENAME_RE = re.compile(
    r"^(?P<name>.+?)-\d{8}-\d{6}-[a-z0-9]+(?:\.zip|\.tar\.gz|\.tgz|\.tar\.xz|\.tar\.bz2)$",
    re.IGNORECASE,
)

app = FastAPI(title="Atesor AI Packages", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"manifest": None, "fetched_at": 0.0, "by_filename": {}}


def _load_persisted() -> dict[str, Any] | None:
    """Return the on-disk manifest if it exists, otherwise ``None``."""
    if not MANIFEST_PATH.exists():
        return None
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        LOGGER.warning("Failed to read cached manifest: %s", exc)
        return None


def _index_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build a ``filename → package entry`` lookup table."""
    return {p["filename"]: p for p in manifest.get("packages", [])}


def _refresh(force: bool = False) -> dict[str, Any]:
    """Return a fresh-enough manifest, fetching from GitHub when needed."""
    with _cache_lock:
        now = time.time()
        cached = _cache["manifest"]
        if not force and cached and now - _cache["fetched_at"] < TTL_SECONDS:
            return cached
        if not force and cached is None:
            persisted = _load_persisted()
            if persisted:
                _cache["manifest"] = persisted
                _cache["fetched_at"] = now
                _cache["by_filename"] = _index_manifest(persisted)
                return persisted

        token = gh._resolve_token()
        try:
            manifest = gh.build_manifest_with_retry(OWNER, REPO, token)
        except Exception as exc:  # noqa: BLE001 - reported to client
            if cached:
                LOGGER.warning("GitHub fetch failed (%s); serving stale cache", exc)
                return cached
            persisted = _load_persisted()
            if persisted:
                LOGGER.warning(
                    "GitHub fetch failed (%s); serving on-disk manifest", exc
                )
                _cache["manifest"] = persisted
                _cache["fetched_at"] = now
                _cache["by_filename"] = _index_manifest(persisted)
                return persisted
            raise HTTPException(
                status_code=502, detail=f"GitHub fetch failed: {exc}"
            ) from exc

        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _cache["manifest"] = manifest
        _cache["fetched_at"] = now
        _cache["by_filename"] = _index_manifest(manifest)
        return manifest


def _resolve_asset(filename: str) -> tuple[str, str]:
    """Map an asset filename to its ``(download_url, package_name)``.

    Raises 404 if the file is not present in the current manifest.
    """
    _refresh(force=False)
    entry = _cache["by_filename"].get(filename)
    if not entry:
        # Stale cache: try one forced refresh before giving up.
        _refresh(force=True)
        entry = _cache["by_filename"].get(filename)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Unknown package: {filename}")

    match = ASSET_FILENAME_RE.match(filename)
    pkg = entry.get("name") or (match.group("name") if match else filename)
    return entry["download_url"], pkg


def _serve_entry(filename: str, candidates: list[str], media_type: str):
    """Fetch the first matching entry from the asset's remote zip."""
    url, _ = _resolve_asset(filename)
    try:
        result = zip_reader.first_match(url, candidates)
    except Exception as exc:  # noqa: BLE001 - surfaced to client
        raise HTTPException(
            status_code=502, detail=f"Failed to read zip: {exc}"
        ) from exc
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"None of {candidates} found inside {filename}",
        )
    _, data = result
    return PlainTextResponse(
        data.decode("utf-8", errors="replace"), media_type=media_type
    )


# Routes ---------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Serve the dashboard SPA."""
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/manifest")
def api_manifest() -> JSONResponse:
    """Return the cached manifest, refreshing from GitHub if stale."""
    return JSONResponse(_refresh(force=False))


@app.post("/api/refresh")
def api_refresh() -> JSONResponse:
    """Force a refresh from GitHub Releases."""
    manifest = _refresh(force=True)
    return JSONResponse(
        {
            "ok": True,
            "package_count": manifest["package_count"],
            "generated_at": manifest["generated_at"],
        }
    )


@app.get("/pkg/{filename}/recipe", response_class=PlainTextResponse)
def pkg_recipe(filename: str) -> PlainTextResponse:
    """Return ``build_recipe.md`` extracted from the release zip."""
    return _serve_entry(
        filename, gh._zip_recipe_candidates(), media_type="text/markdown"
    )


@app.get("/pkg/{filename}/log", response_class=PlainTextResponse)
def pkg_log(
    filename: str,
    which: Literal["agent", "run"] = Query("agent"),
    tail: int = Query(0, ge=0, le=100_000),
) -> PlainTextResponse:
    """Return the per-package log extracted from the release zip.

    ``which=agent`` prefers the long agent trace; ``which=run`` prefers the
    short build-run log. We always fall back to whichever is actually present.
    """
    _, pkg = _resolve_asset(filename)
    if which == "agent":
        candidates = [f"agent_{pkg}.log", f"{pkg}.log"]
    else:
        candidates = [f"{pkg}.log", f"agent_{pkg}.log"]
    response = _serve_entry(filename, candidates, media_type="text/plain")
    if tail and isinstance(response, PlainTextResponse):
        text = response.body.decode("utf-8", errors="replace")
        text = "\n".join(text.splitlines()[-tail:])
        return PlainTextResponse(text, media_type="text/plain")
    return response


@app.get("/pkg/{filename}/manifest", response_class=PlainTextResponse)
def pkg_manifest(filename: str) -> PlainTextResponse:
    """Return the per-package ``manifest.json`` shipped inside the zip."""
    return _serve_entry(filename, ["manifest.json"], media_type="application/json")
