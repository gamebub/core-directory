from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build  # noqa: E402
import sync  # noqa: E402


def make_zip(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content.encode("utf-8") if isinstance(content, str) else content)
    return buf.getvalue()


def core_json(
    core_id: str = "Dev.Sample",
    *,
    name: str = "Sample",
    author: str = "Dev",
    version: str | None = "1.0.0",
    min_firmware: str | None = "1.1.0",
    target: str = "gamebub_rev4",
    bitstream: str = "top.bit",
) -> str:
    meta: dict[str, object] = {"id": core_id, "name": name, "author": author}
    if version is not None:
        meta["version"] = version
    doc: dict[str, object] = {
        "metadata": meta,
        "bitstreams": [{"target": target, "filename": bitstream}],
    }
    if min_firmware is not None:
        doc["min_firmware"] = min_firmware
    return json.dumps(doc)


class TestCoreDirectory(unittest.TestCase):
    def test_repos_toml_is_valid(self):
        entries = sync.load_repos(ROOT / "repos.toml")
        self.assertGreaterEqual(len(entries), 15)

    def test_zip_validation_rules(self):
        # Valid strict layout (including untrimmed target and ignored __MACOSX noise)
        zb = make_zip(
            {
                "cores/Dev.Sample/core.json": core_json(target=" gamebub_rev4 "),
                "cores/Dev.Sample/top.bit": b"\x00",
                "__MACOSX/cores/Dev.Sample/._core.json": b"junk",
            }
        )
        cores, warnings = sync.inspect_zip_bytes(zb, allow_legacy=False)
        self.assertEqual(warnings, [])
        self.assertEqual(cores[0]["id"], "Dev.Sample")
        self.assertEqual(cores[0]["targets"], ["gamebub_rev4"])

        # Key rejections: bad/reserved ID, UTF-8 byte overflow, missing version/min_firmware, missing bitstream, oversized JSON
        bad_cases = [
            {"cores/NoDot/core.json": core_json("NoDot"), "cores/NoDot/top.bit": b"\x00"},
            {"cores/Game-Bub.GBA/core.json": core_json("Game-Bub.GBA"), "cores/Game-Bub.GBA/top.bit": b"\x00"},
            {"cores/Dev.Sample/core.json": core_json(name="é" * 17), "cores/Dev.Sample/top.bit": b"\x00"},
            {"cores/Dev.Sample/core.json": core_json(version=None), "cores/Dev.Sample/top.bit": b"\x00"},
            {"cores/Dev.Sample/core.json": core_json(min_firmware=None), "cores/Dev.Sample/top.bit": b"\x00"},
            {"cores/Dev.Sample/core.json": core_json()},
            {"cores/Dev.Sample/core.json": b"{" + b" " * (sync.MAX_CORE_JSON_BYTES + 1) + b"}", "cores/Dev.Sample/top.bit": b"\x00"},
        ]
        for files in bad_cases:
            cores, warnings = sync.inspect_zip_bytes(make_zip(files), allow_legacy=False)
            self.assertEqual(cores, [], f"expected rejection for {list(files)}")
            self.assertTrue(warnings)

        # Pre-cutoff legacy releases may omit version and min_firmware
        legacy_zb = make_zip(
            {
                "bundle/sdcard/cores/Dev.Sample/core.json": core_json(version=None, min_firmware=None),
                "bundle/sdcard/cores/Dev.Sample/top.bit": b"\x00",
            }
        )
        cores, warnings = sync.inspect_zip_bytes(legacy_zb, allow_legacy=True)
        self.assertEqual(warnings, [])
        self.assertEqual(cores[0]["id"], "Dev.Sample")

    def test_sync_cache_stability_and_prerelease_ignoring(self):
        good_zip = make_zip({"cores/Dev.Sample/core.json": core_json(), "cores/Dev.Sample/top.bit": b"BIT"})

        class FakeClient:
            downloads = 0

            def get_repo(self, repo):
                return {"id": 42, "full_name": repo}

            def get_releases(self, repo):
                return [
                    {
                        "id": 200,
                        "tag_name": "v2.0.0-alpha1",
                        "draft": False,
                        "prerelease": True,
                        "published_at": "2026-11-23T02:00:00Z",
                        "assets": [{"id": 999, "name": "pre.zip", "size": 10, "updated_at": "2026-11-23T02:00:00Z", "browser_download_url": "https://example.invalid/pre.zip"}],
                    },
                    {
                        "id": 100,
                        "tag_name": "v1.0.0",
                        "draft": False,
                        "prerelease": False,
                        "published_at": "2026-11-23T01:00:00Z",
                        "html_url": f"https://github.com/{repo}/releases/tag/v1.0.0",
                        "assets": [{"id": 555, "name": "core.zip", "size": len(good_zip), "updated_at": "2026-11-23T01:00:00Z", "browser_download_url": "https://example.invalid/core.zip"}],
                    },
                ]

            def download_asset(self, url):
                if "pre.zip" in url:
                    raise AssertionError("prerelease should not be downloaded")
                self.downloads += 1
                return good_zip

        client = FakeClient()
        repos = [{"repo": "owner/sample", "assets": None}]
        cache = sync.empty_cache()

        sync.sync_repos(repos, cache, client)
        self.assertEqual(cache["assets"]["555"]["asset_sha256"], hashlib.sha256(good_zip).hexdigest())
        snapshot = json.dumps(cache, sort_keys=True)

        # Second run with unchanged releases must produce zero diff and zero downloads
        sync.sync_repos(repos, cache, client)
        self.assertEqual(client.downloads, 1)
        self.assertEqual(json.dumps(cache, sort_keys=True), snapshot)

    def test_build_site_smoke(self):
        cache = {
            "schema_version": 2,
            "repos": {"1": {"repo_id": 1, "repo": "owner/r", "requested_repo": "owner/r"}},
            "assets": {
                "10": {
                    "repo_id": 1,
                    "repo": "owner/r",
                    "release_id": 100,
                    "release_tag": "v1.0.0",
                    "release_url": "https://github.com/owner/r/releases/tag/v1.0.0",
                    "published_at": "2026-11-23T00:00:00Z",
                    "asset_id": 10,
                    "asset_name": "core.zip",
                    "asset_size": 1024,
                    "asset_sha256": "a" * 64,
                    "asset_updated_at": "2026-11-23T00:00:00Z",
                    "download_url": "https://github.com/owner/r/releases/download/v1.0.0/core.zip",
                    "cores": [
                        {"id": "Dev.Rev4", "name": "Rev4 <script>", "author": "Dev", "version": "1.0.0", "targets": ["gamebub_rev4"]},
                        {"id": "Dev.Rev2Only", "name": "Rev2 Only", "author": "Dev", "version": "1.0.0", "targets": ["gamebub_rev2"]},
                    ],
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_file = tmp_path / "repos.toml"
            cache_file = tmp_path / "cache.json"
            site_dir = tmp_path / "site"
            repos_file.write_text('[[repos]]\nrepo = "owner/r"\n', encoding="utf-8")
            cache_file.write_text(json.dumps(cache), encoding="utf-8")

            summary = build.build_site(repos_file, cache_file, ROOT / "web", site_dir, ROOT / "config.toml")
            self.assertEqual(summary, {"total_tracked_cores": 2, "stable_rendered": 1})

            api = json.loads((site_dir / "cores.json").read_text(encoding="utf-8"))
            self.assertEqual(api["cores"][0]["latest"]["asset_sha256"], "a" * 64)
            self.assertNotIn("prerelease", (site_dir / "cores.json").read_text(encoding="utf-8"))

            html = (site_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("Rev4 &lt;script&gt;", html)
            self.assertNotIn("Rev4 <script>", html)
            self.assertNotIn("Dev.Rev2Only", html)
