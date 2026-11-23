#!/usr/bin/env python3
"""Sync GitHub releases for curated Game Bub core repositories.

Resolves each repository to its immutable GitHub repo_id, scans non-draft
stable releases newest-first for the latest valid release, downloads unseen or
updated .zip assets, validates strict layout and core.json metadata (with a
date-based legacy layout grace period for older releases), and writes an
incremental cache to data/cache.json.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
import posixpath
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
REPOS_PATH = ROOT_DIR / "repos.toml"
CACHE_PATH = ROOT_DIR / "data" / "cache.json"

CACHE_SCHEMA_VERSION = 2
# TODO: Remove LEGACY_RELEASE_CUTOFF / allow_legacy once existing pre-cutoff repos
# publish updated releases using the strict root layout, metadata.version, and min_firmware.
# Releases published strictly before this UTC timestamp may use nested legacy zip paths
# (e.g. <bundle>/sdcard/cores/<id>/core.json) and omit metadata.version / min_firmware.
# Releases at or after this cutoff must use strict root layout and include both fields.
LEGACY_RELEASE_CUTOFF = "2026-11-23T00:00:00Z"

MAX_ZIP_BYTES = 64 * 1024 * 1024  # 64 MiB safety cap per asset
MAX_CORE_JSON_BYTES = 256 * 1024  # 256 KiB safety cap per core.json
MAX_BITSTREAMS = 16
MAX_DESCRIPTION_BYTES = 256
MAX_URL_BYTES = 128
TEMPLATE_CORE_IDS = {"Demo.DemoCore"}
RESERVED_AUTHOR_ID_RE = re.compile(r"game.*bub", re.IGNORECASE)

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MIN_FIRMWARE_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:-[a-z]+\.?\d+)?$", re.IGNORECASE)
RELEASE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')

FETCH_ERRORS = (
    urllib.error.HTTPError,
    urllib.error.URLError,
    http.client.HTTPException,
    TimeoutError,
    OSError,
    ValueError,
)


class AssetTooLargeError(ValueError):
    """Raised when a streamed release asset exceeds MAX_ZIP_BYTES."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_repos(path: Path = REPOS_PATH) -> list[dict[str, Any]]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("repos"), list):
        raise ValueError(f"{path}: expected top-level table with 'repos' array")

    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for idx, entry in enumerate(data["repos"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("repo"), str):
            raise ValueError(f"{path}: repos[{idx}] must be an object with string 'repo'")
        repo = entry["repo"].strip()
        if not REPO_RE.match(repo):
            raise ValueError(f"{path}: repos[{idx}] invalid repo name {repo!r}")
        owner, repo_name = repo.split("/", 1)
        if not is_safe_segment(owner) or not is_safe_segment(repo_name):
            raise ValueError(f"{path}: repos[{idx}] invalid repo name {repo!r}")
        key = repo.lower()
        if key in seen:
            raise ValueError(f"{path}: duplicate repo entry {repo!r}")
        seen.add(key)

        assets = entry.get("assets")
        if assets is not None:
            if not isinstance(assets, list) or not all(isinstance(p, str) for p in assets):
                raise ValueError(f"{path}: repos[{idx}] 'assets' must be a list of regex strings")
            for pattern in assets:
                re.compile(pattern)

        entries.append(
            {
                "repo": repo,
                "assets": assets,
            }
        )
    return entries


def empty_cache() -> dict[str, Any]:
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "legacy_release_cutoff": LEGACY_RELEASE_CUTOFF,
        "repos": {},
        "assets": {},
    }


def load_cache(path: Path = CACHE_PATH) -> dict[str, Any]:
    if not path.exists():
        return empty_cache()
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("schema_version") != CACHE_SCHEMA_VERSION:
        return empty_cache()
    repos = data.get("repos")
    assets = data.get("assets")
    if not isinstance(repos, dict) or not isinstance(assets, dict):
        return empty_cache()
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "legacy_release_cutoff": LEGACY_RELEASE_CUTOFF,
        "repos": repos,
        "assets": assets,
    }


def save_cache(cache: dict[str, Any], path: Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_repos = dict(sorted(cache["repos"].items(), key=lambda kv: int(kv[0])))
    sorted_assets = dict(sorted(cache["assets"].items(), key=lambda kv: int(kv[0])))
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "legacy_release_cutoff": LEGACY_RELEASE_CUTOFF,
        "repos": sorted_repos,
        "assets": sorted_assets,
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")


def is_safe_segment(segment: str) -> bool:
    if not segment or segment in (".", ".."):
        return False
    return "/" not in segment and "\\" not in segment and "\x00" not in segment


def is_safe_relpath(relpath: str) -> bool:
    if not relpath or relpath.startswith("/") or "\\" in relpath or "\x00" in relpath:
        return False
    parts = relpath.split("/")
    return all(is_safe_segment(p) for p in parts)


def normalize_zip_path(name: str) -> str | None:
    cleaned = name.replace("\\", "/")
    if not cleaned or cleaned.endswith("/"):
        return None
    parts = [p for p in cleaned.split("/") if p and p != "."]
    if (
        not parts
        or ".." in parts
        or "__MACOSX" in parts
        or any(p.startswith("._") for p in parts)
    ):
        return None
    return "/".join(parts)


def is_legacy_release(published_at: str) -> bool:
    return bool(published_at) and published_at < LEGACY_RELEASE_CUTOFF


def utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def is_valid_core_id(core_id: str) -> bool:
    if not core_id or utf8_len(core_id) > 32 or not is_safe_segment(core_id):
        return False
    if "." not in core_id or core_id.startswith(".") or core_id.endswith(".") or ".." in core_id:
        return False
    return True


def validate_core_json(
    raw_bytes: bytes,
    folder_name: str,
    core_dir: str,
    zip_files: set[str],
    *,
    require_version: bool = False,
    require_min_firmware: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate core.json against Game Bub firmware rules and zip contents.

    Returns (normalized_core_dict, None) on success, or (None, error_reason).
    """
    try:
        doc = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, f"invalid JSON: {e}"

    if not isinstance(doc, dict):
        return None, "top-level JSON must be an object"

    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return None, "missing 'metadata' object"

    core_id = metadata.get("id")
    name = metadata.get("name")
    author = metadata.get("author")

    if not isinstance(core_id, str) or not is_valid_core_id(core_id):
        return None, f"invalid metadata.id {core_id!r} (must be Author.CoreName, <=32 bytes)"
    if core_id in TEMPLATE_CORE_IDS:
        return None, f"ignoring template core ID {core_id!r}"
    author_id = core_id.split(".", 1)[0]
    if RESERVED_AUTHOR_ID_RE.search(author_id):
        return None, f"reserved author ID {author_id!r} in metadata.id {core_id!r}"
    if core_id != folder_name:
        return None, f"metadata.id {core_id!r} does not match folder {folder_name!r}"

    if not isinstance(name, str) or not name.strip() or utf8_len(name) > 32:
        return None, f"invalid metadata.name {name!r}"
    if not isinstance(author, str) or not author.strip() or utf8_len(author) > 32:
        return None, f"invalid metadata.author {author!r}"

    min_firmware = doc.get("min_firmware")
    if min_firmware is None:
        if require_min_firmware:
            return None, "missing required min_firmware"
    else:
        if not isinstance(min_firmware, str) or not MIN_FIRMWARE_RE.match(min_firmware.strip()):
            return None, f"invalid min_firmware {min_firmware!r}"
        min_firmware = min_firmware.strip()

    bitstreams = doc.get("bitstreams")
    if not isinstance(bitstreams, list) or not bitstreams or len(bitstreams) > MAX_BITSTREAMS:
        return None, "missing or invalid 'bitstreams' array"

    targets: list[str] = []
    seen_targets: set[str] = set()
    for idx, bs in enumerate(bitstreams):
        if not isinstance(bs, dict):
            return None, f"bitstreams[{idx}] must be an object"
        target = bs.get("target")
        filename = bs.get("filename")
        if not isinstance(target, str):
            return None, f"bitstreams[{idx}] invalid target {target!r}"
        target = target.strip()
        if not target or utf8_len(target) > 16:
            return None, f"bitstreams[{idx}] invalid target {target!r}"
        if (
            not isinstance(filename, str)
            or utf8_len(filename) > 32
            or not filename.endswith(".bit")
            or not is_safe_relpath(filename)
        ):
            return None, f"bitstreams[{idx}] invalid filename {filename!r}"

        expected_zip_path = posixpath.normpath(f"{core_dir}/{filename}")
        if expected_zip_path not in zip_files:
            return None, f"bitstreams[{idx}] file {expected_zip_path!r} not found in archive"

        if target not in seen_targets:
            seen_targets.add(target)
            targets.append(target)

    def validate_opt_str(key: str, max_bytes: int) -> tuple[str | None, str | None]:
        val = metadata.get(key)
        if val is None:
            return None, None
        if not isinstance(val, str):
            return None, f"metadata.{key} must be a string"
        val = val.strip()
        if not val:
            return None, f"metadata.{key} cannot be empty"
        byte_len = utf8_len(val)
        if byte_len > max_bytes:
            return None, f"metadata.{key} exceeds {max_bytes} bytes ({byte_len})"
        return val, None

    description, err = validate_opt_str("description", MAX_DESCRIPTION_BYTES)
    if err:
        return None, err

    version, err = validate_opt_str("version", 16)
    if err:
        return None, err
    if require_version and version is None:
        return None, "missing required metadata.version"

    release_date, err = validate_opt_str("release_date", 16)
    if err:
        return None, err
    if release_date is not None:
        if not RELEASE_DATE_RE.match(release_date):
            return None, f"invalid metadata.release_date {release_date!r} (expected YYYY-MM-DD)"
        try:
            datetime.strptime(release_date, "%Y-%m-%d")
        except ValueError:
            return None, f"invalid metadata.release_date {release_date!r} (invalid calendar date)"

    url, err = validate_opt_str("url", MAX_URL_BYTES)
    if err:
        return None, err
    if url is not None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None, f"invalid metadata.url {url!r}"

    license_str, err = validate_opt_str("license", 32)
    if err:
        return None, err

    return (
        {
            "id": core_id,
            "name": name.strip(),
            "author": author.strip(),
            "description": description,
            "version": version,
            "release_date": release_date,
            "url": url,
            "license": license_str,
            "min_firmware": min_firmware,
            "targets": targets,
            "core_path": core_dir,
        },
        None,
    )


def inspect_zip_bytes(
    zip_bytes: bytes,
    *,
    allow_legacy: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract and validate cores from a zip archive.

    Strict layout matches:
      - cores/<id>/core.json
      - <id>/core.json

    When allow_legacy is True (releases before LEGACY_RELEASE_CUTOFF), nested
    paths like <bundle>/sdcard/cores/<id>/core.json are also accepted if no
    strict paths exist, ordered shallowest-first.
    """
    warnings: list[str] = []
    cores: list[dict[str, Any]] = []

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        return [], [f"corrupt zip archive: {e}"]

    with zf:
        normalized_to_orig: dict[str, str] = {}
        for orig_name in zf.namelist():
            norm = normalize_zip_path(orig_name)
            if norm is not None:
                normalized_to_orig[norm] = orig_name

        zip_files = set(normalized_to_orig.keys())

        strict_candidates: list[tuple[str, str, str, str]] = []
        legacy_candidates: list[tuple[int, str, str, str, str]] = []

        for norm_path in sorted(zip_files):
            parts = norm_path.split("/")
            if parts[-1] != "core.json":
                continue
            if len(parts) == 3 and parts[0] == "cores":
                strict_candidates.append((norm_path, f"cores/{parts[1]}", parts[1], "cores/<id>"))
            elif len(parts) == 2 and parts[0] != "cores":
                strict_candidates.append((norm_path, parts[0], parts[0], "<id>"))
            elif len(parts) >= 3:
                folder = parts[-2]
                core_dir = "/".join(parts[:-1])
                legacy_candidates.append((len(parts), norm_path, core_dir, folder, "legacy"))

        if strict_candidates:
            candidates = strict_candidates
        elif allow_legacy and legacy_candidates:
            # Prefer shallowest paths first (e.g. sdcard/cores/<id> over sdcard/alt/cores/<id>)
            legacy_candidates.sort(key=lambda t: (t[0], t[1]))
            candidates = [(p, d, f, l) for _, p, d, f, l in legacy_candidates]
        else:
            if legacy_candidates:
                sample = ", ".join(t[1] for t in legacy_candidates[:3])
                warnings.append(f"ignored non-strict core.json path(s): {sample}")
            return [], warnings

        seen_ids: set[str] = set()
        for norm_path, core_dir, folder, layout in candidates:
            info = zf.getinfo(normalized_to_orig[norm_path])
            if info.file_size > MAX_CORE_JSON_BYTES:
                warnings.append(
                    f"{norm_path}: exceeds {MAX_CORE_JSON_BYTES} byte limit ({info.file_size} bytes)"
                )
                continue
            with zf.open(info) as f:
                raw = f.read(MAX_CORE_JSON_BYTES + 1)
            if len(raw) > MAX_CORE_JSON_BYTES:
                warnings.append(f"{norm_path}: exceeds {MAX_CORE_JSON_BYTES} byte limit")
                continue
            core_data, err = validate_core_json(
                raw,
                folder,
                core_dir,
                zip_files,
                require_version=not allow_legacy,
                require_min_firmware=not allow_legacy,
            )
            if err is not None:
                warnings.append(f"{norm_path}: {err}")
                continue
            assert core_data is not None
            if core_data["id"] in seen_ids:
                warnings.append(f"{norm_path}: duplicate core ID {core_data['id']!r} in zip")
                continue
            seen_ids.add(core_data["id"])
            core_data["zip_layout"] = layout
            cores.append(core_data)

    return cores, warnings


class GitHubClient:
    def __init__(self, token: str | None = None):
        self.token = token

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "User-Agent": "gamebub-core-directory",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_repo(self, repo: str) -> dict[str, Any]:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}",
            headers=self._headers(),
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict) or "id" not in data:
                raise ValueError(f"Unexpected repo response for {repo}")
            return data

    def get_json_paginated(self, url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            if not next_url.startswith("https://api.github.com/"):
                raise ValueError(f"Refusing non-GitHub API URL: {next_url!r}")
            req = urllib.request.Request(next_url, headers=self._headers())
            with urllib.request.urlopen(req, timeout=30) as resp:
                chunk = json.loads(resp.read().decode("utf-8"))
                if not isinstance(chunk, list):
                    raise ValueError(f"Expected list response from {next_url}")
                items.extend(chunk)
                link = resp.headers.get("Link", "")
                m = LINK_NEXT_RE.search(link)
                next_url = m.group(1) if m else None
        return items

    def get_releases(self, repo: str) -> list[dict[str, Any]]:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=100"
        return self.get_json_paginated(url)

    def download_asset(self, url: str, max_bytes: int = MAX_ZIP_BYTES) -> bytes:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"Refusing non-HTTPS asset URL: {url!r}")
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "gamebub-core-directory"},
        )
        buf = bytearray()
        with urllib.request.urlopen(req, timeout=60) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise AssetTooLargeError(f"exceeds {max_bytes} byte limit")
        return bytes(buf)


def asset_matches_filters(asset_name: str, patterns: list[str] | None) -> bool:
    if not asset_name.lower().endswith(".zip"):
        return False
    if not patterns:
        return True
    return any(re.search(p, asset_name) for p in patterns)


def _md_code(value: str, *, escape_pipe: bool = False) -> str:
    text = value.replace("\r", " ").replace("\n", " ")
    if escape_pipe:
        text = text.replace("|", "\\|")
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    if text.startswith("`") or text.endswith("`") or (text.startswith(" ") and text.endswith(" ")):
        return f"{fence} {text} {fence}"
    return f"{fence}{text}{fence}"


def _sanitize_md_url(url: str) -> str:
    return url.strip().replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def format_step_summary(
    stats: dict[str, int],
    warnings: list[dict[str, Any]],
    errors: list[str] | None = None,
) -> str:
    lines = [
        "## Game Bub Core Sync",
        "",
        f"- **Repos synced:** {stats['repos']}",
        f"- **Assets downloaded:** {stats['assets_downloaded']}",
        f"- **Assets cached:** {stats['assets_cached']}",
        f"- **Core entries found:** {stats['cores_found']}",
        f"- **Errors:** {stats['errors']}",
        "",
    ]

    if errors:
        lines.extend(["### Errors", ""])
        for err in errors:
            lines.append(f"- {_md_code(err)}")
        lines.append("")

    lines.extend(["### Warnings (latest release per repo)", ""])
    if warnings:
        lines.extend(
            [
                "| Repo | Release | Asset | Warning |",
                "| --- | --- | --- | --- |",
            ]
        )
        for item in warnings:
            repo = item["repo"]
            tag = item.get("release_tag") or ""
            rel_url = _sanitize_md_url(item.get("release_url") or "")
            asset = item.get("asset_name") or ""
            warning = item.get("warning") or ""
            repo_md = f"[{_md_code(repo, escape_pipe=True)}](https://github.com/{repo})"
            rel_md = (
                f"[{_md_code(tag, escape_pipe=True)}]({rel_url})"
                if rel_url and tag
                else _md_code(tag, escape_pipe=True)
            )
            lines.append(
                f"| {repo_md} | {rel_md} | {_md_code(asset, escape_pipe=True)} | {_md_code(warning, escape_pipe=True)} |"
            )
    else:
        lines.append("_No warnings on latest releases._")
    lines.append("")
    return "\n".join(lines)


def write_step_summary(
    summary_path: Path,
    stats: dict[str, int],
    warnings: list[dict[str, Any]],
    errors: list[str] | None = None,
) -> None:
    content = format_step_summary(stats, warnings, errors=errors)
    with summary_path.open("a", encoding="utf-8") as f:
        f.write(content)


def sync_repos(
    repos: list[dict[str, Any]],
    cache: dict[str, Any],
    client: GitHubClient,
    *,
    only_repos: set[str] | None = None,
    prune: bool = True,
    warnings_out: list[dict[str, Any]] | None = None,
    errors_out: list[str] | None = None,
    empty_repos_out: list[str] | None = None,
) -> dict[str, int]:
    repos_cache: dict[str, Any] = cache["repos"]
    assets_cache: dict[str, Any] = cache["assets"]
    active_repo_ids: set[str] = set()
    active_asset_keys: set[str] = set()
    stats = {"repos": 0, "assets_cached": 0, "assets_downloaded": 0, "cores_found": 0, "errors": 0}

    for entry in repos:
        requested_repo = entry["repo"]
        if only_repos is not None and requested_repo.lower() not in only_repos:
            continue
        stats["repos"] += 1
        patterns = entry.get("assets")

        try:
            repo_info = client.get_repo(requested_repo)
            repo_id = int(repo_info["id"])
            canonical_repo = str(repo_info["full_name"])
            if canonical_repo.lower() != requested_repo.lower():
                print(
                    f"[WARN] {requested_repo} resolved to canonical repo {canonical_repo} (id={repo_id})",
                    file=sys.stderr,
                )
            releases = client.get_releases(canonical_repo)
        except FETCH_ERRORS as e:
            msg = f"{requested_repo}: failed to fetch from GitHub: {e}"
            print(f"[ERROR] {msg}", file=sys.stderr)
            if errors_out is not None:
                errors_out.append(msg)
            stats["errors"] += 1
            # Keep existing cache entries for this repo on transient error
            for rid, rinfo in repos_cache.items():
                if (
                    rinfo.get("requested_repo", "").lower() == requested_repo.lower()
                    or rinfo.get("repo", "").lower() == requested_repo.lower()
                ):
                    active_repo_ids.add(rid)
                    for ak, av in assets_cache.items():
                        if str(av.get("repo_id")) == rid:
                            active_asset_keys.add(ak)
            continue

        rid_key = str(repo_id)
        active_repo_ids.add(rid_key)
        repos_cache[rid_key] = {
            "repo_id": repo_id,
            "repo": canonical_repo,
            "requested_repo": requested_repo,
        }

        stable_releases = [
            r for r in releases if not r.get("draft") and not r.get("prerelease")
        ]
        stable_releases.sort(
            key=lambda r: (r.get("published_at") or r.get("created_at") or "", int(r.get("id") or 0)),
            reverse=True,
        )
        latest_release_id = stable_releases[0].get("id") if stable_releases else None
        repo_cores_found = 0

        for release in stable_releases:
            is_latest_release = (
                latest_release_id is not None and release.get("id") == latest_release_id
            )

            tag = release.get("tag_name") or ""
            release_url = (
                release.get("html_url")
                or f"https://github.com/{canonical_repo}/releases/tag/{urllib.parse.quote(tag, safe='')}"
            )
            published_at = release.get("published_at") or release.get("created_at") or ""
            allow_legacy = is_legacy_release(published_at)

            release_found_core = False

            for asset in release.get("assets", []):
                asset_name = asset.get("name") or ""
                if not asset_matches_filters(asset_name, patterns):
                    continue

                asset_id = int(asset["id"])
                asset_updated_at = asset.get("updated_at") or asset.get("created_at") or ""
                asset_size = int(asset.get("size") or 0)
                download_url = asset.get("browser_download_url") or ""

                key = str(asset_id)
                active_asset_keys.add(key)

                def record_latest_warnings(asset_warnings: list[str]) -> None:
                    if warnings_out is None or not is_latest_release:
                        return
                    for w in asset_warnings:
                        warnings_out.append(
                            {
                                "repo": canonical_repo,
                                "release_tag": tag,
                                "release_url": release_url,
                                "asset_name": asset_name,
                                "warning": w,
                            }
                        )

                existing = assets_cache.get(key)
                if (
                    existing is not None
                    and existing.get("asset_updated_at") == asset_updated_at
                    and existing.get("asset_size") == asset_size
                    and existing.get("allow_legacy") == allow_legacy
                    and (existing.get("asset_sha256") is not None or asset_size > MAX_ZIP_BYTES)
                ):
                    existing["repo_id"] = repo_id
                    existing["repo"] = canonical_repo
                    existing["release_id"] = release.get("id")
                    existing["release_tag"] = tag
                    existing["release_url"] = release_url
                    existing["published_at"] = published_at
                    existing.pop("prerelease", None)
                    existing["download_url"] = download_url
                    existing["asset_name"] = asset_name
                    stats["assets_cached"] += 1
                    cores = existing.get("cores", [])
                    stats["cores_found"] += len(cores)
                    repo_cores_found += len(cores)
                    if cores:
                        release_found_core = True
                    record_latest_warnings(existing.get("warnings", []))
                    continue

                def cache_oversized(warn_msg: str) -> None:
                    assets_cache[key] = {
                        "repo_id": repo_id,
                        "repo": canonical_repo,
                        "release_id": release.get("id"),
                        "release_tag": tag,
                        "release_url": release_url,
                        "published_at": published_at,
                        "allow_legacy": allow_legacy,
                        "asset_id": asset_id,
                        "asset_name": asset_name,
                        "asset_size": asset_size,
                        "asset_sha256": None,
                        "asset_updated_at": asset_updated_at,
                        "download_url": download_url,
                        "scanned_at": utc_now_iso(),
                        "cores": [],
                        "warnings": [warn_msg],
                    }
                    record_latest_warnings([warn_msg])

                if asset_size > MAX_ZIP_BYTES:
                    warn_msg = f"skipped: exceeds {MAX_ZIP_BYTES} byte limit"
                    print(
                        f"[WARN] {canonical_repo}@{tag} {asset_name}: skipping oversized zip ({asset_size} bytes)",
                        file=sys.stderr,
                    )
                    cache_oversized(warn_msg)
                    continue

                mode = "legacy" if allow_legacy else "strict"
                print(f"[SCAN:{mode}] {canonical_repo}@{tag} -> {asset_name} ({asset_size // 1024} KiB)")
                try:
                    zip_bytes = client.download_asset(download_url)
                except AssetTooLargeError as e:
                    warn_msg = f"skipped: {e}"
                    print(
                        f"[WARN] {canonical_repo}@{tag} {asset_name}: {warn_msg}",
                        file=sys.stderr,
                    )
                    cache_oversized(warn_msg)
                    continue
                except FETCH_ERRORS as e:
                    err_msg = f"{canonical_repo}@{tag} {asset_name}: download failed: {e}"
                    print(f"[ERROR] {err_msg}", file=sys.stderr)
                    if errors_out is not None:
                        errors_out.append(err_msg)
                    stats["errors"] += 1
                    continue

                asset_sha256 = hashlib.sha256(zip_bytes).hexdigest()
                cores, warnings = inspect_zip_bytes(zip_bytes, allow_legacy=allow_legacy)
                stats["assets_downloaded"] += 1
                stats["cores_found"] += len(cores)
                repo_cores_found += len(cores)
                if cores:
                    release_found_core = True
                for w in warnings:
                    print(f"  [WARN] {w}", file=sys.stderr)
                record_latest_warnings(warnings)
                for c in cores:
                    print(
                        f"  [OK:{c['zip_layout']}] {c['id']} ({c['name']}) [{', '.join(c['targets'])}]"
                    )

                assets_cache[key] = {
                    "repo_id": repo_id,
                    "repo": canonical_repo,
                    "release_id": release.get("id"),
                    "release_tag": tag,
                    "release_url": release_url,
                    "published_at": published_at,
                    "allow_legacy": allow_legacy,
                    "asset_id": asset_id,
                    "asset_name": asset_name,
                    "asset_size": asset_size,
                    "asset_sha256": asset_sha256,
                    "asset_updated_at": asset_updated_at,
                    "download_url": download_url,
                    "scanned_at": utc_now_iso(),
                    "cores": cores,
                    "warnings": warnings,
                }

            if release_found_core:
                break

        if repo_cores_found == 0 and empty_repos_out is not None:
            empty_repos_out.append(canonical_repo)

    if prune and only_repos is None:
        for rid in list(repos_cache.keys()):
            if rid not in active_repo_ids:
                del repos_cache[rid]
        for ak in list(assets_cache.keys()):
            if ak not in active_asset_keys:
                del assets_cache[ak]

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Game Bub core releases from GitHub")
    parser.add_argument("--repos", type=Path, default=REPOS_PATH, help="Path to repos.toml")
    parser.add_argument("--cache", type=Path, default=CACHE_PATH, help="Path to data/cache.json")
    parser.add_argument(
        "--only",
        action="append",
        help="Only sync specific owner/repo (can be passed multiple times; disables cache pruning)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write cache file")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any synced repo errors or produces zero valid cores",
    )
    args = parser.parse_args(argv)

    repos = load_repos(args.repos)
    cache = load_cache(args.cache)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    client = GitHubClient(token=token)

    only_repos = {r.lower() for r in args.only} if args.only else None
    latest_warnings: list[dict[str, Any]] = []
    sync_errors: list[str] = []
    empty_repos: list[str] = []
    stats = sync_repos(
        repos,
        cache,
        client,
        only_repos=only_repos,
        prune=only_repos is None,
        warnings_out=latest_warnings,
        errors_out=sync_errors,
        empty_repos_out=empty_repos,
    )

    print(
        f"Sync complete: repos={stats['repos']} downloaded={stats['assets_downloaded']} "
        f"cached={stats['assets_cached']} core_entries={stats['cores_found']} errors={stats['errors']}"
    )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        write_step_summary(
            Path(summary_path),
            stats=stats,
            warnings=latest_warnings,
            errors=sync_errors,
        )

    if not args.dry_run:
        save_cache(cache, args.cache)

    if args.strict:
        if empty_repos:
            print(
                f"[ERROR] Strict check failed: no valid cores found for {', '.join(empty_repos)}",
                file=sys.stderr,
            )
        return 1 if stats["errors"] > 0 or bool(empty_repos) else 0

    return 1 if stats["errors"] > 0 and stats["assets_downloaded"] == 0 and stats["assets_cached"] == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
