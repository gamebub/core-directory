# Game Bub Core Directory

Directory of community-developed FPGA cores for [Game Bub](https://gamebub.net/). Available from [cores.gamebub.net](https://cores.gamebub.net/).

## Submitting a Core

Open a PR adding your repository to `repos.toml` (maintain alphabetical order):

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

## Packaging Requirements for Core Releases

To be picked up automatically by the directory, publish a GitHub Release with a `.zip` asset containing one or more cores.

The ZIP file must have one of these structures:

* `<CoreID>/core.json` (ideal for one core)
* `cores/<CoreID>/core.json` (required for multiple cores)

Additionally, the core metadata must pass some basic validation:

* `metadata.id` in `core.json` matches `<CoreID>` exactly and uses `Author.CoreName` format
* `metadata.name`, `metadata.author`, and `metadata.version` are all present
* `metadata.min_firmware` is present and valid
* Optional fields, if present, must be valid:
  * `metadata.description` max 256 bytes
  * `metadata.url` max 256 bytes, valid URL
  * `metadata.release_data` a valid `YYYY-MM-DD` date
  * `metadata.license` max 32 bytes
* Drafts and pre-releases are ignored

## How It Works

1. `repos.toml` contains the curated list of repositories to track
2. Every 30 minutes, a GitHub action runs `scripts/sync.py`
   * Resolves each repository
   * Finds the latest valid release for each repository
   * Downloads release `.zip` files, validates them, and adds them to the database
3. Builds a static site with the latest releases for each core
