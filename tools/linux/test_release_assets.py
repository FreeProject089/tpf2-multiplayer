#!/usr/bin/env python3
"""Offline publisher contract check for build_release.sh output.

Usage: python3 tools/linux/test_release_assets.py DIR VERSION
"""
import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile

spec = importlib.util.spec_from_file_location(
    "publish_release", Path(__file__).resolve().parents[1] / "publish_release.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
folder, version = Path(sys.argv[1]), sys.argv[2]
files = publisher.linux_files(folder, version)
for path in files[:2]:
    legacy = folder / path.name.replace("-native.", ".", 1)
    assert publisher.sha256(path) == publisher.sha256(legacy), path
assert files[0].stat().st_mode & 0o111, "native installer lost executable permission"
with tempfile.TemporaryDirectory(prefix="tpf2mp-assets-test-") as tmp:
    tmp = Path(tmp)
    for path in files:
        shutil.copy2(path, tmp / path.name)
    # A stale checksum or damaged asset must stop release preparation.
    for path in files[:2]:
        copy = tmp / path.name
        with copy.open("ab") as f:
            f.write(b"corrupt")
        try:
            publisher.linux_files(tmp, version)
        except SystemExit as error:
            assert "does not match" in str(error), error
        else:
            raise AssertionError(f"accepted corrupt {path.name}")
        shutil.copy2(path, copy)
    (tmp / files[0].name).unlink()
    try:
        publisher.linux_files(tmp, version)
    except SystemExit as error:
        assert "lacks" in str(error), error
    else:
        raise AssertionError("accepted missing installer")
print("PASS: native release names, legacy bytes, executable mode, checksums and rejection of corrupt/missing assets")
