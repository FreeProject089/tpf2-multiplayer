#!/usr/bin/env python3
"""Offline launcher release regression tests; no credentials or network used."""
import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "publisher", Path(__file__).resolve().parents[1] / "publish_release.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class LauncherReleaseTest(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(windows_launcher=None, linux_launcher=None,
                                    no_linux_launcher=False, publish=False, dry_run=False)
        self.blobs = {"launcher-Setup.exe": b"windows fixture", "launcher.AppImage": b"linux fixture"}
        self.blobs["SHA256SUMS.txt"] = "".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n"
            for name, data in self.blobs.items()).encode()
        self.source = {"tag_name": "v1.2.3", "html_url": "https://example.invalid/launcher",
                       "assets": [{"name": n, "browser_download_url": n} for n in self.blobs]}
        self.existing = []
        self.writes = []
        self.gh = SimpleNamespace(call=self.call, write=self.write)
        # Unexpected credential access and HTTP calls fail, including future code paths.
        self.enterContext(patch.object(publisher, "token", side_effect=AssertionError("credentials")))
        self.enterContext(patch.object(publisher.GitHub, "call", side_effect=AssertionError("network")))
        self.enterContext(patch.object(publisher.urllib.request, "urlopen",
                                      side_effect=lambda url, **kw: io.BytesIO(self.blobs[url])))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def call(self, method, url):
        self.assertEqual(method, "GET")
        if url == f"/repos/{publisher.LAUNCHER_REPO}/releases/latest":
            return self.source
        self.assertTrue(url.startswith(f"/repos/{publisher.REPO}/releases?"), url)
        return self.existing

    def write(self, what, method, url, **kw):
        self.writes.append((method, url, kw))
        if self.args.dry_run:
            return None
        if method == "POST" and url.endswith("/releases"):
            return {"id": 42, "assets": [], "upload_url": "https://example.invalid/upload{?name}"}

    def run_release(self):
        publisher.launcher_release(self.gh, self.args)

    def test_draft_contains_both_verified_launchers(self):
        self.run_release()
        body = self.writes[0][2]["body"]
        self.assertEqual(body["tag_name"], "launcher-v1.2.3")
        self.assertEqual(body["make_latest"], "false")
        self.assertTrue(body["draft"])
        uploads = self.writes[1:]
        self.assertEqual(len(uploads), 2)
        for (method, url, kw), name, data in zip(uploads,
                (publisher.WINDOWS_NAME, publisher.LINUX_NAME), list(self.blobs.values())[:2]):
            self.assertEqual(method, "POST")
            self.assertTrue(url.endswith("?name=" + name))
            self.assertEqual(kw["data"], data)
            self.assertIn("/launcher-v1.2.3/" + name, body["body"])

    def test_publish_never_marks_latest(self):
        self.args.publish = True
        self.run_release()
        self.assertEqual(self.writes[-1], ("PATCH", f"/repos/{publisher.REPO}/releases/42",
                         {"body": {"draft": False, "make_latest": "false"}}))

    def test_bad_linux_checksum_stops_before_writes(self):
        self.blobs["launcher.AppImage"] += b"damaged"
        with self.assertRaisesRegex(SystemExit, "checksum"):
            self.run_release()
        self.assertEqual(self.writes, [])

    def test_published_release_is_untouched(self):
        self.existing = [{"tag_name": "launcher-v1.2.3", "draft": False}]
        with self.assertRaisesRegex(SystemExit, "already published"):
            self.run_release()
        self.assertEqual(self.writes, [])

    def test_dry_run_does_not_upload(self):
        self.args.dry_run = True
        self.args.publish = True
        self.run_release()
        self.assertEqual(len(self.writes), 1)  # Only the suppressed draft creation.

    def test_windows_only_override(self):
        self.args.no_linux_launcher = True
        self.run_release()
        self.assertEqual(len(self.writes), 2)
        self.assertNotIn(publisher.LINUX_NAME, self.writes[0][2]["body"]["body"])

    def test_invalid_launcher_version_rejected(self):
        self.source["tag_name"] = "nightly"
        with self.assertRaisesRegex(SystemExit, "unexpected launcher tag"):
            self.run_release()
        self.assertEqual(self.writes, [])

    def test_mod_still_requires_linux_directory_before_credentials(self):
        with patch.object(publisher.sys, "argv", ["publish_release.py", "v0.7.0.6"]):
            with self.assertRaisesRegex(SystemExit, "needs --linux-dir"):
                publisher.main()

    def test_launcher_cli_needs_no_linux_payload(self):
        with patch.object(publisher.sys, "argv", ["publish_release.py", "launcher"]), \
             patch.object(publisher, "token", return_value="offline"), \
             patch.object(publisher, "GitHub", return_value=self.gh):
            publisher.main()
        self.assertEqual(len(self.writes), 3)


if __name__ == "__main__":
    unittest.main()
