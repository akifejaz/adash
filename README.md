# adash — Atesor AI Package Dashboard

A small, fully self-contained web dashboard that lets anyone browse and
download every package that **Atesor AI** has built for `riscv64`.

* **Source of truth:** GitHub Releases on
  [`akifejaz/atesor`](https://github.com/akifejaz/atesor/releases). Each
  monthly release is tagged `builds-YYYY-MM` and ships every successful
  build as a `<package>-<YYYYMMDD>-<HHMMSS>-<distro>.zip` asset.
* **Zero coupling to atesor-ai source:** the dashboard does not import
  anything from the agent codebase and does not read from any local
  workspace. Everything — package metadata, the porting recipe and the
  build/agent logs — is fetched from GitHub on demand.
* **Lightweight:** FastAPI server + a single static page (vanilla JS, no
  framework, no build step). Only two runtime dependencies:
  `fastapi` and `uvicorn`.

## Layout

```
adash/
├── dashboard/
│   ├── server.py         FastAPI app (UI + JSON + /pkg/{file}/recipe|log|manifest)
│   ├── github_source.py  Releases REST scanner; emits dashboard/manifest.json
│   ├── zip_reader.py     HTTP byte-range remote zip reader + in-memory LRU
│   ├── manifest.json     Generated; do not edit
│   └── static/           index.html · style.css · app.js
├── pyproject.toml
├── requirements.txt
├── LICENSE
└── README.md
```

## Run

```bash
git clone <this-repo> adash && cd adash
pip install -r requirements.txt
uvicorn dashboard.server:app --host 0.0.0.0 --port 8765
# open http://localhost:8765
```

The first request to `/api/manifest` fetches the releases list from
`api.github.com` and writes `dashboard/manifest.json`. The "⟳" button on
the top bar forces a refresh; the in-process cache lives for
`ATESOR_TTL_SECONDS` (default 10 minutes) so casual reloads don't hit
GitHub.

Optional one-shot CLI (warm or rebuild the manifest from the shell):

```bash
python -m dashboard.github_source --owner akifejaz --repo atesor
```

## Configuration

All settings are optional environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `ATESOR_GH_OWNER` | `akifejaz` | GitHub owner whose releases are surfaced |
| `ATESOR_GH_REPO`  | `atesor`   | GitHub repo whose releases are surfaced |
| `ATESOR_TTL_SECONDS` | `600` | Server-side manifest cache TTL |
| `GITHUB_TOKEN` / `GH_TOKEN` | – | Raises GitHub rate limit (60→5000/hr) |

When no token env var is set, the server falls back to
`gh auth token` so it transparently reuses an existing `gh` CLI login.

## How packages are discovered

`github_source.py` paginates
`GET /repos/{owner}/{repo}/releases`, keeps releases whose tag matches
`^builds-(\d{4})-(\d{2})$`, and parses every asset whose name matches
`^<pkg>-YYYYMMDD-HHMMSS-<distro>\.(zip|tar.gz|tgz|tar.xz|tar.bz2)$`.

For each asset the manifest carries: `name`, `distro`, `version`,
`build_date`, `size_bytes`, `download_count`, `release_tag`,
`release_url`, `download_url` (direct CDN URL), `log_url`, `recipe_url`.

## How logs and recipes are served

Each release zip contains, at its root:

```
build_recipe.md
manifest.json
agent_<pkg>.log
<pkg>.log
<pkg>/...           (the ported source tree)
```

When a user opens the **Recipe** or **logs** modal, the server uses
`zip_reader.HTTPRangeReader` to open the remote zip with HTTP
`Range:` byte requests and pull only the entry it needs (typically a
few KB). Repeat views are served from an in-memory LRU
(256 entries / 64 MB cap), so popular packages are effectively free.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/` | Single-page dashboard UI |
| GET | `/api/manifest` | Cached manifest JSON |
| POST | `/api/refresh` | Forces a GitHub re-fetch |
| GET | `/pkg/{filename}/recipe` | `build_recipe.md` from the zip |
| GET | `/pkg/{filename}/log?which=agent\|run&tail=N` | The matching log file |
| GET | `/pkg/{filename}/manifest` | Per-package `manifest.json` from the zip |

## UI features

* Release-tag selector (defaults to the newest month).
* Distro filter with colored pills (alpine = blue, debian = red, ubuntu = orange).
* Name search synced with the top-bar global search.
* Sortable columns: Package · Distro · Version · Size · Downloads · Release · Built.
* **Recipe** column with a dedicated `📋 recipe` button.
* **Actions** column: green `⬇ download` (direct GitHub CDN link) + `logs` modal (Agent log · Build log · Manifest tabs).
* Client-side pagination with 50 / 100 / 200 per page (persisted in `localStorage`).
* Dark / light theme toggle (persisted).
* `⟳` refresh button forces a fresh GitHub fetch.

## Failure handling

* If GitHub is unreachable, the server falls back to the persisted
  `dashboard/manifest.json` so the dashboard stays usable.
* Endpoints return `502` with a useful detail when a zip cannot be read
  and `404` when an asset filename is unknown.

## License

MIT — see [LICENSE](LICENSE).
