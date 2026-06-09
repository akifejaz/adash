"""Build dashboard manifest from GitHub Releases on ``akifejaz/atesor``.

The source of truth for compiled packages is the GitHub Releases page. Each
month is published as a release tag of the form ``builds-YYYY-MM``; every
asset on that release is a built package archive named
``<pkg>-YYYYMMDD-HHMMSS-<distro>.<ext>``.

This module fetches all matching releases via the GitHub REST API, parses the
asset names, and emits a manifest consumed by the dashboard UI. Anonymous
requests work for public repos (60/hr); set ``GITHUB_TOKEN`` to raise the
rate limit. ``gh auth token`` is consulted automatically when available.

CLI:
    python -m dashboard.github_source
    python -m dashboard.github_source --owner akifejaz --repo atesor
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_MANIFEST = Path(__file__).resolve().parent / "manifest.json"

DEFAULT_OWNER = os.environ.get("ATESOR_GH_OWNER", "akifejaz")
DEFAULT_REPO = os.environ.get("ATESOR_GH_REPO", "atesor")
RELEASE_TAG_RE = re.compile(r"^builds-(?P<year>\d{4})-(?P<month>\d{2})$")
ASSET_RE = re.compile(
    r"^(?P<name>.+?)-(?P<date>\d{8})-(?P<time>\d{6})-(?P<distro>[a-z0-9]+)"
    r"(?P<ext>\.zip|\.tar\.gz|\.tgz|\.tar\.xz|\.tar\.bz2)$",
    re.IGNORECASE,
)

API_BASE = "https://api.github.com"
USER_AGENT = "atesor-dashboard/1.0"


@dataclass
class PackageEntry:
    """One downloadable asset surfaced by the dashboard."""

    name: str
    version: str
    distro: str
    arch: str
    size_bytes: int
    build_date: str
    release_tag: str
    release_name: str
    release_url: str
    download_url: str
    download_count: int
    filename: str
    asset_id: int = 0
    log_url: str | None = None
    recipe_url: str | None = None
    categories: list[str] = field(default_factory=list)


def _resolve_token() -> str | None:
    """Return a GitHub token from env or ``gh auth token`` if available."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token.strip()
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _http_get_json(url: str, token: str | None) -> tuple[Any, dict[str, str]]:
    """GET ``url`` returning parsed JSON and response headers."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", USER_AGENT)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return body, headers


def _parse_next_link(link_header: str | None) -> str | None:
    """Extract the ``rel="next"`` URL from a GitHub ``Link`` header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().lstrip("<").rstrip(">")
        rel = section[1].strip()
        if rel == 'rel="next"':
            return url
    return None


def fetch_releases(
    owner: str, repo: str, token: str | None = None, per_page: int = 100
) -> list[dict[str, Any]]:
    """Return every release on ``owner/repo`` (handles pagination)."""
    url: str | None = f"{API_BASE}/repos/{owner}/{repo}/releases?per_page={per_page}"
    out: list[dict[str, Any]] = []
    while url:
        body, headers = _http_get_json(url, token)
        if not isinstance(body, list):
            raise RuntimeError(f"Unexpected GitHub response: {body!r}")
        out.extend(body)
        url = _parse_next_link(headers.get("link"))
    return out


def _zip_log_candidates(repo: str) -> list[str]:
    """Return the zip entry names where the per-package log may live."""
    return [f"agent_{repo}.log", f"{repo}.log"]


def _zip_recipe_candidates() -> list[str]:
    """Return the zip entry names where the porting recipe may live."""
    return ["build_recipe.md", "BUILD_RECIPE.md", "recipe.md"]


def _format_tag(tag: str) -> str:
    """Render ``builds-2026-06`` as ``2026.06`` for the UI selector."""
    m = RELEASE_TAG_RE.match(tag)
    return f"{m['year']}.{m['month']}" if m else tag


def build_manifest(owner: str, repo: str, token: str | None = None) -> dict[str, Any]:
    """Fetch releases and assemble the dashboard manifest payload."""
    releases = fetch_releases(owner, repo, token)
    LOGGER.info("Fetched %d release(s) from %s/%s", len(releases), owner, repo)

    entries: list[PackageEntry] = []
    skipped = 0
    for release in releases:
        tag = release.get("tag_name") or ""
        if not RELEASE_TAG_RE.match(tag):
            continue
        release_name = release.get("name") or tag
        release_url = release.get("html_url") or ""
        display_tag = _format_tag(tag)

        for asset in release.get("assets", []) or []:
            asset_name = asset.get("name") or ""
            match = ASSET_RE.match(asset_name)
            if not match:
                skipped += 1
                continue
            date = match.group("date")
            t = match.group("time")
            try:
                build_dt = datetime.strptime(date + t, "%Y%m%d%H%M%S").replace(
                    tzinfo=timezone.utc
                )
                build_iso = build_dt.isoformat()
            except ValueError:
                build_iso = f"{date}T{t}Z"

            name = match.group("name")
            distro = match.group("distro").lower()
            entries.append(
                PackageEntry(
                    name=name,
                    version=f"{date}-{t}",
                    distro=distro,
                    arch="riscv64",
                    size_bytes=int(asset.get("size", 0) or 0),
                    build_date=build_iso,
                    release_tag=display_tag,
                    release_name=release_name,
                    release_url=release_url,
                    download_url=asset.get("browser_download_url", ""),
                    download_count=int(asset.get("download_count", 0) or 0),
                    filename=asset_name,
                    asset_id=int(asset.get("id", 0) or 0),
                    log_url=f"/pkg/{asset_name}/log",
                    recipe_url=f"/pkg/{asset_name}/recipe",
                    categories=[distro],
                )
            )

    release_tags = sorted({e.release_tag for e in entries}, reverse=True)
    LOGGER.info(
        "Indexed %d package asset(s); skipped %d non-matching asset(s)",
        len(entries),
        skipped,
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {"owner": owner, "repo": repo},
        "package_count": len(entries),
        "releases": release_tags,
        "latest_release": release_tags[0] if release_tags else None,
        "distros": sorted({e.distro for e in entries}),
        "packages": [asdict(e) for e in entries],
    }


def build_manifest_with_retry(
    owner: str, repo: str, token: str | None = None, attempts: int = 3
) -> dict[str, Any]:
    """Wrap :func:`build_manifest` with simple exponential back-off."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return build_manifest(owner, repo, token)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in {403, 429, 500, 502, 503, 504}:
                wait = 2 ** i
                LOGGER.warning("HTTP %s from GitHub; retrying in %ds", exc.code, wait)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            time.sleep(2 ** i)
    raise RuntimeError(f"GitHub fetch failed after {attempts} attempts: {last_exc}")


def main() -> None:
    """CLI entry point — writes manifest.json to disk."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--output", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub token (defaults to $GITHUB_TOKEN or `gh auth token`).",
    )
    args = parser.parse_args()

    token = args.token or _resolve_token()
    manifest = build_manifest_with_retry(args.owner, args.repo, token)
    args.output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    LOGGER.info(
        "Wrote %d packages across %d release(s) to %s",
        manifest["package_count"],
        len(manifest["releases"]),
        args.output,
    )


if __name__ == "__main__":
    main()
