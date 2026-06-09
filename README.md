# adash - Atesor AI Package Dashboard

A small web dashboard for browsing and downloading every package that
**Atesor AI** has built for `riscv64`.

It can run two ways:

- **Hosted on GitHub Pages** as a fully static site. A GitHub Actions
  workflow regenerates the manifest on a schedule and, for every
  package, pre-extracts the recipe, the agent log, the build log, and
  the in-zip manifest from each release zip and writes them as plain
  files alongside the SPA. The browser just fetches those files - no
  backend, no API calls at view time, no CORS surprises.
- **As a local FastAPI app** for development. Same UI, but the manifest
  and zip reads happen on the Python side, on demand.

The source of truth in both cases is GitHub Releases on
[`akifejaz/atesor`](https://github.com/akifejaz/atesor/releases). Each
monthly release is tagged `builds-YYYY-MM` and ships every successful
build as a `<package>-<YYYYMMDD>-<HHMMSS>-<distro>.zip` asset.

## Layout

```
adash/
├── .github/workflows/
│   └── pages.yml             Builds manifest + deploys to GitHub Pages
├── dashboard/
│   ├── server.py             FastAPI app (local dev)
│   ├── github_source.py      Releases REST scanner; emits manifest.json
│   ├── build_static.py       Pre-extracts recipe/log/manifest per package for Pages
│   ├── zip_reader.py         HTTP byte-range remote zip reader
│   ├── manifest.json         Generated; do not edit
│   └── static/               index.html, style.css, app.js
├── pyproject.toml
├── requirements.txt
├── LICENSE
└── README.md
```

## Deploy to GitHub Pages

One-time setup:

1. Push this repository to GitHub.
2. Open **Settings → Pages**.
3. Under **Source**, pick **GitHub Actions** .
4. Either push a commit to `main` or trigger the workflow manually.

Once the workflow finishes, your site is live at
`https://<user>.github.io/<repo>/`. The workflow re-runs every hour, so
new releases show up without any manual action.

In this mode the page detects there is no backend and loads
`./manifest.json` directly. Recipe and log modals just fetch
pre-extracted files at `./pkg/<asset-filename>/recipe.md`,
`log-agent.txt`, `log-run.txt`, and `manifest.json`.

Why pre-extract? Reading entries from a release zip in the browser
sounds nice, but GitHub's asset endpoint redirects to
`objects.githubusercontent.com`, which does not send CORS headers, so
the browser refuses the response with `Failed to fetch`. Doing the zip
reading at build time (where Python can follow redirects freely) sides
steps that entirely.

Build cost: extracting recipe, two logs, and the in-zip manifest for
~300 packages takes a few minutes inside the Action and adds a few
tens of megabytes to the site. Logs are tailed to the last 2000 lines
to keep size reasonable. The workflow uses the action's built-in
`GITHUB_TOKEN`, so all GitHub API calls are authenticated.

## Run locally

```bash
git clone <this-repo> adash && cd adash
pip install -r requirements.txt
uvicorn dashboard.server:app --host 0.0.0.0 --port 8765
# open http://localhost:8765
```

The first request to `/api/manifest` fetches the releases list from
`api.github.com` and writes `dashboard/manifest.json`. The refresh
button on the top bar forces a re-fetch; the in-process cache lives for
`ATESOR_TTL_SECONDS` (default 10 minutes).

Rebuild the manifest from the shell without starting the server:

```bash
python -m dashboard.github_source --owner akifejaz --repo atesor
```

## Configuration

All settings are optional environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `ATESOR_GH_OWNER` | `akifejaz` | GitHub owner whose releases are surfaced |
| `ATESOR_GH_REPO`  | `atesor`   | GitHub repo whose releases are surfaced |
| `ATESOR_TTL_SECONDS` | `600` | Server-side manifest cache TTL (local mode) |
| `GITHUB_TOKEN` / `GH_TOKEN` | - | Raises GitHub rate limit (60 to 5000/hr) |

When no token env var is set, the local server falls back to
`gh auth token` so it transparently reuses an existing `gh` CLI login.
The Pages workflow uses the action's built-in `GITHUB_TOKEN` so the
manifest build always runs authenticated.

## How packages are discovered

`github_source.py` paginates
`GET /repos/{owner}/{repo}/releases`, keeps releases whose tag matches
`^builds-(\d{4})-(\d{2})$`, and parses every asset whose name matches
`^<pkg>-YYYYMMDD-HHMMSS-<distro>\.(zip|tar.gz|tgz|tar.xz|tar.bz2)$`.

For each asset the manifest carries: `name`, `distro`, `version`,
`build_date`, `size_bytes`, `download_count`, `release_tag`,
`release_url`, `download_url` (direct CDN URL), `asset_id` (used by the
in-browser zip reader), `log_url`, `recipe_url`.

## How logs and recipes are served

Each release zip contains, at its root:

```
build_recipe.md
manifest.json
agent_<pkg>.log
<pkg>.log
<pkg>/...           (the ported source tree)
```

- **Local server:** `zip_reader.HTTPRangeReader` opens the remote zip
  over HTTP `Range:` byte requests and pulls only the entry it needs
  (typically a few KB). Repeat views are served from an in-memory LRU
  (256 entries / 64 MB cap).
- **GitHub Pages:** `dashboard/build_static.py` does the same range
  reads at build time, once per zip, and writes the extracted entries
  into `site/pkg/<filename>/`. The SPA just fetches those static files
  at view time.

## Local-mode endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/` | Single-page dashboard UI |
| GET | `/api/manifest` | Cached manifest JSON |
| POST | `/api/refresh` | Forces a GitHub re-fetch |
| GET | `/pkg/{filename}/recipe` | `build_recipe.md` from the zip |
| GET | `/pkg/{filename}/log?which=agent\|run&tail=N` | The matching log file |
| GET | `/pkg/{filename}/manifest` | Per-package `manifest.json` from the zip |

These exist only when you are running `uvicorn` locally. On GitHub
Pages there is no backend; the page talks straight to GitHub.

## UI features

- Release-tag selector (defaults to the newest month).
- Distro filter with colored pills.
- Search box matches name, filename, version, distro, and release tag.
  When the search box is non-empty it ignores the release/distro
  dropdowns so matches in other releases still show up.
- Sortable columns: Package, Distro, Version, Size, Downloads, Release, Built.
- Recipe column with its own button.
- Actions column: direct GitHub download link and a logs modal
  (Agent log, Build log, Manifest tabs).
- Client-side pagination (50 / 100 / 200 per page, persisted in `localStorage`).
- Dark / light theme toggle (persisted).

## Failure handling

- Local mode: if GitHub is unreachable, the server falls back to the
  persisted `dashboard/manifest.json` so the dashboard stays usable.
  Endpoints return `502` with a useful detail when a zip cannot be read
  and `404` when an asset filename is unknown.
- Pages mode: if `manifest.json` cannot be loaded, the table shows the
  underlying fetch error. If a package's release zip could not be
  opened at build time, its recipe/log files contain a short
  `(failed to open release zip: ...)` placeholder rather than a 404,
  so the modal still works.

## License

MIT - see [LICENSE](LICENSE).
