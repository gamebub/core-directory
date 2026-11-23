#!/usr/bin/env python3
"""Build static site and cores.json API from data/cache.json."""

from __future__ import annotations

import argparse
import json
import shutil
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jinja2

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config.toml"
REPOS_PATH = ROOT_DIR / "repos.toml"
CACHE_PATH = ROOT_DIR / "data" / "cache.json"
WEB_DIR = ROOT_DIR / "web"
SITE_DIR = ROOT_DIR / "site"

DEFAULT_CONFIG: dict[str, Any] = {
    "user_guide_url": "https://docs.gamebub.net/user-guide/third-party-cores/",
    "add_core_url": "https://docs.gamebub.net/developing-cores/packaging/#core-directory",
    "github_url": "https://github.com/gamebub/core-directory",
    "default_target": "gamebub_rev4",
    "targets": {
        "gamebub_rev4": "Game Bub (Rev 4)",
        "gamebub_rev2": "Game Bub (Rev 2)",
    },
}


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    with path.open("rb") as f:
        data = tomllib.load(f)
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(data, dict):
        cfg.update(data)
    return cfg


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_display(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def format_date(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    return iso_str[:10]


def release_sort_key(asset: dict[str, Any]) -> tuple[str, str, int]:
    return (
        asset.get("published_at") or "",
        asset.get("asset_updated_at") or "",
        int(asset.get("asset_id") or 0),
    )


def build_release_record(asset: dict[str, Any], core: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": asset["release_tag"],
        "version": core.get("version"),
        "release_date": core.get("release_date") or format_date(asset.get("published_at")),
        "published_at": asset.get("published_at"),
        "release_url": asset["release_url"],
        "download_url": asset["download_url"],
        "asset_name": asset["asset_name"],
        "asset_size": asset.get("asset_size", 0),
        "asset_sha256": asset.get("asset_sha256"),
        "zip_layout": core.get("zip_layout"),
        "core_path": core.get("core_path"),
        "min_firmware": core.get("min_firmware"),
        "targets": core.get("targets", []),
    }


def aggregate_cores(
    cache: dict[str, Any],
    allowed_repos: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate stable cores keyed by canonical (repo_id, core_id).

    By default, each repository only contributes cores from its latest valid
    stable release. This prevents renamed/retired core IDs from older releases
    from lingering as ghost entries.
    """
    raw_repos = cache.get("repos", {})
    repos_meta: dict[int, dict[str, Any]] = {}
    for rid_str, rinfo in raw_repos.items():
        rid = int(rinfo.get("repo_id", rid_str))
        if allowed_repos is not None:
            canon = (rinfo.get("repo") or "").lower()
            req = (rinfo.get("requested_repo") or "").lower()
            if canon not in allowed_repos and req not in allowed_repos:
                continue
        repos_meta[rid] = rinfo

    # Partition assets with >=1 valid core by repo_id
    by_repo: dict[int, list[dict[str, Any]]] = {}
    for asset in cache.get("assets", {}).values():
        if not asset.get("cores") or asset.get("prerelease"):
            continue
        rid = asset.get("repo_id")
        if rid is None:
            continue
        rid = int(rid)
        if raw_repos:
            if rid not in repos_meta:
                continue
        elif allowed_repos is not None and (asset.get("repo") or "").lower() not in allowed_repos:
            continue
        by_repo.setdefault(rid, []).append(asset)

    grouped: dict[tuple[int, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}

    for rid, assets in by_repo.items():
        latest_rel_id = max(
            assets,
            key=lambda a: (a.get("published_at") or "", int(a.get("release_id") or 0)),
        ).get("release_id")
        assets = [a for a in assets if a.get("release_id") == latest_rel_id]

        for asset in assets:
            for core in asset.get("cores", []):
                core_id = core.get("id")
                if not core_id:
                    continue
                grouped.setdefault((rid, core_id), []).append((asset, core))

    results: list[dict[str, Any]] = []
    for (rid, core_id), pairs in grouped.items():
        primary_asset, primary_core = max(pairs, key=lambda pair: release_sort_key(pair[0]))
        canonical_repo = repos_meta.get(rid, {}).get("repo") or primary_asset["repo"]
        repo_url = f"https://github.com/{canonical_repo}"

        results.append(
            {
                "id": core_id,
                "repo_id": rid,
                "repo": canonical_repo,
                "repo_url": repo_url,
                "name": primary_core["name"],
                "author": primary_core["author"],
                "description": primary_core.get("description"),
                "license": primary_core.get("license"),
                "url": primary_core.get("url") or repo_url,
                "min_firmware": primary_core.get("min_firmware"),
                "targets": primary_core.get("targets", []),
                "latest": build_release_record(primary_asset, primary_core),
            }
        )

    results.sort(key=lambda c: (c["name"].lower(), c["id"].lower(), c["repo"].lower()))
    return results


def filter_rendered_cores(
    cores: list[dict[str, Any]],
    default_target: str | None = None,
) -> list[dict[str, Any]]:
    """Select stable cores to render in HTML, filtered to default_target if set."""
    rendered: list[dict[str, Any]] = []
    for c in cores:
        if c.get("latest") is None:
            continue
        if default_target and default_target not in (c.get("targets") or []):
            continue
        rendered.append(c)
    return rendered


def build_site(
    repos_path: Path = REPOS_PATH,
    cache_path: Path = CACHE_PATH,
    web_dir: Path = WEB_DIR,
    site_dir: Path = SITE_DIR,
    config_path: Path = CONFIG_PATH,
) -> dict[str, Any]:
    config = load_config(config_path)
    default_target = config.get("default_target")
    user_guide_url = str(config.get("user_guide_url") or DEFAULT_CONFIG["user_guide_url"])
    add_core_url = str(config.get("add_core_url") or DEFAULT_CONFIG["add_core_url"])
    github_url = str(config.get("github_url") or DEFAULT_CONFIG["github_url"])

    with repos_path.open("rb") as f:
        repos_data = tomllib.load(f)
    allowed_repos = {e["repo"].lower() for e in repos_data.get("repos", [])}

    if cache_path.exists():
        with cache_path.open("r", encoding="utf-8") as f:
            cache = json.load(f)
    else:
        cache = {"repos": {}, "assets": {}}

    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    generated_display = utc_now_display(now)
    cores = aggregate_cores(cache, allowed_repos=allowed_repos)
    rendered_cores = filter_rendered_cores(cores, default_target=default_target)
    stable_count = len(rendered_cores)

    site_dir.mkdir(parents=True, exist_ok=True)

    api_payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "default_target": default_target,
        "targets": config.get("targets", {}),
        "cores": cores,
    }
    with (site_dir / "cores.json").open("w", encoding="utf-8") as f:
        json.dump(api_payload, f, indent=2)
        f.write("\n")

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(web_dir),
        autoescape=jinja2.select_autoescape(["html", "j2"]),
    )
    template = env.get_template("index.html.j2")
    rendered_html = template.render(
        cores=rendered_cores,
        user_guide_url=user_guide_url,
        add_core_url=add_core_url,
        github_url=github_url,
        generated_at=generated_at,
        generated_at_display=generated_display,
    )
    (site_dir / "index.html").write_text(rendered_html, encoding="utf-8")

    for asset_name in ("style.css", "app.js"):
        shutil.copy2(web_dir / asset_name, site_dir / asset_name)

    return {
        "total_tracked_cores": len(cores),
        "stable_rendered": stable_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Game Bub Core Directory static site")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--repos", type=Path, default=REPOS_PATH)
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    parser.add_argument("--web", type=Path, default=WEB_DIR)
    parser.add_argument("--out", type=Path, default=SITE_DIR)
    args = parser.parse_args(argv)

    summary = build_site(
        repos_path=args.repos,
        cache_path=args.cache,
        web_dir=args.web,
        site_dir=args.out,
        config_path=args.config,
    )
    print(
        f"Built site in {args.out}: rendered={summary['stable_rendered']} "
        f"total_json={summary['total_tracked_cores']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
