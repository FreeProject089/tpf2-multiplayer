#!/usr/bin/env python3
"""Complete a release: the install files to the packages repository and to the
version's v-tagged release, the two launchers to its download page, then publish.

    python tools/publish_release.py v0.7.0.6 --linux-dir DIR [--publish]
    python tools/publish_release.py launcher [--publish]     a launcher update on its own
    python tools/publish_release.py page v0.7.0.5 [--publish] a published version to this layout

THE LAYOUT (2026-09-26, the user: releases should "only show 2 things to download
the linux launcher and the windows launcher", then: "move to the two launcher
model with another tagged release for the update files for previous launchers").
Each version has TWO releases in silver2127/tpf2-multiplayer:

    0.7.0.6    the page players open, marked Latest, published FIRST; exactly
               TpF2Multiplayer-Launcher-Windows-Setup.exe   tearded's Setup.exe
               TpF2Multiplayer-Launcher-Linux.AppImage      its Linux launcher
               under names that do not change, so .../releases/latest/download/<name>
               always works
    v0.7.0.6   the install files, as every release up to 0.7.0.5 had them, published
               AFTER the page and not Latest:
               TpF2Multiplayer.msi  TpF2Multiplayer-files.zip  install_proton.py
               install_proton.sh  SHA256SUMS.txt  tpf2mp-linux-<v>-native.run/.tar.gz/.sha256

WHY THAT WAY ROUND. Launchers up to 1.2.0 (tearded/tpf-multiplayer-launcher) take
the newest PUBLISHED release of this repository as the update, then install it
through FetchVersion, which looks up v<version> first and needs TpF2Multiplayer.msi
on exactly that release. So v<version> must hold the MSI and be the newest
published release. Launchers from 1.3.0 skip a tag without the "v" whose v twin is
listed (and launcher-v* releases). The same install files also go to the release
with the same tag in silver2127/tpf2-multiplayer-packages, where the Proton script
and the Linux launcher look first. Only v* tags start the release workflow.

LAUNCHER RELEASES (the user, 2026-09-26: "make separate releases for the launcher
updates, tag them as something else"). `launcher` makes a release tagged
launcher-v<launcher version> with the two files under the same names, published
WITHOUT the "Latest" mark. Being published later, it would be the release older
launchers pick (and refuse: not a version), so the newest stable v-release is then
published again (deleted and re-created with the same tag, name, notes and files).

WHAT IT DOES for a version, in order (each step checks, nothing is published half-done):
  1. the DRAFT release v<version> (the Build MSI workflow makes it with the notes;
     see .github/workflows/build-msi.yml);
  2. the install files: the tag run's workflow artifact (or --payload-dir), checked
     against its SHA256SUMS.txt; the native Linux package from --linux-dir
     (tools/linux/build_release.sh on strelka), checked against its .sha256;
  3. the packages release for the tag (created if missing, pre-release like the mod
     release), and each file uploaded unless the same bytes are already there;
  4. the launchers: tearded's newest launcher release (or --windows-launcher /
     --linux-launcher files), checked against that release's SHA256SUMS.txt;
  5. the page <version> (a draft, created if missing) with exactly the two launchers
     and the notes; v<version> with exactly the install files;
  6. with --publish: the page (Latest unless a pre-release), then v<version> (never
     Latest), in that order.
Without a Linux launcher yet, --no-linux-launcher publishes the Windows one alone.

PAGE (the user, 2026-09-26: "now for two launcher on the original page"): a version
published before this layout (0.7.0.5) gets its page <version> with the two
launchers and its notes, the download table replaced by the launchers; v<version>
keeps its files, is renamed "(install files)", and is published again after the page.

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
import time
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


def find_release(gh, tag):
    """The release for a tag, drafts included (releases/tags/<tag> does not see drafts)."""
    for page in range(1, 6):
        batch = gh.call("GET", f"/repos/{REPO}/releases?per_page=100&page={page}")
        for r in batch:
            if r["tag_name"] == tag:
                return r
        if len(batch) < 100:
            return None
    return None


def page_tag_ref(gh, name, commit, dry):
    """The page's tag as an ANNOTATED tag dated now. The releases list on GitHub is
    ordered by the tag's date, and the tag a release creates by itself is a
    lightweight one carrying the commit's date: older than v<version>'s annotated
    tag, so the page sorted BELOW the install files (0.7.0.5, 2026-09-26: "that did
    not work", the list opened on "0.7.0.5 (install files)"). Launchers up to 1.2.0
    go by the publication date, which stays the other way round."""
    ref = gh.get(f"/repos/{REPO}/git/ref/tags/{name}")
    if ref is not None:
        if ref["object"]["type"] == "tag":
            return
        gh.write(f"delete the lightweight tag {name}", "DELETE", f"/repos/{REPO}/git/refs/tags/{name}")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    obj = gh.write(f"create the annotated tag {name} on {commit[:10]}", "POST", f"/repos/{REPO}/git/tags", body={
        "tag": name, "message": f"TpF2 Multiplayer {name}: the launchers", "object": commit, "type": "commit",
        "tagger": {"name": "silver2127", "email": "52584484+silver2127@users.noreply.github.com", "date": now}})
    if obj is not None:
        gh.write(f"create refs/tags/{name}", "POST", f"/repos/{REPO}/git/refs", body={"ref": f"refs/tags/{name}", "sha": obj["sha"]})


def tag_commit(gh, tag):
    ref = gh.call("GET", f"/repos/{REPO}/git/ref/tags/{tag}")["object"]
    return gh.call("GET", f"/repos/{REPO}/git/tags/{ref['sha']}")["object"]["sha"] if ref["type"] == "tag" else ref["sha"]


def published(gh, rel, latest):
    change = {"draft": False, "make_latest": "true" if latest else "false"}
    gh.write(f"publish {rel['tag_name']}{'' if latest else ' (not Latest)'}", "PATCH",
             f"/repos/{REPO}/releases/{rel['id']}", body=change)


def newest_install_files(gh):
    """The newest published stable v<version> release: the one launchers up to 1.2.0 must pick."""
    found = []
    for page in range(1, 6):
        batch = gh.call("GET", f"/repos/{REPO}/releases?per_page=100&page={page}")
        found += [r for r in batch if not r["draft"] and not r["prerelease"]
                  and re.fullmatch(r"v\d+\.\d+(\.\d+){0,2}", r["tag_name"])]
        if len(batch) < 100:
            break
    return max(found, key=lambda r: r["published_at"] or "", default=None)


def republish(gh, rel, dry):
    """Publish a release again (delete it, re-create it with the same tag, name, notes and
    files), so it is the newest published release once more. Its files are downloaded and
    checked against their digests first; the tag itself is never touched."""
    with tempfile.TemporaryDirectory(prefix="tpf2mp-republish-") as tmp:
        files = []
        for a in rel["assets"]:
            path = Path(tmp) / a["name"]
            path.write_bytes(urllib.request.urlopen(a["browser_download_url"], timeout=600).read())
            want = (a.get("digest") or "")[7:]
            if want and sha256(path) != want:
                fail(f"{rel['tag_name']}: {a['name']} does not match its digest; nothing changed")
            files.append(path)
        say(f"{rel['tag_name']}: {len(files)} file(s) downloaded and checked")
        if dry:
            say(f"  (dry run) would delete and re-create {rel['tag_name']} with them")
            return
        gh.call("DELETE", f"/repos/{REPO}/releases/{rel['id']}")
        try:
            new = gh.call("POST", f"/repos/{REPO}/releases", body={
                "tag_name": rel["tag_name"], "name": rel["name"], "body": rel["body"] or "",
                "draft": True, "prerelease": rel["prerelease"], "make_latest": "false"})
            new["_repo"] = REPO
            upload(gh, new, files)
            published(gh, new, False)
        except Exception as e:
            fail(f"{rel['tag_name']} was deleted but not re-created ({e}): create a release for the existing "
                 f"tag {rel['tag_name']} with these files, from {tmp} before this exits, or from the packages repository")
        say(f"{rel['tag_name']} published again: launchers up to 1.2.0 find it first")


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
    args.launcher_release = rel
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


def launcher_release(gh, args):
    """A launcher update on its own: launcher-v<version>, the two files, not marked Latest."""
    with tempfile.TemporaryDirectory(prefix="tpf2mp-launcher-") as tmp:
        chosen = launchers(gh, args, Path(tmp))
        source = args.launcher_release
        lver = source["tag_name"].lstrip("v")
        if not re.fullmatch(r"\d+\.\d+(\.\d+){0,2}", lver):
            fail(f"unexpected launcher tag {source['tag_name']}")
        tag = f"launcher-v{lver}"
        base = f"https://github.com/{REPO}/releases/download/{tag}"
        body = (f"## Launcher {lver}\n\n"
                "A new version of the launcher that installs, updates and starts TpF2 Multiplayer. "
                "It does not change the mod version: the launcher installs whichever mod release your group plays.\n\n"
                "| You play on | Get this one file |\n| --- | --- |\n"
                f"| **Windows** | **[{WINDOWS_NAME}]({base}/{WINDOWS_NAME})** |\n"
                + ("" if args.no_linux_launcher else f"| **Linux / Steam Deck** | **[{LINUX_NAME}]({base}/{LINUX_NAME})** |\n")
                + f"\nAn installed launcher also updates itself (Settings). What changed: [launcher {lver}]({source['html_url']}).\n")
        existing = find_release(gh, tag)
        if existing and not existing["draft"] and not args.dry_run:
            fail(f"{tag} is already published")
        rel = existing or gh.write(f"create {tag}", "POST", f"/repos/{REPO}/releases", body={
            "tag_name": tag, "target_commitish": "main", "name": f"Launcher {lver}", "body": body,
            "draft": True, "prerelease": False, "make_latest": "false"})
        if rel is None:
            say(f"  (dry run) {tag} would carry: {', '.join(n for _, n in chosen)}")
            return
        rel["_repo"] = REPO
        keep = {n for _, n in chosen}
        for a in rel.get("assets", []):
            if a["name"] not in keep:
                gh.write(f"remove {a['name']}", "DELETE", f"/repos/{REPO}/releases/assets/{a['id']}")
        rel["assets"] = [a for a in rel.get("assets", []) if a["name"] in keep]
        say(f"{tag}:")
        upload(gh, rel, [p for p, _ in chosen], [n for _, n in chosen])
        if args.publish:
            published(gh, rel, False)
            say(f"published: https://github.com/{REPO}/releases/tag/{tag}")
            # published later than every version, it is what launchers up to 1.2.0 would
            # now pick (and refuse); the newest version's install files go back on top
            files = newest_install_files(gh)
            if files is None:
                say("no published v-release to put back on top")
            else:
                republish(gh, files, args.dry_run)
        else:
            say("left as a draft (--publish publishes it)")


def launcher_table(page_tag, files_url):
    base = f"https://github.com/{REPO}/releases/download/{page_tag}"
    return ("## Download\n\n| You play on | Get this one file |\n| --- | --- |\n"
            f"| **Windows** (Steam, game build 35924) | **[{WINDOWS_NAME}]({base}/{WINDOWS_NAME})** -- install it, then **Update & play** |\n"
            f"| **Linux / Steam Deck** (the native game or Proton) | **[{LINUX_NAME}]({base}/{LINUX_NAME})** -- make it executable, run it, then **Update & play** |\n\n"
            f"The launcher installs this version and keeps it up to date. Manual install files (MSI, Linux package, Proton script, checksums) "
            f"are in [the install files]({files_url}). The two *Source code* archives at the bottom are the repository, not the mod. "
            "Everyone in a session needs the same version.\n")


def page_release(gh, args):
    """A version published before this layout: its page with the two launchers, then its
    v-release (the files, untouched) renamed and published again after the page."""
    tag = args.page_tag
    page_tag_name = (tag or "")[1:]
    if not re.fullmatch(r"v\d+\.\d+(\.\d+){0,2}", tag or ""):
        fail("page needs the version's tag, e.g. page v0.7.0.5")
    version = tag[1:]
    mod = find_release(gh, tag)
    if mod is None or mod["draft"]:
        fail(f"{tag} is not a published release (a new version goes through publish_release.py {tag})")
    if not any(a["name"] == "TpF2Multiplayer.msi" for a in mod["assets"]):
        fail(f"{tag} has no TpF2Multiplayer.msi: launchers up to 1.2.0 could not install it")
    page = find_release(gh, page_tag_name)
    if page and not page["draft"]:
        if not args.replace_page:
            fail(f"the page {page_tag_name} is already published (--replace-page makes it again)")
        gh.write(f"delete the published page {page_tag_name}", "DELETE", f"/repos/{REPO}/releases/{page['id']}")
        page = None
    # the page at the commit the version's tag names, under an annotated tag dated now
    commit = tag_commit(gh, tag)
    if page is None:
        page_tag_ref(gh, page_tag_name, commit, args.dry_run)
    notes = mod.get("body") or ""
    if notes.startswith("The install files of ") and "\n\n" in notes:   # a page made before: its notes once
        notes = notes.split("\n\n", 1)[1]
    files_url = mod["html_url"]
    rest = notes.split("\n## ", 1)[1] if notes.startswith("## Download") and "\n## " in notes else notes
    body = launcher_table(page_tag_name, files_url) + ("\n## " + rest if notes.startswith("## Download") else "\n" + rest)
    files_body = (f"The install files of **[TpF2 Multiplayer {version}](https://github.com/{REPO}/releases/tag/{page_tag_name})**, "
                  f"which the launchers download. Players: get the launcher from [that page](https://github.com/{REPO}/releases/tag/{page_tag_name}).\n\n"
                  + notes)
    with tempfile.TemporaryDirectory(prefix="tpf2mp-page-") as tmp:
        chosen = launchers(gh, args, Path(tmp))
        if page is None:
            say(f"creating the page {page_tag_name} at {commit[:10]}")
            page = gh.write(f"create the page {page_tag_name}", "POST", f"/repos/{REPO}/releases", body={
                "tag_name": page_tag_name, "target_commitish": commit, "name": mod["name"] or f"TpF2 Multiplayer {version}",
                "body": body, "draft": True, "prerelease": mod["prerelease"], "make_latest": "false"})
        elif page.get("body") != body:
            gh.write(f"update the page {page_tag_name}'s notes", "PATCH", f"/repos/{REPO}/releases/{page['id']}", body={"body": body})
        if page is None:
            say(f"  (dry run) the page would carry: {', '.join(n for _, n in chosen)}")
        else:
            page["_repo"] = REPO
            keep = {n for _, n in chosen}
            for a in page.get("assets", []):
                if a["name"] not in keep:
                    gh.write(f"remove {a['name']} from the page", "DELETE", f"/repos/{REPO}/releases/assets/{a['id']}")
            page["assets"] = [a for a in page.get("assets", []) if a["name"] in keep]
            say(f"page {page_tag_name} launchers:")
            upload(gh, page, [p for p, _ in chosen], [n for _, n in chosen])
    if not args.publish:
        say("left the page as a draft (--publish publishes it, then the install files again)")
        return
    if page is not None:
        published(gh, page, not mod["prerelease"])
        say(f"published: https://github.com/{REPO}/releases/tag/{page_tag_name}")
    files_name = f"TpF2 Multiplayer {version} (install files)"
    mod["name"], mod["body"] = files_name, files_body
    if not args.dry_run:
        time.sleep(2)
    # renamed and re-created in one go: republish takes the name and notes from `mod`
    republish(gh, mod, args.dry_run)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tag", help="the release tag, e.g. v0.7.0.6; 'launcher' for a launcher update on its own; "
                                "'page' to move a published version to this layout")
    ap.add_argument("page_tag", nargs="?", help="with page: the version's tag, e.g. v0.7.0.5")
    ap.add_argument("--replace-page", action="store_true", help="with page: make an already published page again")
    ap.add_argument("--linux-dir", help="folder with tpf2mp-linux-<v>-native.run/.tar.gz/.sha256 (a mod version)")
    ap.add_argument("--payload-dir", help="folder with the Windows/Proton files instead of the tag run's artifact")
    ap.add_argument("--windows-launcher", help="a launcher Setup.exe instead of tearded's newest release")
    ap.add_argument("--linux-launcher", help="a Linux launcher AppImage instead of tearded's newest release")
    ap.add_argument("--no-linux-launcher", action="store_true", help="publish without a Linux launcher (none released yet)")
    ap.add_argument("--publish", action="store_true", help="publish the draft at the end (default: leave it a draft)")
    ap.add_argument("--dry-run", action="store_true", help="check everything, change nothing on GitHub")
    args = ap.parse_args()
    if args.tag == "launcher":
        return launcher_release(GitHub(token(), args.dry_run), args)
    if args.tag == "page":
        return page_release(GitHub(token(), args.dry_run), args)
    if not re.fullmatch(r"v\d+\.\d+(\.\d+){0,2}", args.tag):
        fail("the tag looks like v0.7.0.6, or is 'launcher'")
    if not args.linux_dir:
        fail("a mod version needs --linux-dir")
    version = args.tag[1:]
    gh = GitHub(token(), args.dry_run)

    mod = draft_release(gh, args.tag)
    say(f"install files release {args.tag}: {'draft' if mod['draft'] else 'PUBLISHED'}, {len(mod['assets'])} asset(s)")
    # A published release is left alone: its files are where older launchers and
    # Proton scripts download them.
    if not mod["draft"] and not args.dry_run:
        fail(f"{args.tag} is already published; this only completes a draft (--dry-run to look anyway)")
    page_tag = version
    page = find_release(gh, page_tag)
    if page and not page["draft"] and not args.dry_run:
        fail(f"the page {page_tag} is already published")
    # the notes' download table links the page's launchers (older drafts linked the v tag)
    body = (mod.get("body") or "").replace(f"/releases/download/{args.tag}/", f"/releases/download/{page_tag}/")
    if args.no_linux_launcher:
        # no Linux launcher yet: its row would link a file this release does not have
        pkg_url = f"https://github.com/{PACKAGES_REPO}/releases/download/{args.tag}"
        body = "\n".join(l if not l.startswith("| **Linux / Steam Deck**") else
                         f"| **Linux / Steam Deck** (the Windows game under Proton) | [install_proton.sh]({pkg_url}/install_proton.sh)"
                         f" -- run it with sh; it fetches the rest itself (the Linux launcher is on its way) |"
                         for l in body.split("\n"))
    page_url = f"https://github.com/{REPO}/releases/tag/{page_tag}"
    files_body = (f"The install files of **[TpF2 Multiplayer {version}]({page_url})**, which the launchers download. "
                  f"Players: get the launcher from [that page]({page_url}).\n\n" + body)
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
                "body": f"Install files of [TpF2 Multiplayer {version}]({page_url}), "
                        "downloaded by its launchers and by install_proton.sh. Players: get the launcher from that release."})
        if pkg is not None:
            pkg["_repo"] = PACKAGES_REPO
            say(f"packages release: {pkg['html_url']}")
            upload(gh, pkg, files)

        # the page: exactly the two launchers, the notes
        if page is None:
            say(f"creating the page {page_tag}")
            page_tag_ref(gh, page_tag, tag_commit(gh, args.tag), args.dry_run)
            page = gh.write(f"create the page {page_tag}", "POST", f"/repos/{REPO}/releases", body={
                "tag_name": page_tag, "target_commitish": mod.get("target_commitish") or "main",
                "name": mod.get("name") or f"TpF2 Multiplayer {version}", "body": body,
                "draft": True, "prerelease": mod["prerelease"], "make_latest": "false"})
        elif page.get("body") != body:
            gh.write(f"update the page {page_tag}'s notes", "PATCH", f"/repos/{REPO}/releases/{page['id']}", body={"body": body})
        if page is not None:
            page["_repo"] = REPO
            keep = {name for _, name in chosen}
            for a in page.get("assets", []):
                if a["name"] not in keep:
                    gh.write(f"remove {a['name']} from the page", "DELETE", f"/repos/{REPO}/releases/assets/{a['id']}")
            page["assets"] = [a for a in page.get("assets", []) if a["name"] in keep]
            say(f"page {page_tag} launchers:")
            upload(gh, page, [p for p, _ in chosen], [n for _, n in chosen])

        # v<version>: exactly the install files
        mod["_repo"] = REPO
        keep = {p.name for p in files}
        for a in mod["assets"]:
            if a["name"] not in keep:
                gh.write(f"remove {a['name']} from {args.tag}", "DELETE", f"/repos/{REPO}/releases/assets/{a['id']}")
        mod["assets"] = [a for a in mod["assets"] if a["name"] in keep]
        say(f"{args.tag} install files:")
        upload(gh, mod, files)
    files_name = f"TpF2 Multiplayer {version} (install files)"
    if mod.get("body") != files_body or mod.get("name") != files_name:
        gh.write(f"update {args.tag}'s name and notes", "PATCH", f"/repos/{REPO}/releases/{mod['id']}",
                 body={"body": files_body, "name": files_name})

    if args.publish:
        if page is not None:
            published(gh, page, not mod["prerelease"])
        # strictly later than the page: launchers up to 1.2.0 take the newest published one
        if not args.dry_run:
            time.sleep(2)
        published(gh, mod, False)
        say(("(dry run) would be " if args.dry_run else "") + f"published: {page_url} (launchers), "
            f"https://github.com/{REPO}/releases/tag/{args.tag} (install files)")
    else:
        say("left as drafts (--publish publishes the page, then the install files)")

if __name__ == "__main__":
    main()
