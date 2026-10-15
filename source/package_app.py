"""Build-time packaging of a portable Python and an immutable managed engine.

Inputs are software/runtime bundles only. Never reads a user Codex/CC Switch
home, credentials, signing passwords, or a live multi-instance workspace.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

from local_signing import sign_app
from managed_desktop import APP_BUNDLE_NAME, BUNDLE_ID, prepare_copy


MACHO = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
         b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout


def package_python(source: Path, destination: Path, identity: str) -> None:
    source = source.resolve()
    if not (source / "bin/python3").is_file():
        raise ValueError("--python-runtime must contain a portable bin/python3 and lib directory")
    if destination.exists():
        shutil.rmtree(destination)
    # The application uses only stdlib. Do not distribute the build host's
    # third-party packages or mutable import caches.
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc",
                                                 "include", "pkgconfig", "man"))
    for path in destination.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic not in MACHO:
            continue
        libraries = run(["/usr/bin/otool", "-L", str(path)]).splitlines()[1:]
        for line in libraries:
            library = line.strip().split(" (", 1)[0]
            if library.startswith("/") and not library.startswith(("/usr/lib/", "/System/Library/")):
                raise ValueError("Portable Python depends on an external library: " + library)
        sign_app(path, deep=False, identity=identity)
    executable = destination / "bin/python3"
    probe = run([str(executable), "-I", "-B", "-c",
                 "import sys,sqlite3,ctypes,tomllib,ssl,zlib,hashlib;"
                 "assert sys.version_info >= (3,11);print(sys.prefix)"])
    if Path(probe.strip()).resolve() != destination.resolve():
        raise ValueError("Packaged Python is not relocatable")
    if not list(destination.glob("lib/python*/LICENSE.txt")):
        raise ValueError("Portable Python license is missing")


def package_engine(app: Path, source_app: Path, identity: str) -> None:
    resources = app / "Contents/Resources"
    # Signing can change this file's bytes. Do it before the engine receipt
    # fingerprints the launcher and embeds the exact same code.
    launcher = Path(__file__).with_name("duo-instance-launcher")
    sign_app(launcher, deep=False, identity=identity)
    shutil.copyfile(launcher, resources / "source/duo-instance-launcher")
    destination = resources / "desktop"
    if destination.exists():
        shutil.rmtree(destination)
    with tempfile.TemporaryDirectory(prefix="codex-duo-build-") as temporary:
        root = Path(temporary).resolve()
        (root / "manifest.json").write_text(json.dumps({"owner": "codex-dual", "root": str(root)}))
        template = prepare_copy(root, source_app, signing_identity=identity)
        result = subprocess.run(["/usr/bin/codesign", "-d", "-r-",
                                 str(template / "Contents/MacOS/ChatGPT")],
                                capture_output=True, text=True, check=True)
        # codesign prefixes an implicit ad-hoc requirement with "# ".
        requirements = [line.removeprefix("# ") for line in (result.stdout + result.stderr).splitlines()]
        requirement = next(line for line in requirements if line.startswith("designated => "))
        receipt = root / "desktop/copy.json"
        record = json.loads(receipt.read_text())
        record["engine_requirement"] = requirement
        metadata = subprocess.run(["/usr/bin/codesign", "-dvvv", str(template)],
                                  capture_output=True, text=True, check=True)
        record["engine_cdhash"] = next(line.split("=", 1)[1]
                                       for line in (metadata.stdout + metadata.stderr).splitlines()
                                       if line.startswith("CDHash="))
        receipt.write_text(json.dumps(record, indent=2) + "\n")
        # Keep local packaging cheap on APFS; copies distributed off this Mac
        # remain ordinary complete files, never links to the installed Codex.
        run(["/bin/cp", "-cR", str(root / "desktop"), str(destination)])
    info = plistlib.loads((destination / APP_BUNDLE_NAME / "Contents/Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == BUNDLE_ID


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--python-runtime", type=Path, required=True)
    parser.add_argument("--source-app", type=Path, default=Path("/Applications/ChatGPT.app"))
    parser.add_argument("--sign-identity", default="-")
    args = parser.parse_args()
    package_python(args.python_runtime, args.app / "Contents/Resources/python", args.sign_identity)
    package_engine(args.app, args.source_app, args.sign_identity)
    # All nested code is already signed. Preserve the engine's signing identity.
    sign_app(args.app, deep=False, identity=args.sign_identity)
    print("Packaged portable Python and fixed Codex Duo engine; no user data included.")


if __name__ == "__main__":
    main()
