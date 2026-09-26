#!/usr/bin/env python3
"""Complete a release: the install files to the packages repository, the two
launchers to the mod's release, then publish it.

    python tools/publish_release.py v0.7.0.6 --linux-dir DIR [--publish]

THE LAYOUT (2026-09-26, the user: releases should "only show 2 things to download
the linux launcher and the windows launcher"). A release of silver2127/tpf2-multiplayer
carries exactly two files:

    TpF2Multiplayer-Launcher-Windows-Setup.exe   tearded/tpf-multiplayer-launcher's Setup.exe
    TpF2Multiplayer-Launcher-Linux.AppImage      its Linux launcher

under names that do not change, so .../releases/latest/download/<name> always
works. Everything the launchers and the Proton script install from goes to a
release with the SAME TAG in silver2127/tpf2-multiplayer-packages:

    TpF2Multiplayer.msi  TpF2Multiplayer-files.zip  install_proton.py  install_proton.sh
    SHA256SUMS.txt       tpf2mp-linux-<v>-native.run / .tar.gz / .sha256

Releases up to 0.7.0.5 keep their files on the mod's own releases; the launchers
and tools/proton/install*.py/.sh look in the packages repository first and fall
back to the mod's release for those.

WHAT IT DOES, in order (each step checks, nothing is published half-done):
  1. the mod's DRAFT release for the tag (the Build MSI workflow makes it with the
     notes; see .github/workflows/build-msi.yml);
  2. the install files: the tag run's workflow artifact (or --payload-dir), checked
     against its SHA256SUMS.txt; the native Linux package from --linux-dir
     (tools/linux/build_release.sh on strelka), checked against its .sha256;
  3. the packages release for the tag (created if missing, pre-release like the mod
     release), and each file uploaded unless the same bytes are already there;
  4. the launchers: tearded's newest launcher release (or --windows-launcher /
     --linux-launcher files), checked against that release's SHA256SUMS.txt;
  5. the mod release's assets made exactly those two (anything else is removed);
  6. with --publish, the draft published (latest unless it is a pre-release).
Without a Linux launcher yet, --no-linux-launcher publishes the Windows one alone.

The token is GH_TOKEN, else the one Git stores for github.com (git credential fill).
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = "silver2127/tpf2-multiplayer"
PACKAGES_REPO = "silver2127/tpf2-multiplayer-packages"
LAUNCHER_REPO = "tearded/tpf-multiplayer-launcher"
WINDOWS_NAME = "TpF2Multiplayer-Launcher-Windows-Setup.exe"
LINUX_NAME = "TpF2Multiplayer-Launcher-Linux.AppImage"
PAYLOAD = ["TpF2Multiplayer.msi", "TpF2Multiplayer-files.zip", "install_proton.py", "install_proton.sh", "SHA256SUMS.txt"]
API = "https://api.github.com"


def say(text):
    print(text, flush=True)


def fail(text):
    raise SystemExit(f"error: {text}")


def token():
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if t:
        return t
    out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line[9:]
    fail("no GitHub token: set GH_TOKEN or store one with git")


class GitHub:
    def __init__(self, tok, dry):
        self.tok, self.dry = tok, dry

    def call(self, method, url, body=None, data=None, ctype=None, raw=False):
        if not url.startswith("http"):
            url = API + url
        headers = {"Authorization": f"Bearer {self.tok}", "Accept": "application/vnd.github+json",
                   "User-Agent": "tpf2mp-publish-release"}
        if body is not None:
            data, ctype = json.dumps(body).encode(), "application/json"
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=600) as r:
            payload = r.read()
        return payload if raw else (json.loads(payload) if payload else None)

    def redirected(self, url):
        """An artifact: the API answers 302 to a signed storage URL, which refuses our
        token (urllib would forward it), so the redirect is fetched without it."""
        class Stop(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.tok}",
                                                   "User-Agent": "tpf2mp-publish-release"})
        try:
            urllib.request.build_opener(Stop).open(req, timeout=60)
            fail(f"no redirect for {url}")
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise
            target = e.headers["Location"]
        with urllib.request.urlopen(target, timeout=600) as r:
            return r.read()

    def get(self, path):
        try:
            return self.call("GET", path)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def write(self, what, method, url, **kw):
        if self.dry:
            say(f"  (dry run) would {what}")
            return None
        return self.call(method, url, **kw)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sums(text):
    """'<hash>  <name>' lines -> {name: hash}."""
    out = {}
    for line in text.replace("\r", "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            out[parts[-1].lstrip("*")] = parts[0].lower()
    return out


def draft_release(gh, tag):
    for page in range(1, 6):
        for r in gh.call("GET", f"/repos/{REPO}/releases?per_page=100&page={page}"):
            if r["tag_name"] == tag:
                return r
    fail(f"{REPO} has no release for {tag}: push the tag and let the Build MSI workflow draft it first")


def payload_from_ci(gh, tag, into):
    runs = gh.call("GET", f"/repos/{REPO}/actions/runs?event=push&per_page=50")["workflow_runs"]
    run = next((r for r in runs if r["head_branch"] == tag and r["name"] == "Build MSI"), None)
    if run is None:
        fail(f"no Build MSI run for {tag}")
    if run["conclusion"] != "success":
        fail(f"the Build MSI run for {tag} is {run['status']}/{run['conclusion']}: wait for it, or pass --payload-dir")
    arts = gh.call("GET", f"/repos/{REPO}/actions/runs/{run['id']}/artifacts")["artifacts"]
    art = next((a for a in arts if a["name"].startswith("TpF2Multiplayer-") and not a["expired"]), None)
    if art is None:
        fail(f"the {tag} run has no TpF2Multiplayer-* artifact (expired?): pass --payload-dir")
    say(f"payload: artifact {art['name']} of run {run['id']}")
    blob = gh.redirected(art["archive_download_url"])
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in PAYLOAD:
            (into / name).write_bytes(z.read(name))
    return into


def check_payload(folder, version):
    missing = [n for n in PAYLOAD if not (folder / n).is_file()]
    if missing:
        fail(f"the payload lacks {', '.join(missing)}")
    listed = sums((folder / "SHA256SUMS.txt").read_text())
    for name in ("TpF2Multiplayer.msi", "TpF2Multiplayer-files.zip"):
        if listed.get(name) != sha256(folder / name):
            fail(f"{name} does not match SHA256SUMS.txt")
    for script, stamp in (("install_proton.py", f'DEFAULT_VERSION = "{version}"'), ("install_proton.sh", f'DEFAULT_VERSION="{version}"')):
        if stamp not in (folder / script).read_text(encoding="utf-8"):
            fail(f"{script} is not pinned to {version} (a tag run stamps it)")
    return [folder / n for n in PAYLOAD]


def linux_files(folder, version):
    names = [f"tpf2mp-linux-{version}-native.{ext}" for ext in ("run", "tar.gz", "sha256")]
    paths = [Path(folder) / n for n in names]
    missing = [p.name for p in paths if not p.is_file()]
    if missing:
        fail(f"--linux-dir lacks {', '.join(missing)} (tools/linux/build_release.sh --version {version}, then the -native copies)")
    listed = sums(paths[2].read_text())
    for p in paths[:2]:
        if listed.get(p.name) != sha256(p):
            fail(f"{p.name} does not match {paths[2].name}")
    return paths


def upload(gh, release, files, names=None):
    """Upload files (as `names`) to a release, skipping identical ones and replacing others."""
    have = {a["name"]: a for a in release.get("assets", [])}
    upload_url = release["upload_url"].split("{")[0]
    for i, path in enumerate(files):
        name = names[i] if names else path.name
        digest = "sha256:" + sha256(path)
        old = have.get(name)
        if old and old.get("digest") == digest:
            say(f"  {name}: already there")
            continue
        if old:
            gh.write(f"replace {name}", "DELETE", f"/repos/{release['_repo']}/releases/assets/{old['id']}")
        say(f"  {name}: uploading {path.stat().st_size} bytes")
        gh.write(f"upload {name}", "POST", f"{upload_url}?name={name}", data=path.read_bytes(), ctype="application/octet-stream")


def launchers(gh, args, into):
    want = [("windows", args.windows_launcher, r"-Setup\.exe$", WINDOWS_NAME)]
    if not args.no_linux_launcher:
        want.append(("linux", args.linux_launcher, r"\.AppImage$", LINUX_NAME))
    rel = gh.call("GET", f"/repos/{LAUNCHER_REPO}/releases/latest")
    listed = {}
    sums_asset = next((a for a in rel["assets"] if a["name"] == "SHA256SUMS.txt"), None)
    if sums_asset:
        listed = sums(urllib.request.urlopen(sums_asset["browser_download_url"], timeout=60).read().decode())
    out = []
    for kind, local, pattern, name in want:
        if local:
            path = Path(local)
            if not path.is_file():
                fail(f"no such {kind} launcher: {local}")
        else:
            asset = next((a for a in rel["assets"] if re.search(pattern, a["name"])), None)
            if asset is None:
                fail(f"{LAUNCHER_REPO} {rel['tag_name']} has no {kind} launcher; pass --{kind}-launcher FILE"
                     + (" or --no-linux-launcher" if kind == "linux" else ""))
            path = into / asset["name"]
            path.write_bytes(urllib.request.urlopen(asset["browser_download_url"], timeout=600).read())
            expected = listed.get(asset["name"]) or (asset.get("digest") or "")[7:]
            if expected and sha256(path) != expected:
                fail(f"{asset['name']} does not match {LAUNCHER_REPO} {rel['tag_name']}'s checksum")
            say(f"{kind} launcher: {asset['name']} from {LAUNCHER_REPO} {rel['tag_name']}")
        out.append((path, name))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tag", help="the release tag, e.g. v0.7.0.6")
    ap.add_argument("--linux-dir", required=True, help="folder with tpf2mp-linux-<v>-native.run/.tar.gz/.sha256")
    ap.add_argument("--payload-dir", help="folder with the Windows/Proton files instead of the tag run's artifact")
    ap.add_argument("--windows-launcher", help="a launcher Setup.exe instead of tearded's newest release")
    ap.add_argument("--linux-launcher", help="a Linux launcher AppImage instead of tearded's newest release")
    ap.add_argument("--no-linux-launcher", action="store_true", help="publish without a Linux launcher (none released yet)")
    ap.add_argument("--publish", action="store_true", help="publish the draft at the end (default: leave it a draft)")
    ap.add_argument("--dry-run", action="store_true", help="check everything, change nothing on GitHub")
    args = ap.parse_args()
    if not re.fullmatch(r"v\d+\.\d+(\.\d+){0,2}", args.tag):
        fail("the tag looks like v0.7.0.6")
    version = args.tag[1:]
    gh = GitHub(token(), args.dry_run)

    mod = draft_release(gh, args.tag)
    say(f"mod release {args.tag}: {'draft' if mod['draft'] else 'PUBLISHED'}, {len(mod['assets'])} asset(s)")
    # A published release is left alone: its files are where older launchers and
    # Proton scripts download them (every release up to 0.7.0.5 keeps them there).
    if not mod["draft"] and not args.dry_run:
        fail(f"{args.tag} is already published; this only completes a draft (--dry-run to look anyway)")
    with tempfile.TemporaryDirectory(prefix="tpf2mp-publish-") as tmp:
        tmp = Path(tmp)
        folder = Path(args.payload_dir) if args.payload_dir else payload_from_ci(gh, args.tag, tmp)
        files = check_payload(folder, version) + linux_files(args.linux_dir, version)
        say(f"install files: {len(files)} checked")
        chosen = launchers(gh, args, tmp)

        # the packages release (same tag, same pre-release flag)
        pkg = gh.get(f"/repos/{PACKAGES_REPO}/releases/tags/{args.tag}")
        if pkg is None:
            say(f"creating {PACKAGES_REPO} {args.tag}")
            pkg = gh.write("create the packages release", "POST", f"/repos/{PACKAGES_REPO}/releases", body={
                "tag_name": args.tag, "target_commitish": "main", "name": f"TpF2 Multiplayer {version} (install files)",
                "prerelease": mod["prerelease"], "make_latest": "false" if mod["prerelease"] else "true",
                "body": f"Install files of [TpF2 Multiplayer {version}](https://github.com/{REPO}/releases/tag/{args.tag}), "
                        "downloaded by its launchers and by install_proton.sh. Players: get the launcher from that release."})
        if pkg is not None:
            pkg["_repo"] = PACKAGES_REPO
            say(f"packages release: {pkg['html_url']}")
            upload(gh, pkg, files)

        # the mod release: exactly the two launchers
        mod["_repo"] = REPO
        keep = {name for _, name in chosen}
        for a in mod["assets"]:
            if a["name"] not in keep:
                gh.write(f"remove {a['name']} from the mod release", "DELETE", f"/repos/{REPO}/releases/assets/{a['id']}")
        mod["assets"] = [a for a in mod["assets"] if a["name"] in keep]
        say("mod release launchers:")
        upload(gh, mod, [p for p, _ in chosen], [n for _, n in chosen])

    change = {}
    if args.no_linux_launcher:
        # no Linux launcher yet: its row would link a file this release does not have
        pkg_url = f"https://github.com/{PACKAGES_REPO}/releases/download/{args.tag}"
        lines = [l if not l.startswith("| **Linux / Steam Deck**") else
                 f"| **Linux / Steam Deck** (the Windows game under Proton) | [install_proton.sh]({pkg_url}/install_proton.sh)"
                 f" -- run it with sh; it fetches the rest itself (the Linux launcher is on its way) |"
                 for l in (mod.get("body") or "").split("\n")]
        if "\n".join(lines) != mod.get("body"):
            change["body"] = "\n".join(lines)
    if args.publish:
        change.update({"draft": False, "make_latest": "false" if mod["prerelease"] else "true"})
    if change:
        gh.write(f"update {args.tag} ({', '.join(change)})", "PATCH", f"/repos/{REPO}/releases/{mod['id']}", body=change)
    if args.publish:
        say(("(dry run) would be " if args.dry_run else "") + f"published: https://github.com/{REPO}/releases/tag/{args.tag}")
    else:
        say("left as a draft (--publish publishes it)")


if __name__ == "__main__":
    main()
