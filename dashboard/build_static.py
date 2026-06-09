"""Pre-extract per-package recipe/log/manifest files for the static site.

The GitHub Pages build cannot read release-zip entries from the browser:
``api.github.com``'s ``Accept: application/octet-stream`` endpoint redirects
to ``objects.githubusercontent.com`` which does not set CORS headers, so
``fetch`` from a Pages origin always fails with ``Failed to fetch``.

Workaround: do the zip reading server-side at build time and ship the
extracted text alongside the manifest::

    site/
      manifest.json
      pkg/<asset-filename>/
        recipe.md
        log-agent.txt
        log-run.txt
        manifest.json

The SPA in static mode just fetches those URLs directly.

Usage::

    python -m dashboard.build_static --manifest site/manifest.json --out site
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import zip_reader

LOGGER = logging.getLogger(__name__)

RECIPE_NAMES = ["build_recipe.md", "BUILD_RECIPE.md", "recipe.md"]
PKG_MANIFEST_NAMES = ["manifest.json"]
MAX_LOG_LINES = 2000


def _tail(text: str, n: int) -> str:
    """Return the last ``n`` lines of ``text`` (or all of it if shorter)."""
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if len(lines) > n else text


def _extract_from_zip(
    zf: zipfile.ZipFile, names_in_zip: set[str], candidates: list[str]
) -> bytes | None:
    """Return the bytes of the first matching candidate, or ``None``."""
    for cand in candidates:
        if cand in names_in_zip:
            try:
                return zf.read(cand)
            except Exception as exc:  # noqa: BLE001 - best-effort
                LOGGER.warning("read %s failed: %s", cand, exc)
                return None
    return None


def _write_or_placeholder(
    target: Path,
    data: bytes | None,
    candidates: list[str],
    *,
    tail_lines: int | None = None,
) -> bool:
    """Write ``data`` to ``target``, or a placeholder if missing.

    Returns ``True`` if real bytes were written.
    """
    if data is None:
        target.write_text(
            f"(not present in this package; tried: {', '.join(candidates)})\n",
            encoding="utf-8",
        )
        return False
    text = data.decode("utf-8", errors="replace")
    if tail_lines is not None:
        text = _tail(text, tail_lines)
    target.write_text(text, encoding="utf-8")
    return True


def _failure_placeholders(pkg_dir: Path, exc: Exception) -> None:
    """Write placeholder files so the SPA still has something to display."""
    msg = f"(failed to open release zip: {exc})\n"
    (pkg_dir / "recipe.md").write_text(msg, encoding="utf-8")
    (pkg_dir / "log-agent.txt").write_text(msg, encoding="utf-8")
    (pkg_dir / "log-run.txt").write_text(msg, encoding="utf-8")
    (pkg_dir / "manifest.json").write_text(
        json.dumps({"error": str(exc)}, indent=2), encoding="utf-8"
    )


def _extract_one(pkg: dict[str, Any], out_root: Path) -> dict[str, Any]:
    """Pull recipe/log/manifest out of one release zip into ``site/pkg/<file>/``.

    Opens the zip once and reads all four entries through the same
    :class:`zip_reader.HTTPRangeReader`, so the central directory is fetched
    only once per package instead of once per entry.
    """
    filename = pkg["filename"]
    url = pkg["download_url"]
    name = pkg["name"]

    log_agent_candidates = [f"agent_{name}.log", f"{name}.log"]
    log_run_candidates = [f"{name}.log", f"agent_{name}.log"]

    pkg_dir = out_root / "pkg" / filename
    pkg_dir.mkdir(parents=True, exist_ok=True)

    try:
        reader = zip_reader.HTTPRangeReader(url)
        with zipfile.ZipFile(reader) as zf:
            names_in_zip = set(zf.namelist())
            recipe = _extract_from_zip(zf, names_in_zip, RECIPE_NAMES)
            log_agent = _extract_from_zip(zf, names_in_zip, log_agent_candidates)
            log_run = _extract_from_zip(zf, names_in_zip, log_run_candidates)
            pkg_manifest = _extract_from_zip(zf, names_in_zip, PKG_MANIFEST_NAMES)
    except Exception as exc:  # noqa: BLE001 - report and move on
        LOGGER.warning("%s: failed to open zip: %s", filename, exc)
        _failure_placeholders(pkg_dir, exc)
        return {"filename": filename, "ok": False, "error": str(exc)}

    _write_or_placeholder(pkg_dir / "recipe.md", recipe, RECIPE_NAMES)
    _write_or_placeholder(
        pkg_dir / "log-agent.txt",
        log_agent,
        log_agent_candidates,
        tail_lines=MAX_LOG_LINES,
    )
    _write_or_placeholder(
        pkg_dir / "log-run.txt",
        log_run,
        log_run_candidates,
        tail_lines=MAX_LOG_LINES,
    )
    _write_or_placeholder(pkg_dir / "manifest.json", pkg_manifest, PKG_MANIFEST_NAMES)
    return {"filename": filename, "ok": True}


def build_static(manifest_path: Path, out_root: Path, workers: int = 8) -> int:
    """Extract entries for every package listed in ``manifest_path``.

    Returns the number of packages that failed to open at all.
    """
    manifest = json.loads(manifest_path.read_text("utf-8"))
    packages = manifest.get("packages", [])
    LOGGER.info(
        "Extracting recipe/log/manifest for %d package(s) with %d worker(s)",
        len(packages),
        workers,
    )
    if not packages:
        return 0

    failures = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_extract_one, p, out_root): p["filename"] for p in packages}
        for fut in as_completed(futures):
            res = fut.result()
            done += 1
            if not res.get("ok"):
                failures += 1
            if done % 25 == 0 or done == len(packages):
                LOGGER.info("Progress: %d/%d (%d failed)", done, len(packages), failures)
    LOGGER.info("Done. %d/%d packages failed to open.", failures, len(packages))
    return failures


def main() -> None:
    """CLI entry point used by the Pages workflow."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    failures = build_static(args.manifest, args.out, workers=args.workers)
    # Hard-fail only if *every* package failed (probably auth/network).
    manifest = json.loads(args.manifest.read_text("utf-8"))
    total = len(manifest.get("packages", []))
    if total and failures == total:
        LOGGER.error("All %d packages failed to open; aborting deploy.", total)
        sys.exit(1)


if __name__ == "__main__":
    main()
