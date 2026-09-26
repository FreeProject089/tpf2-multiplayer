#!/usr/bin/env python3
"""Offline launcher release regression tests; no credentials or network used."""
import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
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
            return {**kw["body"], "id": 42, "assets": [],
                    "upload_url": "https://example.invalid/upload{?name}"}

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


class VersionReleaseTest(unittest.TestCase):
    """Exercise the full publisher with local payloads and an in-memory service."""
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '.git')))
        self.version = '0.7.0.6'
        self.files = dict(zip(publisher.PAYLOAD[:4], (
            b'msi fixture', b'zip fixture', b'DEFAULT_VERSION = "0.7.0.6"',
            b'DEFAULT_VERSION="0.7.0.6"')))
        self.files['SHA256SUMS.txt'] = self.checksums(self.files)
        native = {f'tpf2mp-linux-{self.version}-native.{ext}': ext.encode()
                  for ext in ('run', 'tar.gz')}
        native[f'tpf2mp-linux-{self.version}-native.sha256'] = self.checksums(native)
        self.files.update(native)
        for name, data in self.files.items():
            (self.root / name).write_bytes(data)
        for name in (publisher.WINDOWS_NAME, publisher.LINUX_NAME):
            (self.root / name).write_bytes(name.encode())
        self.mod = self.release(1, 'v' + self.version)
        self.mod['body'] = '| **Linux / Steam Deck** | https://example.invalid/releases/download/v0.7.0.6/' + publisher.LINUX_NAME + ' |'
        self.mod['assets'] = [{'id': 90, 'name': publisher.LINUX_NAME}]
        self.releases = [self.mod]
        self.writes = []
        self.gh = SimpleNamespace(call=self.call, write=self.write, get=lambda url: None)
        self.enterContext(patch.object(publisher, 'token', return_value='offline'))
        self.enterContext(patch.object(publisher, 'GitHub', return_value=self.gh))
        self.enterContext(patch.object(publisher.urllib.request, 'urlopen', side_effect=AssertionError('network')))
        self.sleep = self.enterContext(patch.object(publisher.time, 'sleep'))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    @staticmethod
    def checksums(files):
        return ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n'
                       for name, data in files.items()).encode()

    @staticmethod
    def release(id, tag):
        return dict(id=id, tag_name=tag, draft=True, prerelease=False, assets=[],
                    name='TpF2 Multiplayer', body='', target_commitish='fixture-commit',
                    html_url=f'https://example.invalid/{tag}',
                    upload_url=f'https://example.invalid/upload/{id}' + '{?name}')

    def call(self, method, url):
        self.assertEqual(method, 'GET')
        if url == f'/repos/{publisher.LAUNCHER_REPO}/releases/latest':
            return dict(tag_name='v1.3.0', assets=[])
        self.assertTrue(url.startswith(f'/repos/{publisher.REPO}/releases?'), url)
        return self.releases

    def write(self, what, method, url, **kw):
        self.writes.append((method, url, kw))
        if method == 'POST' and url.endswith('/releases'):
            return {**self.release(10 + len(self.writes), kw['body']['tag_name']), **kw['body']}

    def run_version(self, *flags):
        argv = ['publish_release.py', 'v' + self.version, '--linux-dir', str(self.root),
                '--payload-dir', str(self.root), '--windows-launcher', str(self.root / publisher.WINDOWS_NAME),
                '--linux-launcher', str(self.root / publisher.LINUX_NAME), *flags]
        with patch.object(publisher.sys, 'argv', argv):
            publisher.main()

    def test_native_files_on_both_install_releases_and_page_published_first(self):
        self.run_version('--publish')
        uploads = [(url, kw['data']) for method, url, kw in self.writes if '?name=' in url]
        self.assertEqual(len(uploads), 18)  # Eight files twice, plus two launchers.
        for name, data in self.files.items():
            self.assertEqual([b for url, b in uploads if url.endswith('?name=' + name)], [data, data])
        for name in (publisher.WINDOWS_NAME, publisher.LINUX_NAME):
            self.assertEqual(sum(url.endswith('?name=' + name) for url, _ in uploads), 1)
        published = [(url, kw['body']) for method, url, kw in self.writes
                     if method == 'PATCH' and 'draft' in kw['body']]
        self.assertEqual([body for _, body in published], [
            {'draft': False, 'make_latest': 'true'}, {'draft': False, 'make_latest': 'false'}])
        self.assertTrue(published[-1][0].endswith('/1'))
        self.sleep.assert_called_once_with(2)
        page = next(kw['body'] for method, url, kw in self.writes
                    if method == 'POST' and url == f'/repos/{publisher.REPO}/releases')
        self.assertEqual(page['tag_name'], self.version)
        self.assertIn('/download/0.7.0.6/', page['body'])
        self.assertEqual(page['target_commitish'], 'fixture-commit')
        self.assertIn(('DELETE', f'/repos/{publisher.REPO}/releases/assets/90', {}), self.writes)

    def test_prerelease_page_is_not_latest(self):
        self.mod['prerelease'] = True
        self.run_version('--publish')
        for _, _, kw in self.writes:
            if 'make_latest' in kw.get('body', {}):
                self.assertEqual(kw['body']['make_latest'], 'false')

    def test_default_leaves_both_as_drafts(self):
        self.run_version()
        self.assertFalse(any(kw.get('body', {}).get('draft') is False for _, _, kw in self.writes))
        self.sleep.assert_not_called()

    def test_corrupt_native_file_prevents_all_writes(self):
        (self.root / f'tpf2mp-linux-{self.version}-native.run').write_bytes(b'damaged')
        with self.assertRaisesRegex(SystemExit, 'does not match'):
            self.run_version('--publish')
        self.assertEqual(self.writes, [])

    def test_published_page_is_protected(self):
        page = self.release(2, self.version)
        page['draft'] = False
        self.releases.append(page)
        with self.assertRaisesRegex(SystemExit, 'already published'):
            self.run_version('--publish')
        self.assertEqual(self.writes, [])

    def test_windows_only_page_links_proton_fallback(self):
        self.run_version('--no-linux-launcher')
        page = next(kw['body'] for method, url, kw in self.writes
                    if method == 'POST' and url == f'/repos/{publisher.REPO}/releases')
        self.assertNotIn(publisher.LINUX_NAME, page['body'])
        self.assertIn(f'{publisher.PACKAGES_REPO}/releases/download/v0.7.0.6/install_proton.sh', page['body'])


class RepublishTest(unittest.TestCase):
    def setUp(self):
        self.data = b'native installer fixture'
        self.rel = dict(id=7, tag_name='v0.7.0.5', name='original name', body='original notes',
                        draft=False, prerelease=False, published_at='2026-09-25T12:00:00Z', assets=[
                            dict(name='native.run', browser_download_url='https://example.invalid/native.run',
                                 digest='sha256:' + hashlib.sha256(self.data).hexdigest())])
        self.calls = []
        self.gh = SimpleNamespace(call=self.call, write=lambda what, *args, **kw: self.call(*args, **kw))
        self.enterContext(patch.object(publisher.urllib.request, 'urlopen',
                                      side_effect=lambda *args, **kw: io.BytesIO(self.data)))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def call(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if method == 'POST' and url.endswith('/releases'):
            return dict(id=8, upload_url='https://example.invalid/upload', assets=[], **kw['body'])

    def test_recreation_preserves_metadata_and_bytes_without_touching_tag(self):
        publisher.republish(self.gh, self.rel, False)
        self.assertEqual([c[0] for c in self.calls], ['DELETE', 'POST', 'POST', 'PATCH'])
        body = self.calls[1][2]['body']
        for key in ('tag_name', 'name', 'body', 'prerelease'):
            self.assertEqual(body[key], self.rel[key])
        self.assertTrue(body['draft'])
        self.assertEqual(self.calls[2][2]['data'], self.data)
        self.assertEqual(self.calls[-1][2]['body'], {'draft': False, 'make_latest': 'false'})
        self.assertTrue(self.calls[0][1].endswith('/releases/7'))

    def test_corrupt_download_stops_before_deletion(self):
        self.data += b'corrupt'
        with self.assertRaisesRegex(SystemExit, 'does not match'):
            publisher.republish(self.gh, self.rel, False)
        self.assertEqual(self.calls, [])

    def test_dry_run_never_deletes_or_uploads(self):
        publisher.republish(self.gh, self.rel, True)
        self.assertEqual(self.calls, [])

    def test_selection_excludes_pages_launchers_drafts_and_prereleases(self):
        releases = [self.rel] + [dict(self.rel, published_at='2026-09-26T12:00:00Z', **changes)
                                for changes in ({'tag_name': '0.7.0.6'}, {'tag_name': 'launcher-v1.3.0'},
                                                {'draft': True}, {'prerelease': True})]
        gh = SimpleNamespace(call=lambda *args: releases)
        self.assertIs(publisher.newest_install_files(gh), self.rel)

    def test_launcher_publish_republishes_install_files_last(self):
        args = SimpleNamespace(no_linux_launcher=False, dry_run=False, publish=True)
        args.launcher_release = dict(tag_name='v1.3.0', html_url='https://example.invalid/launcher')
        with patch.object(publisher, 'launchers', return_value=[]), \
             patch.object(publisher, 'find_release', return_value=None), \
             patch.object(publisher, 'newest_install_files', return_value=self.rel):
            publisher.launcher_release(self.gh, args)
        self.assertEqual([c[0] for c in self.calls], ['POST', 'PATCH', 'DELETE', 'POST', 'POST', 'PATCH'])
        self.assertEqual(self.calls[0][2]['body']['tag_name'], 'launcher-v1.3.0')
        self.assertEqual(self.calls[3][2]['body']['tag_name'], 'v0.7.0.5')

if __name__ == "__main__":
    unittest.main()
