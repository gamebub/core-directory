# Game Bub Core Directory

Automated directory and static website for community-developed FPGA cores for [Game Bub](https://gamebub.net/) (`cores.gamebub.net`).

## How It Works

1. `repos.toml` contains the curated list of GitHub repositories to track.
2. Every 30 minutes, `.github/workflows/update.yml` runs `scripts/sync.py`:
   - Resolves each repository to its canonical GitHub `repo_id` and `full_name` (so repository renames or transfers do not break caching or split entries).
   - Walks non-draft, non-prerelease GitHub Releases newest-first to find each repo's latest valid release.
   - Downloads unseen or updated `.zip` release assets (cached incrementally in `data/cache.json` by GitHub's globally unique `asset_id` and update timestamp).
   - Validates archive packaging layout, `core.json` metadata, and referenced `.bit` files.
3. `scripts/build.py` aggregates active cores by `(repo_id, core_id)` from each repo's latest valid stable release and builds the static site (`site/index.html`, `site/cores.json`, `site/style.css`, `site/app.js`).

## Packaging Requirements for Core Releases

To be picked up automatically by the directory, publish a non-draft, non-prerelease GitHub Release with a `.zip` asset containing one or more cores in one of these strict root layouts:

- `cores/<CoreID>/core.json` (recommended — extracts straight onto the microSD card root)
- `<CoreID>/core.json`

Where:
- Each `.zip` asset is at most 64 MiB and each `core.json` is at most 256 KiB.
- `metadata.id` in `core.json` matches `<CoreID>` exactly, uses `Author.CoreName` format (must contain a `.` in the middle, no leading/trailing `.` or `..`, not `Demo.DemoCore`, and `Author` must not match `Game.*Bub`), and is at most 32 UTF-8 bytes.
- `metadata.name` and `metadata.author` are non-empty strings (max 32 UTF-8 bytes).
- `metadata.version` (non-empty string, max 16 UTF-8 bytes) and `min_firmware` are required for releases on or after `2026-11-23T00:00:00Z` (optional for legacy releases before the cutoff).
- Optional fields (`metadata.description` max 256 UTF-8 bytes, `metadata.url` valid `http(s)` URL max 128 UTF-8 bytes, `metadata.release_date` valid `YYYY-MM-DD` date, `metadata.license` max 32 UTF-8 bytes) must pass validation if present; cores failing validation are logged as warnings and excluded.
- `bitstreams` contains 1–16 entries; each `target` is non-empty (max 16 UTF-8 bytes) and each `filename` ends with `.bit` (max 32 UTF-8 bytes) and exists inside `<CoreID>/` in the `.zip`.
- Drafts and pre-releases (`prerelease: true` on GitHub) are ignored.
- `site/index.html` displays cores supporting `default_target` from `config.toml` (`gamebub_rev4`), while `site/cores.json` includes all valid targets.

*Legacy cutoff:* Releases published before `2026-11-23T00:00:00Z` are grandfathered to allow nested paths such as `<bundle>/sdcard/cores/<CoreID>/core.json`. All releases published on or after `2026-11-23T00:00:00Z` must use the strict root layout above.

Multiple cores can be packaged in the same `.zip` (e.g., `cores/Author.CoreA/` and `cores/Author.CoreB/`). Each `(repo_id, core_id)` pair gets its own row.

## Submitting a Core

Open a Pull Request adding your repository to `repos.toml` (in alphabetical order):

```toml
[[repos]]
repo = "owner/repo-name"
```

If your GitHub Releases contain extra `.zip` assets you don't want scanned (such as test builds or hardware-specific splits), you can optionally filter `.zip` filenames using regexes:

```toml
[[repos]]
repo = "owner/repo-name"
assets = ['^my-core-.*\.zip$']
```

## Running Locally

Requires Python 3.11+ and Jinja2:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional: set GH_TOKEN to avoid GitHub's 60 req/hr unauthenticated limit
export GH_TOKEN=...

python3 scripts/sync.py
python3 scripts/build.py
python3 -m http.server -d site 8000
```

Run tests:

```sh
python3 -m unittest discover -s tests -v
```
